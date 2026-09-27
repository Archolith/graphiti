"""Focused no-DB tests for the fork-native candidate-filter hook (Phase D3).

Covers:
- absent-hook compatibility and exact legacy delegate shapes at every call path
- search candidates and existing_nodes_override both filtered
- merge/dedupe occurs before filtering (hook sees only unique merged candidates)
- order preservation and exactly-once invocation per unique candidate
- INCLUDE/EXCLUDE semantics; same candidate evaluated once per extracted node
- all-excluded no-candidate behavior with no dedupe LLM and no identity gate
- filter applies before deterministic exact/fuzzy handling and before LLM indexing
- strict invalid-return TypeError and hook exception propagation; no cross-call leakage
- Graphiti class-level __new__ default and real constructor storage
- call-site wiring across single add_episode, bulk (extract/dedupe + resolve),
  add_triplet, and direct resolver paths
- composition with the D2 identity gate and request-local edge evidence
"""

import ast
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import graphiti_core.graphiti as graphiti_module
import graphiti_core.utils.bulk_utils as bulk_utils
from graphiti_core.candidate_filter import (
    CandidateFilterContext,
    CandidateFilterDecision,
    CandidateFilterHook,
)
from graphiti_core.cross_encoder import CrossEncoderClient
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder import EmbedderClient
from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError
from graphiti_core.graphiti import Graphiti
from graphiti_core.identity_gate import IdentityGateContext, IdentityGateDecision
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.utils.bulk_utils import dedupe_nodes_bulk
from graphiti_core.utils.maintenance.node_operations import resolve_extracted_nodes

GROUP_ID = 'filter_test_group'
NOW = datetime(2026, 9, 14)


def _make_clients():
    driver = MagicMock()
    embedder = MagicMock()
    cross_encoder = MagicMock()
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock()
    return (
        GraphitiClientsStub(driver, embedder, cross_encoder, llm_client),
        llm_client.generate_response,
    )


def GraphitiClientsStub(driver, embedder, cross_encoder, llm_client):
    from graphiti_core.graphiti_types import GraphitiClients

    return GraphitiClients.model_construct(
        driver=driver,
        embedder=embedder,
        cross_encoder=cross_encoder,
        llm_client=llm_client,
    )


def _make_episode(name: str = 'test_episode') -> EpisodicNode:
    return EpisodicNode(
        name=name,
        group_id=GROUP_ID,
        labels=[],
        source=EpisodeType.message,
        source_description='test',
        content='Alice likes Bob',
        created_at=NOW,
        valid_at=NOW,
        entity_edges=[],
    )


def _make_node(name: str) -> EntityNode:
    return EntityNode(
        name=name,
        group_id=GROUP_ID,
        labels=['Entity'],
        created_at=NOW,
        summary='',
    )


def _make_edge(source: EntityNode, target: EntityNode, fact: str) -> EntityEdge:
    return EntityEdge(
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        group_id=GROUP_ID,
        name='LIKES',
        fact=fact,
        episodes=[],
        created_at=NOW,
    )


def _make_full_graphiti(candidate_filter_hook=None, identity_gate_hook=None) -> Graphiti:
    """Build a fully-constructed Graphiti without touching DB/network defaults."""
    return Graphiti(
        graph_driver=Mock(spec=GraphDriver),
        llm_client=Mock(spec=LLMClient),
        embedder=Mock(spec=EmbedderClient),
        cross_encoder=Mock(spec=CrossEncoderClient),
        identity_gate_hook=identity_gate_hook,
        candidate_filter_hook=candidate_filter_hook,
    )


class RecordingFilterHook:
    """Deterministic candidate-filter hook that records invocation contexts.

    ``decisions`` maps candidate name -> CandidateFilterDecision; unlisted
    candidates get ``default``.
    """

    def __init__(self, default=CandidateFilterDecision.INCLUDE, decisions=None):
        self.default = default
        self.decisions = decisions or {}
        self.calls: list[CandidateFilterContext] = []

    async def filter_candidate(self, context: CandidateFilterContext) -> CandidateFilterDecision:
        self.calls.append(context)
        return self.decisions.get(context.candidate_node.name, self.default)


class RecordingIdentityGate:
    """Recording identity-gate hook stand-in (ALLOW always, unless veto_names match)."""

    def __init__(self, veto_names=()):
        self.veto_names = veto_names
        self.calls: list[IdentityGateContext] = []

    async def evaluate_identity_gate(self, context: IdentityGateContext) -> IdentityGateDecision:
        self.calls.append(context)
        if context.candidate_node.name in self.veto_names:
            return IdentityGateDecision.VETO
        return IdentityGateDecision.ALLOW


def _semantic_candidates(candidate_groups):
    async def fake_search(*_, **__):
        return candidate_groups

    return fake_search


async def _resolve_with_llm_merge(monkeypatch, llm_generate, extracted='Joe', candidate='Joseph'):
    """Stub semantic search so one candidate exists; LLM proposes a merge for it."""
    candidate_node = _make_node(candidate)
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_node]]),
    )
    llm_generate.return_value = {
        'entity_resolutions': [{'id': 0, 'name': extracted, 'duplicate_candidate_id': 0}]
    }
    return candidate_node


# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------


def test_filter_hook_is_runtime_checkable_protocol():
    assert isinstance(RecordingFilterHook(), CandidateFilterHook)


def test_frozen_context_rejects_reassignment():
    context = CandidateFilterContext(
        extracted_node=_make_node('Joe'),
        candidate_node=_make_node('Joseph'),
    )
    with pytest.raises(AttributeError):
        cast(Any, context).extracted_node = _make_node('Bob')


# ---------------------------------------------------------------------------
# Resolver behavior (direct resolve_extracted_nodes path)
# ---------------------------------------------------------------------------


async def test_no_hook_keeps_ordinary_merge(monkeypatch):
    clients, llm_generate = _make_clients()
    candidate_node = await _resolve_with_llm_merge(monkeypatch, llm_generate)

    extracted = _make_node('Joe')
    resolved, uuid_map, duplicates = await resolve_extracted_nodes(clients, [extracted])

    assert [node.name for node in resolved] == ['Joseph']
    assert uuid_map[extracted.uuid] == candidate_node.uuid
    assert duplicates == [(extracted, candidate_node)]


async def test_exclude_search_candidate(monkeypatch):
    clients, llm_generate = _make_clients()
    candidate_node = await _resolve_with_llm_merge(monkeypatch, llm_generate)

    extracted = _make_node('Joe')
    hook = RecordingFilterHook(decisions={'Joseph': CandidateFilterDecision.EXCLUDE})
    resolved, uuid_map, duplicates = await resolve_extracted_nodes(
        clients, [extracted], candidate_filter_hook=hook
    )

    # EXCLUDE removes the candidate: ordinary no-candidate behavior keeps the node new,
    # and the dedupe LLM is never consulted.
    assert [node.uuid for node in resolved] == [extracted.uuid]
    assert uuid_map[extracted.uuid] == extracted.uuid
    assert duplicates == []
    llm_generate.assert_not_called()
    assert [call.candidate_node for call in hook.calls] == [candidate_node]
    assert [call.extracted_node for call in hook.calls] == [extracted]


async def test_include_preserves_candidate(monkeypatch):
    clients, llm_generate = _make_clients()
    candidate_node = await _resolve_with_llm_merge(monkeypatch, llm_generate)

    extracted = _make_node('Joe')
    hook = RecordingFilterHook(default=CandidateFilterDecision.INCLUDE)
    resolved, uuid_map, _ = await resolve_extracted_nodes(
        clients, [extracted], candidate_filter_hook=hook
    )

    assert [node.uuid for node in resolved] == [candidate_node.uuid]
    assert uuid_map[extracted.uuid] == candidate_node.uuid


async def test_exclude_existing_nodes_override_candidate(monkeypatch):
    clients, llm_generate = _make_clients()
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[]]),
    )
    llm_generate.return_value = {
        'entity_resolutions': [{'id': 0, 'name': 'Joe', 'duplicate_candidate_id': 0}]
    }

    extracted = _make_node('Joe')
    override = _make_node('Joseph')
    hook = RecordingFilterHook(decisions={'Joseph': CandidateFilterDecision.EXCLUDE})
    resolved, uuid_map, _ = await resolve_extracted_nodes(
        clients,
        [extracted],
        existing_nodes_override=[override],
        candidate_filter_hook=hook,
    )

    assert [node.uuid for node in resolved] == [extracted.uuid]
    assert uuid_map[extracted.uuid] == extracted.uuid
    llm_generate.assert_not_called()


async def test_merge_dedupe_occurs_before_filtering(monkeypatch):
    """A candidate duplicated across search results and override reaches the hook once."""
    clients, llm_generate = _make_clients()
    candidate_node = await _resolve_with_llm_merge(monkeypatch, llm_generate)

    extracted = _make_node('Joe')
    hook = RecordingFilterHook()
    await resolve_extracted_nodes(
        clients,
        [extracted],
        existing_nodes_override=[candidate_node],
        candidate_filter_hook=hook,
    )

    # same uuid in search results and override: merged/deduped BEFORE the hook
    assert len(hook.calls) == 1
    assert hook.calls[0].candidate_node is candidate_node


async def test_order_preserved_and_exactly_once_per_candidate(monkeypatch):
    clients, llm_generate = _make_clients()
    candidates = [_make_node('Joseph'), _make_node('Joe Jr'), _make_node('Josephine')]
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([candidates]),
    )
    llm_generate.return_value = {
        'entity_resolutions': [{'id': 0, 'name': 'Joe', 'duplicate_candidate_id': 0}]
    }

    extracted = _make_node('Joe')
    hook = RecordingFilterHook(decisions={'Joe Jr': CandidateFilterDecision.EXCLUDE})
    resolved, _, _ = await resolve_extracted_nodes(clients, [extracted], candidate_filter_hook=hook)

    # exactly one call per unique candidate, in candidate order
    assert [call.candidate_node for call in hook.calls] == candidates
    # excluded middle candidate removed; surviving order preserved into the
    # dedupe LLM candidate index (Joseph indexed before Josephine)
    assert llm_generate.call_count == 1
    rendered = str(llm_generate.call_args.args[0])
    assert rendered.index('Joseph') < rendered.index('Josephine')
    assert resolved[0].name == 'Joseph'


async def test_filter_applies_before_deterministic_exact_resolution(monkeypatch):
    """An exact-name candidate would merge deterministically; EXCLUDE must prevent that."""
    clients, llm_generate = _make_clients()
    candidate_node = _make_node('Joe')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_node]]),
    )

    extracted = _make_node('Joe')
    hook = RecordingFilterHook(decisions={'Joe': CandidateFilterDecision.EXCLUDE})
    resolved, uuid_map, duplicates = await resolve_extracted_nodes(
        clients, [extracted], candidate_filter_hook=hook
    )

    assert [node.uuid for node in resolved] == [extracted.uuid]
    assert uuid_map[extracted.uuid] == extracted.uuid
    assert duplicates == []
    llm_generate.assert_not_called()


async def test_included_exact_candidate_resolves_deterministically_without_llm(monkeypatch):
    clients, llm_generate = _make_clients()
    candidate_node = _make_node('Joe')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_node]]),
    )

    extracted = _make_node('Joe')
    hook = RecordingFilterHook()
    resolved, uuid_map, duplicates = await resolve_extracted_nodes(
        clients, [extracted], candidate_filter_hook=hook
    )

    assert [node.uuid for node in resolved] == [candidate_node.uuid]
    assert uuid_map[extracted.uuid] == candidate_node.uuid
    assert duplicates == [(extracted, candidate_node)]
    llm_generate.assert_not_called()


async def test_same_candidate_evaluated_once_per_extracted_node(monkeypatch):
    clients, llm_generate = _make_clients()
    candidate_node = await _resolve_with_llm_merge(monkeypatch, llm_generate)
    # one candidate list per extracted node, both pointing at the same candidate
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_node], [candidate_node]]),
    )

    extracted_a = _make_node('Joe')
    extracted_b = _make_node('Joseph')
    hook = RecordingFilterHook()
    await resolve_extracted_nodes(clients, [extracted_a, extracted_b], candidate_filter_hook=hook)

    # the shared candidate is evaluated exactly once per (extracted node, candidate) pair
    assert len(hook.calls) == 2
    assert [call.extracted_node for call in hook.calls] == [extracted_a, extracted_b]
    assert [call.candidate_node for call in hook.calls] == [candidate_node, candidate_node]


async def test_all_excluded_runs_no_llm_and_no_identity_gate(monkeypatch):
    clients, llm_generate = _make_clients()
    await _resolve_with_llm_merge(monkeypatch, llm_generate)

    extracted = _make_node('Joe')
    identity_gate = RecordingIdentityGate()
    hook = RecordingFilterHook(decisions={'Joseph': CandidateFilterDecision.EXCLUDE})
    resolved, _, _ = await resolve_extracted_nodes(
        clients,
        [extracted],
        identity_gate_hook=identity_gate,
        candidate_filter_hook=hook,
    )

    assert [node.uuid for node in resolved] == [extracted.uuid]
    llm_generate.assert_not_called()
    assert identity_gate.calls == []


async def test_invalid_return_raises_typeerror(monkeypatch):
    clients, _ = _make_clients()
    candidate_node = _make_node('Joseph')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_node]]),
    )

    class BadReturnHook:
        async def filter_candidate(self, context):
            return 'include'

    with pytest.raises(TypeError, match='CandidateFilterDecision'):
        await resolve_extracted_nodes(
            clients,
            [_make_node('Joe')],
            candidate_filter_hook=cast(CandidateFilterHook, BadReturnHook()),
        )


async def test_hook_exception_propagates(monkeypatch):
    clients, _ = _make_clients()
    candidate_node = _make_node('Joseph')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_node]]),
    )

    class ExplodingHook:
        async def filter_candidate(self, context):
            raise RuntimeError('hook failure')

    with pytest.raises(RuntimeError, match='hook failure'):
        await resolve_extracted_nodes(
            clients, [_make_node('Joe')], candidate_filter_hook=ExplodingHook()
        )


async def test_no_cross_call_leakage(monkeypatch):
    clients, llm_generate = _make_clients()
    extracted_a = _make_node('Joe')
    candidate_a = _make_node('Joseph')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_a]]),
    )
    llm_generate.return_value = {
        'entity_resolutions': [{'id': 0, 'name': 'Joe', 'duplicate_candidate_id': 0}]
    }

    hook = RecordingFilterHook()
    await resolve_extracted_nodes(clients, [extracted_a], candidate_filter_hook=hook)
    first_call_contexts = list(hook.calls)

    extracted_b = _make_node('Bob')
    candidate_b = _make_node('Bobby')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_b]]),
    )
    llm_generate.return_value = {
        'entity_resolutions': [{'id': 0, 'name': 'Bob', 'duplicate_candidate_id': 0}]
    }
    await resolve_extracted_nodes(clients, [extracted_b], candidate_filter_hook=hook)

    # call 2 only saw call 2's evidence: nothing leaked from call 1
    assert first_call_contexts == [
        CandidateFilterContext(extracted_node=extracted_a, candidate_node=candidate_a)
    ]
    assert hook.calls[-1] == CandidateFilterContext(
        extracted_node=extracted_b, candidate_node=candidate_b
    )
    assert len(hook.calls) == 2


# ---------------------------------------------------------------------------
# Graphiti instance wiring
# ---------------------------------------------------------------------------


def test_graphiti_constructor_stores_candidate_filter_hook():
    hook = RecordingFilterHook()
    graphiti = _make_full_graphiti(candidate_filter_hook=hook)
    assert graphiti.candidate_filter_hook is hook

    default_graphiti = _make_full_graphiti()
    assert default_graphiti.candidate_filter_hook is None
    assert default_graphiti.identity_gate_hook is None


def test_graphiti_bypassing_init_defaults_to_no_hook():
    bare = Graphiti.__new__(Graphiti)
    assert bare.candidate_filter_hook is None


async def _run_add_episode_wiring(monkeypatch, graphiti: Graphiti) -> dict:
    """Run public add_episode with all extraction/resolution stubbed; capture resolver kwargs."""
    node_a, node_b = _make_node('Alice'), _make_node('Bob')
    edge = _make_edge(node_a, node_b, 'Alice likes Bob')
    precomputed_edges = [edge]
    episode = _make_episode()
    captured: dict = {}

    async def fake_combined(clients, ep, previous_episodes, **kwargs):
        return [node_a, node_b], precomputed_edges, {node_a.uuid: [0], node_b.uuid: [0]}

    async def fake_resolve_nodes(
        clients, extracted_nodes, ep, previous_episodes, entity_types, **kwargs
    ):
        captured['kwargs'] = kwargs
        return [node_a, node_b], {}, []

    async def fake_resolve_edges(clients, edges, ep, nodes, edge_types, edge_type_map):
        return edges, [], list(edges)

    async def fake_extract_attributes(clients, nodes, ep, previous_episodes, entity_types, edges):
        return nodes

    async def fake_process_episode_data(
        episode,
        nodes,
        entity_edges,
        now,
        group_id,
        saga=None,
        saga_previous_episode_uuid=None,
        node_episode_index_map=None,
        clients=None,
    ):
        return [], episode

    monkeypatch.setattr(graphiti_module, 'extract_nodes_and_edges', fake_combined)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_nodes', fake_resolve_nodes)
    monkeypatch.setattr(graphiti_module, 'resolve_edge_pointers', lambda edges, uuid_map: edges)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edges', fake_resolve_edges)
    monkeypatch.setattr(graphiti_module, 'extract_attributes_from_nodes', fake_extract_attributes)
    monkeypatch.setattr(graphiti_module, 'get_default_group_id', lambda provider: GROUP_ID)

    graphiti.driver._database = GROUP_ID
    monkeypatch.setattr(graphiti, 'retrieve_episodes', AsyncMock(return_value=[]))
    monkeypatch.setattr(graphiti, '_process_episode_data', fake_process_episode_data)

    span = Mock()
    span_cm = Mock()
    span_cm.__enter__ = Mock(return_value=span)
    span_cm.__exit__ = Mock(return_value=False)
    tracer = Mock()
    tracer.start_span.return_value = span_cm
    monkeypatch.setattr(graphiti, 'tracer', tracer, raising=False)

    await graphiti.add_episode(
        name=episode.name,
        episode_body=episode.content,
        source_description='test',
        reference_time=NOW,
        group_id=GROUP_ID,
    )
    captured['precomputed_edges'] = precomputed_edges
    return captured


async def test_add_episode_wiring_passes_candidate_filter_hook(monkeypatch):
    graphiti = _make_full_graphiti(candidate_filter_hook=RecordingFilterHook())
    captured = await _run_add_episode_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('candidate_filter_hook') is graphiti.candidate_filter_hook
    # no identity hook configured: identity kwargs stay absent even though the
    # candidate filter is forwarded
    assert 'identity_gate_hook' not in captured['kwargs']
    assert 'identity_gate_edges' not in captured['kwargs']


async def test_add_episode_no_hook_uses_legacy_resolver_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_add_episode_wiring(monkeypatch, graphiti)

    assert 'candidate_filter_hook' not in captured['kwargs']
    assert 'identity_gate_hook' not in captured['kwargs']
    assert 'identity_gate_edges' not in captured['kwargs']


async def test_add_episode_composes_candidate_filter_with_identity_gate(monkeypatch):
    graphiti = _make_full_graphiti(
        candidate_filter_hook=RecordingFilterHook(),
        identity_gate_hook=RecordingIdentityGate(),
    )
    captured = await _run_add_episode_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('candidate_filter_hook') is graphiti.candidate_filter_hook
    assert captured['kwargs'].get('identity_gate_hook') is graphiti.identity_gate_hook
    assert captured['kwargs'].get('identity_gate_edges') is captured['precomputed_edges']


async def _run_add_triplet_wiring(monkeypatch, graphiti: Graphiti) -> list[dict]:
    """Run public add_triplet with persistence stubbed; capture every resolver call in order."""
    source = _make_node('Alice')
    target = _make_node('Bob')
    edge = _make_edge(source, target, 'Alice likes Bob')
    source.name_embedding = [0.1]
    target.name_embedding = [0.1]
    edge.fact_embedding = [0.1]
    captured: list[dict] = []

    async def fake_resolve_nodes(clients, extracted_nodes, *args, **kwargs):
        captured.append({'resolved_name': extracted_nodes[0].name, 'args': args, 'kwargs': kwargs})
        return [extracted_nodes[0]], {}, []

    async def fake_get_node_by_uuid(driver, node_uuid):
        raise NodeNotFoundError(node_uuid)

    async def fake_get_edge_by_uuid(driver, edge_uuid):
        raise EdgeNotFoundError(edge_uuid)

    async def fake_get_between_nodes(driver, source_uuid, target_uuid):
        return []

    async def fake_search(*args, **kwargs):
        result = Mock()
        result.edges = []
        return result

    async def fake_resolve_extracted_edge(*args, **kwargs):
        return edge, [], []

    async def fake_embeddings(*args, **kwargs):
        return None

    async def fake_add_bulk(*args, **kwargs):
        return None

    monkeypatch.setattr(graphiti_module.EntityNode, 'get_by_uuid', fake_get_node_by_uuid)
    monkeypatch.setattr(graphiti_module.EntityEdge, 'get_by_uuid', fake_get_edge_by_uuid)
    monkeypatch.setattr(graphiti_module.EntityEdge, 'get_between_nodes', fake_get_between_nodes)
    monkeypatch.setattr(graphiti_module, 'search', fake_search)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edge', fake_resolve_extracted_edge)
    monkeypatch.setattr(graphiti_module, 'create_entity_edge_embeddings', fake_embeddings)
    monkeypatch.setattr(graphiti_module, 'create_entity_node_embeddings', fake_embeddings)
    monkeypatch.setattr(graphiti_module, 'add_nodes_and_edges_bulk', fake_add_bulk)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_nodes', fake_resolve_nodes)

    await graphiti.add_triplet(source, edge, target)
    return captured


async def test_add_triplet_wiring_passes_candidate_filter_hook(monkeypatch):
    graphiti = _make_full_graphiti(candidate_filter_hook=RecordingFilterHook())
    captured = await _run_add_triplet_wiring(monkeypatch, graphiti)

    assert [call['resolved_name'] for call in captured] == ['Alice', 'Bob']
    for call in captured:
        assert call['args'] == ()
        assert call['kwargs'].get('candidate_filter_hook') is graphiti.candidate_filter_hook
        assert 'identity_gate_hook' not in call['kwargs']
        assert 'identity_gate_edges' not in call['kwargs']


async def test_add_triplet_no_hook_uses_legacy_resolver_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_add_triplet_wiring(monkeypatch, graphiti)

    for call in captured:
        assert call['args'] == ()
        assert 'candidate_filter_hook' not in call['kwargs']
        assert 'identity_gate_hook' not in call['kwargs']


async def _run_bulk_dedupe_wiring(monkeypatch, graphiti: Graphiti) -> dict:
    """Run _extract_and_dedupe_nodes_bulk with extraction/dedupe stubbed; capture kwargs."""
    episode = _make_episode()
    nodes = [_make_node('Alice')]
    edges = [_make_edge(nodes[0], _make_node('Bob'), 'Alice likes Bob')]
    captured: dict = {}

    async def fake_extract_bulk(*args, **kwargs):
        return [nodes], [edges]

    async def fake_dedupe_bulk(clients, extracted_nodes, episode_context, entity_types, **kwargs):
        captured['kwargs'] = kwargs
        return {episode.uuid: nodes}, {}

    monkeypatch.setattr(graphiti_module, 'extract_nodes_and_edges_bulk', fake_extract_bulk)
    monkeypatch.setattr(graphiti_module, 'dedupe_nodes_bulk', fake_dedupe_bulk)

    (
        _nodes_by_episode,
        _uuid_map,
        extracted_edges_bulk,
    ) = await graphiti._extract_and_dedupe_nodes_bulk(
        [(episode, [])],
        {('Entity', 'Entity'): []},
        None,
        None,
        None,
    )
    captured['extracted_edges_bulk'] = extracted_edges_bulk
    captured['episode_edges'] = edges
    return captured


async def test_extract_and_dedupe_nodes_bulk_wiring(monkeypatch):
    graphiti = _make_full_graphiti(candidate_filter_hook=RecordingFilterHook())
    captured = await _run_bulk_dedupe_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('candidate_filter_hook') is graphiti.candidate_filter_hook
    assert 'identity_gate_hook' not in captured['kwargs']
    assert 'extracted_edges' not in captured['kwargs']


async def test_extract_and_dedupe_nodes_bulk_no_hook_uses_legacy_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_bulk_dedupe_wiring(monkeypatch, graphiti)

    assert 'candidate_filter_hook' not in captured['kwargs']
    assert 'identity_gate_hook' not in captured['kwargs']
    assert 'extracted_edges' not in captured['kwargs']


async def test_extract_and_dedupe_nodes_bulk_composes_both_hooks(monkeypatch):
    graphiti = _make_full_graphiti(
        candidate_filter_hook=RecordingFilterHook(),
        identity_gate_hook=RecordingIdentityGate(),
    )
    captured = await _run_bulk_dedupe_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('candidate_filter_hook') is graphiti.candidate_filter_hook
    assert captured['kwargs'].get('identity_gate_hook') is graphiti.identity_gate_hook
    assert captured['kwargs'].get('extracted_edges') is captured['extracted_edges_bulk']


async def _run_bulk_resolve_wiring(monkeypatch, graphiti: Graphiti) -> list[dict]:
    """Run _resolve_nodes_and_edges_bulk with resolution stubbed; capture resolver kwargs."""
    episode = _make_episode()
    nodes = [_make_node('Alice')]
    edges = [_make_edge(nodes[0], _make_node('Bob'), 'Alice likes Bob')]
    captured: list[dict] = []

    async def fake_resolve_nodes(
        clients, extracted_nodes, ep, previous_episodes, entity_types, **kwargs
    ):
        captured.append(kwargs)
        return extracted_nodes, {}, []

    async def fake_extract_attributes(clients, nodes, ep, previous_episodes, entity_types):
        return nodes

    async def fake_resolve_edges(clients, edges, ep, nodes, edge_types, edge_type_map):
        return edges, [], list(edges)

    monkeypatch.setattr(graphiti_module, 'resolve_extracted_nodes', fake_resolve_nodes)
    monkeypatch.setattr(graphiti_module, 'extract_attributes_from_nodes', fake_extract_attributes)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edges', fake_resolve_edges)

    await graphiti._resolve_nodes_and_edges_bulk(
        {episode.uuid: nodes},
        {episode.uuid: edges},
        [(episode, [])],
        None,
        None,
        {('Entity', 'Entity'): []},
        [episode],
    )
    return captured


async def test_resolve_nodes_and_edges_bulk_wiring(monkeypatch):
    graphiti = _make_full_graphiti(candidate_filter_hook=RecordingFilterHook())
    captured = await _run_bulk_resolve_wiring(monkeypatch, graphiti)

    assert len(captured) == 1
    assert captured[0].get('candidate_filter_hook') is graphiti.candidate_filter_hook
    assert 'identity_gate_hook' not in captured[0]


async def test_resolve_nodes_and_edges_bulk_no_hook_uses_legacy_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_bulk_resolve_wiring(monkeypatch, graphiti)

    assert 'candidate_filter_hook' not in captured[0]
    assert 'identity_gate_hook' not in captured[0]
    assert 'identity_gate_edges' not in captured[0]


async def test_resolve_nodes_and_edges_bulk_composes_both_hooks(monkeypatch):
    graphiti = _make_full_graphiti(
        candidate_filter_hook=RecordingFilterHook(),
        identity_gate_hook=RecordingIdentityGate(),
    )
    captured = await _run_bulk_resolve_wiring(monkeypatch, graphiti)

    assert captured[0].get('candidate_filter_hook') is graphiti.candidate_filter_hook
    assert captured[0].get('identity_gate_hook') is graphiti.identity_gate_hook
    assert 'identity_gate_edges' in captured[0]


# ---------------------------------------------------------------------------
# dedupe_nodes_bulk pass-through
# ---------------------------------------------------------------------------


async def test_dedupe_nodes_bulk_passes_through_candidate_filter_hook(monkeypatch):
    episode = _make_episode()
    nodes = [_make_node('Alice')]
    captured: list[dict] = []

    async def fake_resolve(clients, extracted_nodes, ep, previous_episodes, entity_types, **kwargs):
        captured.append(kwargs)
        return extracted_nodes, {}, []

    monkeypatch.setattr(bulk_utils, 'resolve_extracted_nodes', fake_resolve)

    hook = RecordingFilterHook()
    await dedupe_nodes_bulk(
        _make_clients()[0], [nodes], [(episode, [])], candidate_filter_hook=hook
    )

    assert captured[0].get('candidate_filter_hook') is hook


async def test_dedupe_nodes_bulk_no_hook_uses_legacy_resolver_signature(monkeypatch):
    episode = _make_episode()
    nodes = [_make_node('Alice')]
    captured: list[dict] = []

    async def fake_resolve(clients, extracted_nodes, ep, previous_episodes, entity_types, **kwargs):
        captured.append(kwargs)
        return extracted_nodes, {}, []

    monkeypatch.setattr(bulk_utils, 'resolve_extracted_nodes', fake_resolve)

    await dedupe_nodes_bulk(_make_clients()[0], [nodes], [(episode, [])])

    assert 'candidate_filter_hook' not in captured[0]
    assert 'identity_gate_hook' not in captured[0]
    assert 'identity_gate_edges' not in captured[0]


# ---------------------------------------------------------------------------
# Structural policy check
# ---------------------------------------------------------------------------


def test_no_menhir_imports_in_candidate_filter_mechanism():
    """Structural check: candidate-filter code must not import any Menhir module."""
    repo_root = Path(__file__).resolve().parents[1]
    targets = [
        repo_root / 'graphiti_core' / 'candidate_filter.py',
        repo_root / 'graphiti_core' / 'identity_gate.py',
        repo_root / 'graphiti_core' / 'graphiti.py',
        repo_root / 'graphiti_core' / 'utils' / 'maintenance' / 'node_operations.py',
        repo_root / 'graphiti_core' / 'utils' / 'bulk_utils.py',
    ]
    for path in targets:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                module_names = [node.module] if node.module else []
            else:
                continue
            for module_name in module_names:
                assert module_name.split('.')[0].lower() != 'menhir', (
                    f'{path.name} imports Menhir module: {module_name}'
                )
