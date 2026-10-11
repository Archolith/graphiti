"""Caller-configured preserved edge attributes.

On the LLM resolution path, attributes are recomputed (schema) or cleared (no schema). The
named keys of the resolved edge are captured first and restored afterwards, before the
edge-expiry hook runs. The early return and the exact-fact fast path are unchanged, and the
option is threaded through every resolver call in Graphiti.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest
from pydantic import BaseModel

import graphiti_core.graphiti as graphiti_module
import graphiti_core.utils.bulk_utils as bulk_utils
import graphiti_core.utils.maintenance.edge_operations as edge_ops
from graphiti_core.cross_encoder import CrossEncoderClient
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.edge_expiry import EdgeExpiryContext, EdgeExpiryDecision
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder import EmbedderClient
from graphiti_core.graphiti import Graphiti
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_config import SearchResults
from graphiti_core.utils.maintenance.edge_operations import (
    preserved_attributes_kwargs,
    resolve_extracted_edge,
    resolve_extracted_edges,
)

GROUP_ID = 'preserved_attributes_group'
FIXED_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
STORED_UUID = 'stored-rating'
STORED_FACT = 'Alice rated the Rome hotel'
PARAPHRASE = 'Alice gave the hotel in Rome a rating'
WORLD_END = '2024-04-01T00:00:00+00:00'
PRESERVED = frozenset({'time_expiry', 'time_world_end'})
KEPT = {'time_expiry': 'world_end', 'time_world_end': WORLD_END}


class Rating(BaseModel):
    rating: int


class _SpyHook:
    """Records a snapshot of the resolved edge's attributes at decision time."""

    def __init__(self):
        self.contexts: list[EdgeExpiryContext] = []
        self.seen_attributes: list[dict] = []

    async def decide_edge_expiry(self, context: EdgeExpiryContext) -> EdgeExpiryDecision:
        self.contexts.append(context)
        self.seen_attributes.append(dict(context.resolved_edge.attributes))
        return EdgeExpiryDecision.WORLD_END


def _edge(fact: str, *, uuid: str | None = None, attributes: dict | None = None) -> EntityEdge:
    return EntityEdge(
        uuid=uuid if uuid is not None else str(uuid4()),
        source_node_uuid='source_uuid',
        target_node_uuid='target_uuid',
        name='RATED',
        group_id=GROUP_ID,
        fact=fact,
        episodes=['episode_0'] if uuid == STORED_UUID else [],
        created_at=FIXED_NOW - timedelta(days=30),
        attributes=dict(attributes) if attributes is not None else {},
    )


def _stored(*, attributes: dict | None = None, ended: bool = False) -> EntityEdge:
    if attributes is None:
        attributes = {**KEPT, 'scratch': 1}
    edge = _edge(STORED_FACT, uuid=STORED_UUID, attributes=attributes)
    edge.valid_at = FIXED_NOW - timedelta(days=30)
    if ended:
        edge.invalid_at = FIXED_NOW - timedelta(days=10)
    return edge


def _episode(index: int = 1) -> EpisodicNode:
    return EpisodicNode(
        uuid=f'episode_{index}',
        name=f'Episode {index}',
        group_id=GROUP_ID,
        source=EpisodeType.message,
        source_description='desc',
        content='Episode content',
        valid_at=FIXED_NOW,
    )


def _llm(rating: int = 2) -> MagicMock:
    async def generate_response(messages, **kwargs):
        if kwargs.get('prompt_name') == 'extract_edges.extract_attributes':
            return {'rating': rating}
        return {'duplicate_facts': [0], 'contradicted_facts': []}

    llm = MagicMock()
    llm.generate_response = AsyncMock(side_effect=generate_response)
    return llm


@pytest.fixture(autouse=True)
def _patch_edge_ops(monkeypatch):
    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    monkeypatch.setattr(edge_ops, 'utc_now', lambda: FIXED_NOW)
    monkeypatch.setattr(edge_ops, 'semaphore_gather', immediate_gather)
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    monkeypatch.setattr(edge_ops, '_extract_edge_timestamps', AsyncMock(return_value=None))
    monkeypatch.setattr(EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))


async def _resolve(llm, mention, stored, *, schema=False, **kwargs):
    edge_types = {'RATED': Rating} if schema else None
    return await resolve_extracted_edge(
        llm, mention, [stored], [], _episode(), edge_types, **kwargs
    )


# --- resolve_extracted_edge: LLM duplicate path ---------------------------------------------


async def test_no_schema_clears_everything_but_the_preserved_keys():
    stored = _stored()

    resolved, _, _ = await _resolve(
        _llm(), _edge(PARAPHRASE), stored, preserved_attributes=PRESERVED
    )

    assert resolved is stored
    assert resolved.attributes == KEPT  # 'scratch' cleared


async def test_no_schema_without_the_option_clears_to_empty():
    """Upstream behavior is unchanged when nothing is preserved."""
    resolved, _, _ = await _resolve(_llm(), _edge(PARAPHRASE), _stored())

    assert resolved.attributes == {}


async def test_schema_attributes_are_recomputed_and_preserved_keys_restored():
    resolved, _, _ = await _resolve(
        _llm(rating=2), _edge(PARAPHRASE), _stored(), schema=True, preserved_attributes=PRESERVED
    )

    assert resolved.attributes == {'rating': 2, **KEPT}


async def test_schema_wins_on_a_name_clash():
    stored = _stored(attributes={'rating': 1, 'scratch': 1})

    resolved, _, _ = await _resolve(
        _llm(rating=2),
        _edge(PARAPHRASE),
        stored,
        schema=True,
        preserved_attributes=frozenset({'rating'}),
    )

    assert resolved.attributes['rating'] == 2


async def test_a_preserved_key_absent_from_the_stored_edge_is_not_invented():
    stored = _stored(attributes={'time_expiry': 'world_end'})

    resolved, _, _ = await _resolve(
        _llm(), _edge(PARAPHRASE), stored, preserved_attributes=PRESERVED
    )

    assert resolved.attributes == {'time_expiry': 'world_end'}


async def test_the_expiry_hook_sees_the_preserved_keys():
    hook = _SpyHook()
    stored = _stored(ended=True)

    resolved, _, _ = await _resolve(
        _llm(),
        _edge(PARAPHRASE),
        stored,
        preserved_attributes=PRESERVED,
        edge_expiry_hook=hook,
    )

    assert len(hook.contexts) == 1
    assert hook.contexts[0].resolved_edge is stored
    assert hook.seen_attributes == [KEPT]
    assert resolved.expired_at is None  # WORLD_END decision kept it unexpired


# --- unchanged paths ------------------------------------------------------------------------


async def test_exact_fact_fast_path_is_unchanged_with_preserved_set():
    llm = _llm()
    stored = _stored()

    resolved, duplicates, invalidated = await _resolve(
        llm, _edge(STORED_FACT), stored, preserved_attributes=PRESERVED
    )

    assert resolved is stored
    assert (duplicates, invalidated) == ([], [])
    assert resolved.attributes == {**KEPT, 'scratch': 1}
    llm.generate_response.assert_not_awaited()


@pytest.mark.parametrize('schema', [True, False])
async def test_no_candidates_early_return_is_unchanged(schema):
    new = _edge(PARAPHRASE, attributes={'own': 'value'})
    edge_types = {'RATED': Rating} if schema else None

    resolved, duplicates, invalidated = await resolve_extracted_edge(
        _llm(),
        new,
        [],
        [],
        _episode(),
        edge_types,
        preserved_attributes=PRESERVED,
    )

    assert resolved is new
    assert (duplicates, invalidated) == ([], [])
    assert not PRESERVED & set(resolved.attributes)
    if not schema:
        assert resolved.attributes == {'own': 'value'}


# --- through the batch resolver -------------------------------------------------------------

NODES = [
    EntityNode(uuid='source_uuid', name='Alice', group_id=GROUP_ID, labels=['Entity']),
    EntityNode(uuid='target_uuid', name='Rome hotel', group_id=GROUP_ID, labels=['Entity']),
]


@pytest.mark.parametrize('with_schema', [True, False])
@pytest.mark.parametrize('exact_first', [True, False])
async def test_every_copy_of_the_stored_edge_ends_with_the_preserved_keys(
    monkeypatch, exact_first, with_schema
):
    async def fake_search(clients, query, group_ids, config, search_filter):
        return SearchResults(edges=[_stored()])

    monkeypatch.setattr(edge_ops, 'search', fake_search)
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=_llm(), embedder=MagicMock(), cross_encoder=MagicMock()
    )
    exact, paraphrase = _edge(STORED_FACT), _edge(PARAPHRASE)
    mentions = [exact, paraphrase] if exact_first else [paraphrase, exact]

    resolved, invalidated, _ = await resolve_extracted_edges(
        clients,  # type: ignore[arg-type]
        mentions,
        _episode(),
        NODES,
        {'RATED': Rating} if with_schema else {},
        {('Entity', 'Entity'): ['RATED']} if with_schema else {},
        preserved_attributes=PRESERVED,
    )

    copies = [edge for edge in resolved + invalidated if edge.uuid == STORED_UUID]
    assert len(copies) == 2
    expected = {'rating': 2, **KEPT} if with_schema else KEPT
    assert [edge.attributes for edge in copies] == [expected, expected]


# --- dedupe_edges_bulk ----------------------------------------------------------------------


async def _run_bulk(monkeypatch, **kwargs) -> list[dict]:
    captured: list[dict] = []

    async def fake_embeddings(embedder, edges):
        for edge in edges:
            edge.fact_embedding = [0.1, 0.2, 0.3]

    async def fake_resolve_extracted_edge(*args, **call_kwargs):
        captured.append(call_kwargs)
        return args[1], [], []

    monkeypatch.setattr(bulk_utils, 'create_entity_edge_embeddings', fake_embeddings)
    monkeypatch.setattr(bulk_utils, 'resolve_extracted_edge', fake_resolve_extracted_edge)
    clients = SimpleNamespace(llm_client=MagicMock(), embedder=MagicMock())

    await bulk_utils.dedupe_edges_bulk(
        clients,  # type: ignore[arg-type]
        [[_edge(PARAPHRASE)]],
        [(_episode(), [])],
        [],
        {},
        {},
        **kwargs,
    )
    return captured


async def test_dedupe_edges_bulk_forwards_preserved_attributes(monkeypatch):
    captured = await _run_bulk(monkeypatch, preserved_attributes=PRESERVED)

    assert len(captured) == 1
    assert captured[0].get('preserved_attributes') == PRESERVED


async def test_dedupe_edges_bulk_forwards_nothing_when_empty(monkeypatch):
    captured = await _run_bulk(monkeypatch)

    assert len(captured) == 1
    # Either absent or an empty set is acceptable; a non-empty value is not.
    assert captured[0].get('preserved_attributes', frozenset()) == frozenset()


# --- preserved_attributes_kwargs ------------------------------------------------------------


def test_preserved_attributes_kwargs_empty_is_empty():
    assert preserved_attributes_kwargs(frozenset()) == {}


def test_preserved_attributes_kwargs_non_empty_names_the_parameter():
    assert preserved_attributes_kwargs(PRESERVED) == {'preserved_attributes': PRESERVED}


# --- Graphiti call sites and constructor ----------------------------------------------------

_RESOLVER_CALLS = {'resolve_extracted_edge', 'resolve_extracted_edges', 'dedupe_edges_bulk'}


def _call_name(func: ast.expr) -> str | None:
    return func.id if isinstance(func, ast.Name) else getattr(func, 'attr', None)


def _threads_preserved_kwargs(call: ast.Call) -> bool:
    return any(
        keyword.arg is None
        and isinstance(keyword.value, ast.Call)
        and isinstance(keyword.value.func, ast.Name)
        and keyword.value.func.id == 'preserved_attributes_kwargs'
        for keyword in call.keywords
    )


def test_every_graphiti_resolver_call_threads_preserved_attributes():
    path = Path(graphiti_module.__file__)
    tree = ast.parse(path.read_text(encoding='utf-8'))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node.func) in _RESOLVER_CALLS
    ]

    assert len(calls) >= 4
    missing = [call.lineno for call in calls if not _threads_preserved_kwargs(call)]
    assert missing == []


def test_the_call_site_guard_rejects_an_unthreaded_call():
    good = ast.parse('f(a, **preserved_attributes_kwargs(self.x))').body[0].value  # type: ignore
    bad = ast.parse('f(a, **edge_expiry_kwargs(self.x))').body[0].value  # type: ignore
    literal = ast.parse('f(a, preserved_attributes=self.x)').body[0].value  # type: ignore

    assert _threads_preserved_kwargs(good)
    assert not _threads_preserved_kwargs(bad)
    assert not _threads_preserved_kwargs(literal)


def _make_graphiti(**kwargs) -> Graphiti:
    return Graphiti(
        graph_driver=Mock(spec=GraphDriver),
        llm_client=Mock(spec=LLMClient),
        embedder=Mock(spec=EmbedderClient),
        cross_encoder=Mock(spec=CrossEncoderClient),
        **kwargs,
    )


def test_graphiti_defaults_to_no_preserved_attributes():
    assert _make_graphiti().preserved_edge_attributes == frozenset()


def test_graphiti_stores_preserved_attributes_as_a_frozenset():
    graphiti = _make_graphiti(preserved_edge_attributes=['a', 'b', 'a'])

    assert graphiti.preserved_edge_attributes == frozenset({'a', 'b'})
    assert isinstance(graphiti.preserved_edge_attributes, frozenset)


@pytest.mark.parametrize('value', ['abc', b'abc', [1], [''], ['a', None]])
def test_graphiti_rejects_invalid_preserved_attributes(value):
    with pytest.raises(TypeError):
        _make_graphiti(preserved_edge_attributes=value)


def test_graphiti_class_default_is_empty():
    assert object.__new__(Graphiti).preserved_edge_attributes == frozenset()


# --- review fixes: echo, new edge, None attributes ------------------------------------------


def _llm_with(attribute_response: dict, duplicate_facts: list[int] | None = None) -> MagicMock:
    """LLM answering the attribute prompt with ``attribute_response`` verbatim."""
    duplicates = [0] if duplicate_facts is None else duplicate_facts

    async def generate_response(messages, **kwargs):
        if kwargs.get('prompt_name') == 'extract_edges.extract_attributes':
            return dict(attribute_response)
        return {'duplicate_facts': duplicates, 'contradicted_facts': []}

    llm = MagicMock()
    llm.generate_response = AsyncMock(side_effect=generate_response)
    return llm


@pytest.mark.parametrize('echoed', [None, '2024-04-01'])
async def test_attribute_llm_echo_never_overrides_a_non_schema_preserved_key(echoed):
    """Fails if a preserved key outside the schema takes the recomputed (echoed) value."""
    resolved, _, _ = await _resolve(
        _llm_with({'rating': 2, 'time_world_end': echoed}),
        _edge(PARAPHRASE),
        _stored(),
        schema=True,
        preserved_attributes=PRESERVED,
    )

    assert resolved.attributes['time_world_end'] == WORLD_END
    assert resolved.attributes['time_expiry'] == 'world_end'
    assert resolved.attributes['rating'] == 2


async def test_a_preserved_schema_field_missing_from_the_recompute_keeps_the_stored_value():
    """apply_capped_attributes uses replace mode: a field the LLM omits is simply absent from
    the recomputed attributes, so the stored value must be restored."""
    stored = _stored(attributes={'rating': 1, 'scratch': 1})

    resolved, _, _ = await _resolve(
        _llm_with({}),
        _edge(PARAPHRASE),
        stored,
        schema=True,
        preserved_attributes=frozenset({'rating'}),
    )

    assert resolved.attributes == {'rating': 1}


async def test_a_new_edge_does_not_inherit_preserved_keys():
    """No duplicate: the extracted edge is resolved, upstream clear applies, nothing restored."""
    new = _edge(PARAPHRASE, attributes={'time_expiry': 'world_end', 'scratch': 1})

    resolved, _, _ = await _resolve(
        _llm_with({}, duplicate_facts=[]),
        new,
        _stored(),
        preserved_attributes=frozenset({'time_expiry'}),
    )

    assert resolved is new
    assert resolved.attributes == {}


@pytest.mark.parametrize('preserved', [frozenset(), PRESERVED])
async def test_a_stored_edge_with_none_attributes_does_not_crash(preserved):
    stored = _stored()
    object.__setattr__(stored, 'attributes', None)
    assert stored.attributes is None

    resolved, _, _ = await _resolve(
        _llm(), _edge(PARAPHRASE), stored, preserved_attributes=preserved
    )

    assert resolved is stored
    assert resolved.attributes == {}


# --- review fixes: strict call-site guard ---------------------------------------------------


def _passes_self_preserved(call: ast.Call) -> bool:
    """True when a call spreads ``preserved_attributes_kwargs(self.preserved_edge_attributes)``."""
    for keyword in call.keywords:
        value = keyword.value
        if not (
            keyword.arg is None
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == 'preserved_attributes_kwargs'
        ):
            continue
        if len(value.args) != 1 or value.keywords:
            continue
        arg = value.args[0]
        if (
            isinstance(arg, ast.Attribute)
            and arg.attr == 'preserved_edge_attributes'
            and isinstance(arg.value, ast.Name)
            and arg.value.id == 'self'
        ):
            return True
    return False


def test_every_graphiti_resolver_call_passes_exactly_the_instance_option():
    tree = ast.parse(Path(graphiti_module.__file__).read_text(encoding='utf-8'))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node.func) in _RESOLVER_CALLS
    ]

    assert len(calls) >= 4
    assert [call.lineno for call in calls if not _passes_self_preserved(call)] == []


@pytest.mark.parametrize(
    'source',
    [
        'f(a, **preserved_attributes_kwargs(frozenset()))',
        'f(a, **preserved_attributes_kwargs(self.x))',
        'f(a, **preserved_attributes_kwargs(other.preserved_edge_attributes))',
        'f(a, **preserved_attributes_kwargs())',
        'f(a, **edge_expiry_kwargs(self.preserved_edge_attributes))',
        'f(a, preserved_attributes=self.preserved_edge_attributes)',
    ],
)
def test_the_strict_guard_rejects_wrong_arguments(source):
    call = ast.parse(source).body[0].value  # type: ignore[attr-defined]

    assert not _passes_self_preserved(call)


def test_the_strict_guard_accepts_the_instance_option():
    source = 'f(a, **preserved_attributes_kwargs(self.preserved_edge_attributes))'
    call = ast.parse(source).body[0].value  # type: ignore[attr-defined]

    assert _passes_self_preserved(call)


# --- review fixes: constructor ---------------------------------------------------------------


def test_graphiti_accepts_a_generator_of_names():
    graphiti = _make_graphiti(preserved_edge_attributes=(name for name in ['a', 'b']))

    assert graphiti.preserved_edge_attributes == frozenset({'a', 'b'})


@pytest.mark.parametrize('value', ['  ', ['  '], ['a', ' ']])
def test_graphiti_rejects_whitespace_only_names(value):
    with pytest.raises(TypeError):
        _make_graphiti(preserved_edge_attributes=value)


@pytest.mark.parametrize('value', ['abc', [1]])
def test_invalid_preserved_attributes_fail_before_any_driver_is_created(monkeypatch, value):
    driver_class = Mock()
    monkeypatch.setattr(graphiti_module, 'Neo4jDriver', driver_class)

    with pytest.raises(TypeError):
        Graphiti(
            uri='bolt://x',
            user='u',
            password='p',
            llm_client=Mock(spec=LLMClient),
            embedder=Mock(spec=EmbedderClient),
            cross_encoder=Mock(spec=CrossEncoderClient),
            preserved_edge_attributes=value,
        )

    driver_class.assert_not_called()


# --- review fixes: cross-episode bulk --------------------------------------------------------


@pytest.mark.parametrize('paraphrase_first', [True, False])
async def test_bulk_episodes_keep_the_preserved_keys_on_every_copy(monkeypatch, paraphrase_first):
    """add_episode_bulk: one episode paraphrases the stored edge, another states it exactly.
    Real per-episode resolution and cross-episode reconcile; both copies keep the stamp."""

    async def fake_search(clients, query, group_ids, config, search_filter):
        return SearchResults(edges=[_stored()])

    monkeypatch.setattr(edge_ops, 'search', fake_search)
    monkeypatch.setattr(
        graphiti_module, 'resolve_extracted_nodes', AsyncMock(return_value=([], {}))
    )
    monkeypatch.setattr(
        graphiti_module, 'extract_attributes_from_nodes', AsyncMock(return_value=NODES)
    )
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=_llm(), embedder=MagicMock(), cross_encoder=MagicMock()
    )
    episodes = [_episode(1), _episode(2)]
    first, second = _edge(PARAPHRASE), _edge(STORED_FACT)
    if not paraphrase_first:
        first, second = second, first

    graphiti = object.__new__(Graphiti)
    graphiti.clients = clients  # type: ignore[assignment]
    graphiti.edge_expiry_hook = None
    graphiti.identity_gate_hook = None
    graphiti.candidate_filter_hook = None
    graphiti.node_pre_resolution_hook = None
    graphiti.preserved_edge_attributes = PRESERVED

    _, resolved, invalidated, _ = await graphiti._resolve_nodes_and_edges_bulk(
        {episode.uuid: [] for episode in episodes},
        {'episode_1': [first], 'episode_2': [second]},
        [(episode, []) for episode in episodes],
        None,
        {},
        {},
        episodes,
    )

    copies = [edge for edge in resolved + invalidated if edge.uuid == STORED_UUID]
    assert len(copies) == 2
    assert [edge.attributes for edge in copies] == [KEPT, KEPT]
