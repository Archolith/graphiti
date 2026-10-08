"""Focused no-DB tests for the fork-native edge-expiry hook.

Covers:
- no hook and an EXPIRE hook are identical to upstream (path 1 expires the edge)
- WORLD_END keeps a new edge and an LLM-matched duplicate unexpired
- the path-2 overlap rule: a newer contradiction inside the window truncates and expires,
  one at or after the end is ignored; EXPIRE keeps upstream path 2
- path 3 (older contradicted edges) is unchanged by the decision
- invocation gates (no invalid_at, already expired, early return, exact-fact fast path)
- strict invalid-return TypeError and hook exception propagation
- call-site wiring: resolve_extracted_edges, dedupe_edges_bulk, add_triplet, and every
  resolver call in graphiti_core; the no-hook call keeps the legacy signature
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import graphiti_core.graphiti as graphiti_module
import graphiti_core.utils.bulk_utils as bulk_utils
import graphiti_core.utils.maintenance.edge_operations as edge_ops
from graphiti_core.cross_encoder import CrossEncoderClient
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.edge_expiry import (
    EdgeExpiryContext,
    EdgeExpiryDecision,
    EdgeExpiryHook,
    edge_expiry_kwargs,
)
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder import EmbedderClient
from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError
from graphiti_core.graphiti import Graphiti
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.search.search_config import SearchResults
from graphiti_core.utils.maintenance.edge_operations import (
    resolve_extracted_edge,
    resolve_extracted_edges,
)

GROUP_ID = 'expiry_test_group'
FIXED_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TRIP_START = datetime(2026, 3, 27, tzinfo=timezone.utc)
TRIP_END = datetime(2026, 4, 1, tzinfo=timezone.utc)


class RecordingExpiryHook:
    """Deterministic hook that records every context and returns a fixed decision."""

    def __init__(self, decision: object = EdgeExpiryDecision.WORLD_END):
        self.decision = decision
        self.contexts: list[EdgeExpiryContext] = []

    async def decide_edge_expiry(self, context: EdgeExpiryContext) -> EdgeExpiryDecision:
        self.contexts.append(context)
        return self.decision  # type: ignore[return-value]


class RaisingExpiryHook:
    async def decide_edge_expiry(self, context: EdgeExpiryContext) -> EdgeExpiryDecision:
        raise RuntimeError('hook failed')


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(edge_ops, 'utc_now', lambda: FIXED_NOW)


def _edge(
    fact: str,
    *,
    valid_at: datetime | None = TRIP_START,
    invalid_at: datetime | None = TRIP_END,
    expired_at: datetime | None = None,
    episodes: list[str] | None = None,
) -> EntityEdge:
    return EntityEdge(
        source_node_uuid='source_uuid',
        target_node_uuid='target_uuid',
        name='TRAVELED_TO',
        group_id=GROUP_ID,
        fact=fact,
        episodes=episodes if episodes is not None else [],
        created_at=FIXED_NOW - timedelta(days=1),
        valid_at=valid_at,
        invalid_at=invalid_at,
        expired_at=expired_at,
    )


def _episode() -> EpisodicNode:
    return EpisodicNode(
        uuid='episode_uuid',
        name='Episode',
        group_id=GROUP_ID,
        source='message',
        source_description='desc',
        content='Episode content',
        valid_at=FIXED_NOW,
    )


def _llm(duplicate_facts: list[int] | None = None, contradicted_facts: list[int] | None = None):
    client = MagicMock()
    client.generate_response = AsyncMock(
        return_value={
            'duplicate_facts': duplicate_facts or [],
            'contradicted_facts': contradicted_facts or [],
        }
    )
    return client


async def _resolve(hook, *, related=None, existing=None, extracted=None, llm=None):
    extracted = extracted or _edge('Alice traveled to Rome from Mar 27 to Apr 1')
    related = related if related is not None else [_edge('Alice likes pasta', invalid_at=None)]
    kwargs = edge_expiry_kwargs(hook)
    resolved, invalidated, duplicates = await resolve_extracted_edge(
        llm or _llm(),
        extracted,
        related,
        existing or [],
        _episode(),
        None,
        **kwargs,
    )
    return extracted, resolved, invalidated, duplicates


def _snapshot(edge: EntityEdge) -> dict:
    return edge.model_dump(exclude={'uuid'})


# --- protocol and helpers -------------------------------------------------------------


def test_hook_is_runtime_checkable_protocol():
    assert isinstance(RecordingExpiryHook(), EdgeExpiryHook)


def test_frozen_context_rejects_reassignment():
    edge = _edge('fact')
    context = EdgeExpiryContext(
        extracted_edge=edge, resolved_edge=edge, is_duplicate=False, episode=None
    )
    with pytest.raises(AttributeError):
        context.is_duplicate = True  # type: ignore[misc]


def test_kwargs_helper_is_empty_without_hook():
    hook = RecordingExpiryHook()
    assert edge_expiry_kwargs(None) == {}
    assert edge_expiry_kwargs(hook) == {'edge_expiry_hook': hook}


# --- upstream identity ------------------------------------------------------------------


async def test_no_hook_expires_edge_with_own_end():
    _, resolved, invalidated, _ = await _resolve(None)
    assert resolved.expired_at == FIXED_NOW
    assert resolved.invalid_at == TRIP_END
    assert invalidated == []


async def test_expire_hook_is_identical_to_no_hook():
    _, baseline, base_inv, base_dup = await _resolve(None)
    hook = RecordingExpiryHook(EdgeExpiryDecision.EXPIRE)
    _, resolved, invalidated, duplicates = await _resolve(hook)

    assert len(hook.contexts) == 1
    assert _snapshot(resolved) == _snapshot(baseline)
    assert [_snapshot(e) for e in invalidated] == [_snapshot(e) for e in base_inv]
    assert [_snapshot(e) for e in duplicates] == [_snapshot(e) for e in base_dup]


async def test_expire_hook_keeps_upstream_path_two_masking():
    """EXPIRE: path 1 expires first, so a newer contradiction does not truncate."""
    newer = _edge('Alice lives in Rome', valid_at=TRIP_START + timedelta(days=2), invalid_at=None)
    llm = _llm(contradicted_facts=[1])
    hook = RecordingExpiryHook(EdgeExpiryDecision.EXPIRE)
    _, resolved, _, _ = await _resolve(hook, existing=[newer], llm=llm)
    assert resolved.invalid_at == TRIP_END
    assert resolved.expired_at == FIXED_NOW


# --- WORLD_END ---------------------------------------------------------------------------


async def test_world_end_keeps_new_edge_unexpired():
    hook = RecordingExpiryHook()
    extracted, resolved, invalidated, _ = await _resolve(hook)

    assert resolved is extracted
    assert resolved.expired_at is None
    assert resolved.invalid_at == TRIP_END
    assert invalidated == []
    (context,) = hook.contexts
    assert context.extracted_edge is extracted
    assert context.resolved_edge is resolved
    assert context.is_duplicate is False
    assert context.episode is not None and context.episode.uuid == 'episode_uuid'


async def test_world_end_keeps_llm_matched_duplicate_unexpired():
    """P3-1: a restatement resolved to the stored ended edge must not re-expire it."""
    stored = _edge('Alice was in Rome Mar 27 - Apr 1', episodes=['first_episode'])
    hook = RecordingExpiryHook()
    extracted, resolved, _, duplicates = await _resolve(
        hook, related=[stored], llm=_llm(duplicate_facts=[0])
    )

    assert resolved is stored
    assert duplicates == [stored]
    assert resolved.expired_at is None
    assert resolved.invalid_at == TRIP_END
    assert 'episode_uuid' in resolved.episodes
    (context,) = hook.contexts
    assert context.extracted_edge is extracted
    assert context.resolved_edge is stored
    assert context.is_duplicate is True


async def test_no_hook_re_expires_llm_matched_duplicate():
    """Pin of the upstream behavior the hook exists to change."""
    stored = _edge('Alice was in Rome Mar 27 - Apr 1')
    _, resolved, _, _ = await _resolve(None, related=[stored], llm=_llm(duplicate_facts=[0]))
    assert resolved is stored
    assert resolved.expired_at == FIXED_NOW


async def test_world_end_contradiction_inside_window_truncates_and_expires():
    inside = TRIP_START + timedelta(days=2)
    candidate = _edge('Alice moved to Milan', valid_at=inside, invalid_at=None)
    hook = RecordingExpiryHook()
    _, resolved, _, _ = await _resolve(hook, existing=[candidate], llm=_llm(contradicted_facts=[1]))

    assert resolved.invalid_at == inside
    assert resolved.expired_at == FIXED_NOW


@pytest.mark.parametrize('offset', [timedelta(0), timedelta(days=30)])
async def test_world_end_contradiction_at_or_after_end_is_ignored(offset):
    candidate = _edge('Alice moved to Milan', valid_at=TRIP_END + offset, invalid_at=None)
    hook = RecordingExpiryHook()
    _, resolved, _, _ = await _resolve(hook, existing=[candidate], llm=_llm(contradicted_facts=[1]))

    assert resolved.invalid_at == TRIP_END
    assert resolved.expired_at is None


async def test_world_end_contradiction_before_start_does_not_truncate():
    """Path 2 only considers candidates newer than the edge; unchanged by WORLD_END."""
    older = _edge(
        'Alice was in Paris',
        valid_at=TRIP_START - timedelta(days=10),
        invalid_at=None,
    )
    hook = RecordingExpiryHook()
    _, resolved, invalidated, _ = await _resolve(
        hook, existing=[older], llm=_llm(contradicted_facts=[1])
    )

    assert resolved.invalid_at == TRIP_END
    assert resolved.expired_at is None
    assert invalidated == [older]


@pytest.mark.parametrize('hook_decision', [None, EdgeExpiryDecision.WORLD_END])
async def test_path_three_on_older_edges_is_unchanged(hook_decision):
    overlapping = _edge(
        'Alice was in Paris',
        valid_at=TRIP_START - timedelta(days=10),
        invalid_at=None,
    )
    ended_before = _edge(
        'Alice was in Oslo',
        valid_at=TRIP_START - timedelta(days=20),
        invalid_at=TRIP_START - timedelta(days=15),
    )
    hook = RecordingExpiryHook(hook_decision) if hook_decision else None
    _, _, invalidated, _ = await _resolve(
        hook,
        existing=[overlapping, ended_before],
        llm=_llm(contradicted_facts=[1, 2]),
    )

    assert invalidated == [overlapping]
    assert overlapping.invalid_at == TRIP_START
    assert overlapping.expired_at == FIXED_NOW
    assert ended_before.expired_at is None


# --- invocation gates --------------------------------------------------------------------


async def test_hook_not_called_without_invalid_at():
    hook = RecordingExpiryHook()
    extracted = _edge('Alice likes Rome', invalid_at=None)
    _, resolved, _, _ = await _resolve(hook, extracted=extracted)
    assert hook.contexts == []
    assert resolved.expired_at is None


async def test_hook_not_called_when_duplicate_already_expired():
    expired_before = FIXED_NOW - timedelta(days=5)
    stored = _edge('Alice was in Rome Mar 27 - Apr 1', expired_at=expired_before)
    hook = RecordingExpiryHook()
    _, resolved, _, _ = await _resolve(hook, related=[stored], llm=_llm(duplicate_facts=[0]))
    assert hook.contexts == []
    assert resolved.expired_at == expired_before


async def test_hook_not_called_on_early_return():
    hook = RecordingExpiryHook()
    llm = _llm()
    _, resolved, _, _ = await _resolve(hook, related=[], existing=[], llm=llm)
    assert hook.contexts == []
    assert resolved.expired_at is None
    llm.generate_response.assert_not_called()


async def test_hook_not_called_on_exact_fact_fast_path():
    stored = _edge('Alice traveled to Rome from Mar 27 to Apr 1')
    hook = RecordingExpiryHook()
    _, resolved, _, _ = await _resolve(hook, related=[stored])
    assert resolved is stored
    assert hook.contexts == []
    assert resolved.expired_at is None


# --- invalid hooks -----------------------------------------------------------------------


@pytest.mark.parametrize('bad', [True, 'world_end', None])
async def test_invalid_return_raises_typeerror(bad):
    with pytest.raises(TypeError, match='EdgeExpiryDecision'):
        await _resolve(RecordingExpiryHook(bad))


async def test_hook_exception_propagates():
    with pytest.raises(RuntimeError, match='hook failed'):
        await _resolve(RaisingExpiryHook())


# --- call-site wiring --------------------------------------------------------------------


def _patch_resolve_extracted_edges_deps(monkeypatch, captured: list[dict]):
    async def fake_resolve_extracted_edge(*args, **kwargs):
        captured.append(kwargs)
        return args[1], [], []

    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    monkeypatch.setattr(EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))
    monkeypatch.setattr(edge_ops, 'semaphore_gather', immediate_gather)
    monkeypatch.setattr(edge_ops, 'search', AsyncMock(return_value=SearchResults()))
    monkeypatch.setattr(edge_ops, 'resolve_extracted_edge', fake_resolve_extracted_edge)


@pytest.mark.parametrize('with_hook', [True, False])
async def test_resolve_extracted_edges_threads_hook(monkeypatch, with_hook):
    captured: list[dict] = []
    _patch_resolve_extracted_edges_deps(monkeypatch, captured)
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=MagicMock(), embedder=MagicMock(), cross_encoder=MagicMock()
    )
    nodes = [
        EntityNode(uuid='source_uuid', name='Alice', group_id=GROUP_ID, labels=['Entity']),
        EntityNode(uuid='target_uuid', name='Rome', group_id=GROUP_ID, labels=['Entity']),
    ]
    hook = RecordingExpiryHook() if with_hook else None

    await resolve_extracted_edges(
        clients,  # type: ignore[arg-type]
        [_edge('Alice traveled to Rome')],
        _episode(),
        nodes,
        {},
        {},
        **edge_expiry_kwargs(hook),
    )

    assert len(captured) == 1
    if with_hook:
        assert captured[0].get('edge_expiry_hook') is hook
    else:
        assert 'edge_expiry_hook' not in captured[0]


@pytest.mark.parametrize('with_hook', [True, False])
async def test_dedupe_edges_bulk_threads_hook(monkeypatch, with_hook):
    captured: list[dict] = []

    async def fake_embeddings(embedder, edges):
        for edge in edges:
            edge.fact_embedding = [0.1, 0.2, 0.3]

    async def fake_resolve_extracted_edge(*args, **kwargs):
        captured.append(kwargs)
        return args[1], [], []

    monkeypatch.setattr(bulk_utils, 'create_entity_edge_embeddings', fake_embeddings)
    monkeypatch.setattr(bulk_utils, 'resolve_extracted_edge', fake_resolve_extracted_edge)
    clients = SimpleNamespace(llm_client=MagicMock(), embedder=MagicMock())
    hook = RecordingExpiryHook() if with_hook else None

    await bulk_utils.dedupe_edges_bulk(
        clients,  # type: ignore[arg-type]
        [[_edge('Alice traveled to Rome')]],
        [(_episode(), [])],
        [],
        {},
        {},
        **edge_expiry_kwargs(hook),
    )

    assert len(captured) == 1
    if with_hook:
        assert captured[0].get('edge_expiry_hook') is hook
    else:
        assert 'edge_expiry_hook' not in captured[0]


async def test_dedupe_edges_bulk_reaches_real_resolver_hook(monkeypatch):
    async def fake_embeddings(embedder, edges):
        for edge in edges:
            edge.fact_embedding = [0.1, 0.2, 0.3]

    monkeypatch.setattr(bulk_utils, 'create_entity_edge_embeddings', fake_embeddings)
    ended = _edge('Alice traveled to Rome in spring')
    other = _edge('Alice liked Rome a lot', invalid_at=None)
    clients = SimpleNamespace(llm_client=_llm(), embedder=MagicMock())
    hook = RecordingExpiryHook()

    await bulk_utils.dedupe_edges_bulk(
        clients,  # type: ignore[arg-type]
        [[ended, other]],
        [(_episode(), [])],
        [],
        {},
        {},
        edge_expiry_hook=hook,
    )

    assert [c.extracted_edge.uuid for c in hook.contexts] == [ended.uuid]
    assert ended.expired_at is None


def _make_full_graphiti(edge_expiry_hook=None) -> Graphiti:
    return Graphiti(
        graph_driver=Mock(spec=GraphDriver),
        llm_client=Mock(spec=LLMClient),
        embedder=Mock(spec=EmbedderClient),
        cross_encoder=Mock(spec=CrossEncoderClient),
        edge_expiry_hook=edge_expiry_hook,
    )


def test_graphiti_constructor_stores_edge_expiry_hook():
    hook = RecordingExpiryHook()
    assert _make_full_graphiti(hook).edge_expiry_hook is hook
    assert _make_full_graphiti().edge_expiry_hook is None


def test_graphiti_bypassing_init_defaults_to_no_hook():
    assert Graphiti.__new__(Graphiti).edge_expiry_hook is None


async def _run_add_triplet(monkeypatch, graphiti: Graphiti) -> list[dict]:
    source = EntityNode(name='Alice', group_id=GROUP_ID, labels=['Entity'])
    target = EntityNode(name='Rome', group_id=GROUP_ID, labels=['Entity'])
    source.name_embedding = [0.1]
    target.name_embedding = [0.1]
    edge = EntityEdge(
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        name='TRAVELED_TO',
        group_id=GROUP_ID,
        fact='Alice traveled to Rome',
        episodes=[],
        created_at=FIXED_NOW,
        fact_embedding=[0.1],
    )
    captured: list[dict] = []

    async def fake_resolve_nodes(clients, extracted_nodes, *args, **kwargs):
        return [extracted_nodes[0]], {}, []

    async def fake_get_node_by_uuid(driver, node_uuid):
        raise NodeNotFoundError(node_uuid)

    async def fake_get_edge_by_uuid(driver, edge_uuid):
        raise EdgeNotFoundError(edge_uuid)

    async def fake_search(*args, **kwargs):
        result = Mock()
        result.edges = []
        return result

    async def fake_resolve_extracted_edge(*args, **kwargs):
        captured.append({'args': args, 'kwargs': kwargs})
        return edge, [], []

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(graphiti_module.EntityNode, 'get_by_uuid', fake_get_node_by_uuid)
    monkeypatch.setattr(graphiti_module.EntityEdge, 'get_by_uuid', fake_get_edge_by_uuid)
    monkeypatch.setattr(graphiti_module.EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))
    monkeypatch.setattr(graphiti_module, 'search', fake_search)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edge', fake_resolve_extracted_edge)
    monkeypatch.setattr(graphiti_module, 'create_entity_edge_embeddings', noop)
    monkeypatch.setattr(graphiti_module, 'create_entity_node_embeddings', noop)
    monkeypatch.setattr(graphiti_module, 'add_nodes_and_edges_bulk', noop)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_nodes', fake_resolve_nodes)

    await graphiti.add_triplet(source, edge, target)
    return captured


async def test_add_triplet_threads_hook(monkeypatch):
    hook = RecordingExpiryHook()
    captured = await _run_add_triplet(monkeypatch, _make_full_graphiti(hook))
    assert len(captured) == 1
    assert captured[0]['kwargs'] == {'edge_expiry_hook': hook}


async def test_add_triplet_no_hook_uses_legacy_signature(monkeypatch):
    captured = await _run_add_triplet(monkeypatch, _make_full_graphiti())
    assert len(captured) == 1
    assert captured[0]['kwargs'] == {}


_RESOLVER_CALLS = {'resolve_extracted_edge', 'resolve_extracted_edges', 'dedupe_edges_bulk'}


def _resolver_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, 'attr', None)
            if name in _RESOLVER_CALLS:
                calls.append(node)
    return calls


def test_every_resolver_call_in_graphiti_core_threads_the_hook():
    """Structural guard: a new resolver call site without the hook would bypass it."""
    package = Path(__file__).resolve().parents[1] / 'graphiti_core'
    found = 0
    for path in package.rglob('*.py'):
        for call in _resolver_calls(path):
            found += 1
            threaded = [
                kw
                for kw in call.keywords
                if kw.arg is None
                and isinstance(kw.value, ast.Call)
                and isinstance(kw.value.func, ast.Name)
                and kw.value.func.id == 'edge_expiry_kwargs'
            ]
            assert threaded, f'{path.name}:{call.lineno} does not thread edge_expiry_hook'
    # graphiti.py x4, bulk_utils.py x1, edge_operations.py x1
    assert found == 6


def test_no_menhir_imports_in_edge_expiry_mechanism():
    path = Path(__file__).resolve().parents[1] / 'graphiti_core' / 'edge_expiry.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.lower().startswith('menhir')
        elif isinstance(node, ast.Import):
            assert all(not alias.name.lower().startswith('menhir') for alias in node.names)
