"""Focused no-DB tests for the fork-native identity-gate hook (Phase D2).

Covers:
- absent-hook compatibility (ordinary promotion behavior unchanged)
- allow/veto decisions for valid LLM-proposed merges
- exactly-once invocation, only for valid proposed merges (never for -1,
  invalid/duplicate/skipped ids, or deterministic resolution paths)
- context evidence carried through ordinary arguments (nodes, ids, episode,
  previous episodes, request-local edge evidence) with no cross-call leakage
- strict invalid-return handling (TypeError) and exception propagation
- Graphiti constructor/call-site wiring: single add_episode, bulk, add_triplet
"""

import ast
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import graphiti_core.graphiti as graphiti_module
import graphiti_core.utils.bulk_utils as bulk_utils
from graphiti_core.cross_encoder import CrossEncoderClient
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder import EmbedderClient
from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError
from graphiti_core.graphiti import Graphiti
from graphiti_core.identity_gate import (
    IdentityGateContext,
    IdentityGateDecision,
    IdentityGateHook,
)
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.utils.bulk_utils import dedupe_nodes_bulk
from graphiti_core.utils.maintenance.node_operations import resolve_extracted_nodes

GROUP_ID = 'gate_test_group'
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


def _make_full_graphiti(identity_gate_hook=None) -> Graphiti:
    """Build a fully-constructed Graphiti without touching DB/network defaults.

    Spec'd mocks satisfy GraphitiClients' runtime type validation; every
    required attribute (including ``single_episode_extraction_hook`` and
    ``identity_gate_hook``) is therefore initialized exactly as production
    does.
    """
    return Graphiti(
        graph_driver=Mock(spec=GraphDriver),
        llm_client=Mock(spec=LLMClient),
        embedder=Mock(spec=EmbedderClient),
        cross_encoder=Mock(spec=CrossEncoderClient),
        identity_gate_hook=identity_gate_hook,
    )


class RecordingHook:
    """Deterministic identity-gate hook that records invocation contexts."""

    def __init__(self, decision: IdentityGateDecision = IdentityGateDecision.ALLOW):
        self.decision = decision
        self.calls: list[IdentityGateContext] = []

    async def evaluate_identity_gate(self, context: IdentityGateContext) -> IdentityGateDecision:
        self.calls.append(context)
        return self.decision


def _semantic_candidates(candidate_groups):
    async def fake_search(*_, **__):
        return candidate_groups

    return fake_search


def _llm_resolution(relative_id: int, candidate_id: int, name: str = 'Joseph'):
    return {'id': relative_id, 'name': name, 'duplicate_candidate_id': candidate_id}


async def _resolve_with_merge(
    monkeypatch, hook=None, edges=None, extracted='Joe', candidate='Joseph'
):
    """Drive a full resolution where the LLM proposes a merge for one node.

    Returns ((resolved, uuid_map, duplicates), extracted_node, candidate_node, episode).
    """
    clients, llm_generate = _make_clients()
    llm_generate.return_value = {'entity_resolutions': [_llm_resolution(0, 0)]}

    candidate_node = _make_node(candidate)
    extracted_node = _make_node(extracted)
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_node]]),
    )

    episode = _make_episode()
    result = await resolve_extracted_nodes(
        clients,
        [extracted_node],
        episode=episode,
        previous_episodes=[],
        identity_gate_hook=hook,
        identity_gate_edges=edges,
    )
    return result, extracted_node, candidate_node, episode


async def test_no_hook_compatibility_preserves_promotion(monkeypatch):
    result, extracted, candidate, _ = await _resolve_with_merge(monkeypatch)
    resolved, uuid_map, duplicates = result

    assert resolved[0].uuid == candidate.uuid
    assert uuid_map[extracted.uuid] == candidate.uuid
    assert duplicates == [(extracted, candidate)]


async def test_hook_allow_preserves_promotion(monkeypatch):
    hook = RecordingHook(IdentityGateDecision.ALLOW)
    result, extracted, candidate, _ = await _resolve_with_merge(monkeypatch, hook=hook)
    resolved, uuid_map, duplicates = result

    assert resolved[0].uuid == candidate.uuid
    assert uuid_map[extracted.uuid] == candidate.uuid
    assert duplicates == [(extracted, candidate)]
    assert len(hook.calls) == 1


async def test_hook_veto_preserves_no_duplicate_behavior(monkeypatch):
    hook = RecordingHook(IdentityGateDecision.VETO)
    result, extracted, candidate, _ = await _resolve_with_merge(monkeypatch, hook=hook)
    resolved, uuid_map, duplicates = result

    assert resolved[0].uuid == extracted.uuid
    assert uuid_map[extracted.uuid] == extracted.uuid
    assert duplicates == []
    assert len(hook.calls) == 1


async def test_hook_invoked_exactly_once_per_merge(monkeypatch):
    clients, llm_generate = _make_clients()
    llm_generate.return_value = {
        'entity_resolutions': [
            _llm_resolution(0, 0, 'Joe'),
            # the Java node must reference the FLATTENED candidate index:
            # [Joseph, Java candidate 1, Java candidate 2] -> Java candidate 1 is id 1
            _llm_resolution(1, 1, 'Java'),
        ]
    }

    candidate_joe = _make_node('Joseph')
    java_candidate_1 = _make_node('Java')
    java_candidate_2 = _make_node('Java')
    extracted_nodes = [_make_node('Joe'), _make_node('Java')]
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate_joe], [java_candidate_1, java_candidate_2]]),
    )

    hook = RecordingHook(IdentityGateDecision.ALLOW)
    resolved, uuid_map, _ = await resolve_extracted_nodes(
        clients,
        extracted_nodes,
        episode=_make_episode(),
        previous_episodes=[],
        identity_gate_hook=hook,
    )

    assert len(hook.calls) == 2
    # first call: Joe -> Joseph (flattened candidate id 0)
    assert hook.calls[0].extracted_node is extracted_nodes[0]
    assert hook.calls[0].candidate_node is candidate_joe
    assert hook.calls[0].candidate_id == 0
    # second call: Java -> Java candidate 1 (flattened candidate id 1, not Joseph)
    assert hook.calls[1].extracted_node is extracted_nodes[1]
    assert hook.calls[1].candidate_node is java_candidate_1
    assert hook.calls[1].candidate_id == 1
    assert resolved[0].uuid == candidate_joe.uuid
    assert resolved[1].uuid == java_candidate_1.uuid
    assert uuid_map[extracted_nodes[0].uuid] == candidate_joe.uuid
    assert uuid_map[extracted_nodes[1].uuid] == java_candidate_1.uuid


async def test_hook_not_invoked_for_negative_or_invalid_or_skipped_resolutions(monkeypatch):
    clients, llm_generate = _make_clients()
    llm_generate.return_value = {
        'entity_resolutions': [
            _llm_resolution(0, -1, 'Joe'),  # negative decision
            _llm_resolution(1, 999, 'Java'),  # invalid candidate id
            _llm_resolution(5, 0, 'Bob'),  # out-of-range relative id -> skipped
        ]
    }

    extracted_nodes = [_make_node('Joe'), _make_node('Java'), _make_node('Bob')]
    candidates = [[_make_node('Joseph')], [_make_node('Jav')], [_make_node('Bobby')]]
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates(candidates),
    )

    hook = RecordingHook(IdentityGateDecision.ALLOW)
    await resolve_extracted_nodes(
        clients,
        extracted_nodes,
        episode=_make_episode(),
        previous_episodes=[],
        identity_gate_hook=hook,
    )

    assert hook.calls == []


async def test_hook_not_invoked_for_duplicate_relative_ids(monkeypatch):
    clients, llm_generate = _make_clients()
    llm_generate.return_value = {
        'entity_resolutions': [
            _llm_resolution(0, 0),
            _llm_resolution(0, -1),  # duplicate relative id -> second ignored
        ]
    }

    candidate = _make_node('Joseph')
    extracted = _make_node('Joe')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate]]),
    )

    hook = RecordingHook(IdentityGateDecision.ALLOW)
    await resolve_extracted_nodes(
        clients,
        [extracted],
        episode=_make_episode(),
        previous_episodes=[],
        identity_gate_hook=hook,
    )

    assert len(hook.calls) == 1


async def test_hook_not_invoked_for_deterministic_exact_match(monkeypatch):
    clients, llm_generate = _make_clients()

    candidate = _make_node('Joe Michaels')
    extracted = _make_node('Joe Michaels')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate]]),
    )

    hook = RecordingHook(IdentityGateDecision.ALLOW)
    await resolve_extracted_nodes(
        clients,
        [extracted],
        episode=_make_episode(),
        previous_episodes=[],
        identity_gate_hook=hook,
    )

    llm_generate.assert_not_awaited()
    assert hook.calls == []


async def test_hook_not_invoked_for_deterministic_fuzzy_match(monkeypatch):
    clients, llm_generate = _make_clients()

    candidate = _make_node('Joe-Michaels')
    extracted = _make_node('Joe Michaels')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate]]),
    )

    hook = RecordingHook(IdentityGateDecision.ALLOW)
    await resolve_extracted_nodes(
        clients,
        [extracted],
        episode=_make_episode(),
        previous_episodes=[],
        identity_gate_hook=hook,
    )

    llm_generate.assert_not_awaited()
    assert hook.calls == []


async def test_hook_not_invoked_for_no_candidates(monkeypatch):
    clients, llm_generate = _make_clients()

    extracted = _make_node('Completely New Thing')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[]]),
    )

    hook = RecordingHook(IdentityGateDecision.ALLOW)
    await resolve_extracted_nodes(
        clients,
        [extracted],
        episode=_make_episode(),
        previous_episodes=[],
        identity_gate_hook=hook,
    )

    llm_generate.assert_not_awaited()
    assert hook.calls == []


async def test_context_carries_full_evidence(monkeypatch):
    hook = RecordingHook(IdentityGateDecision.ALLOW)
    edge_a = _make_edge(_make_node('Alice'), _make_node('Bob'), 'Alice likes Bob')
    edge_b = _make_edge(_make_node('Bob'), _make_node('Carol'), 'Bob knows Carol')
    edges = [edge_a, edge_b]

    result, extracted, candidate, episode = await _resolve_with_merge(
        monkeypatch, hook=hook, edges=edges
    )

    call = hook.calls[0]
    assert isinstance(call, IdentityGateContext)
    assert call.extracted_node is extracted
    assert call.candidate_node is candidate
    assert call.candidate_id == 0
    assert call.episode is episode
    assert call.previous_episodes == []
    assert call.edges == edges


async def test_context_with_previous_episodes(monkeypatch):
    hook = RecordingHook(IdentityGateDecision.ALLOW)
    prev = [_make_episode('previous')]
    clients, llm_generate = _make_clients()
    llm_generate.return_value = {'entity_resolutions': [_llm_resolution(0, 0)]}

    candidate = _make_node('Joseph')
    extracted = _make_node('Joe')
    monkeypatch.setattr(
        'graphiti_core.utils.maintenance.node_operations._semantic_candidate_search',
        _semantic_candidates([[candidate]]),
    )

    await resolve_extracted_nodes(
        clients,
        [extracted],
        episode=_make_episode(),
        previous_episodes=prev,
        identity_gate_hook=hook,
    )

    assert hook.calls[0].previous_episodes == prev


async def test_no_cross_call_leakage(monkeypatch):
    hook = RecordingHook(IdentityGateDecision.ALLOW)
    edges_first = [_make_edge(_make_node('Alice'), _make_node('Bob'), 'first')]
    edges_second = [_make_edge(_make_node('Carol'), _make_node('Dave'), 'second')]

    await _resolve_with_merge(monkeypatch, hook=hook, edges=edges_first)
    await _resolve_with_merge(monkeypatch, hook=hook, edges=edges_second)

    assert len(hook.calls) == 2
    assert hook.calls[0].edges == edges_first
    assert hook.calls[1].edges == edges_second


async def test_invalid_hook_return_raises_typeerror(monkeypatch):
    class BadHook:
        async def evaluate_identity_gate(self, context):
            return 'allow'

    with pytest.raises(TypeError):
        await _resolve_with_merge(monkeypatch, hook=BadHook())


async def test_hook_exception_propagates(monkeypatch):
    class ExplodingHook:
        async def evaluate_identity_gate(self, context):
            raise RuntimeError('hook failure')

    with pytest.raises(RuntimeError, match='hook failure'):
        await _resolve_with_merge(monkeypatch, hook=ExplodingHook())


def test_identity_gate_hook_is_runtime_checkable_protocol():
    hook = RecordingHook()
    assert isinstance(hook, IdentityGateHook)


def test_frozen_context_rejects_reassignment():
    context = IdentityGateContext(
        extracted_node=_make_node('Joe'),
        candidate_node=_make_node('Joseph'),
        candidate_id=0,
        episode=None,
        previous_episodes=[],
        edges=[],
    )
    # cast-through-Any keeps the type checker clean; the frozen dataclass still raises
    with pytest.raises(AttributeError):
        cast(Any, context).candidate_id = 5


# ---------------------------------------------------------------------------
# Graphiti wiring
# ---------------------------------------------------------------------------


def test_graphiti_constructor_stores_identity_gate_hook():
    hook = RecordingHook()
    graphiti = _make_full_graphiti(hook)
    assert graphiti.identity_gate_hook is hook

    default_graphiti = _make_full_graphiti()
    assert default_graphiti.identity_gate_hook is None
    assert default_graphiti.single_episode_extraction_hook is None


def test_graphiti_bypassing_init_defaults_to_no_hook():
    """Regression: __new__-style doubles/subclasses must observe 'no hook configured'.

    Full-flow compatibility evidence for a __new__-prepared instance through public
    ``add_episode`` is supplied by the pre-existing
    ``tests/test_extraction_routing.py::test_add_episode_combined_route_wiring_end_to_end``,
    which properly prepares such a double; this suite deliberately does not duplicate
    that setup.
    """
    bare = Graphiti.__new__(Graphiti)
    assert bare.identity_gate_hook is None


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

    # configure a local Mock tracer fully before installing it, so the instance
    # attribute keeps its declared Tracer type while still exposing span assertions.
    # raising=False: __new__-style bare instances have no tracer attribute at all.
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
    # expose the actual objects the flow passed through, for identity assertions
    captured['precomputed_edges'] = precomputed_edges
    return captured


async def test_add_episode_wiring_passes_hook_and_precomputed_edges(monkeypatch):
    graphiti = _make_full_graphiti(RecordingHook())
    captured = await _run_add_episode_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('identity_gate_hook') is graphiti.identity_gate_hook
    # ordinary request-local pass-through: the exact list object the combined
    # extractor produced reaches resolution unchanged
    assert captured['kwargs'].get('identity_gate_edges') is captured['precomputed_edges']


async def test_add_episode_no_hook_uses_legacy_resolver_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_add_episode_wiring(monkeypatch, graphiti)

    assert 'identity_gate_hook' not in captured['kwargs']
    assert 'identity_gate_edges' not in captured['kwargs']


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
        captured.append(
            {
                'resolved_name': extracted_nodes[0].name,
                'args': args,
                'kwargs': kwargs,
            }
        )
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


async def test_add_triplet_wiring_passes_hook_without_episode_or_edges(monkeypatch):
    graphiti = _make_full_graphiti(RecordingHook())
    captured = await _run_add_triplet_wiring(monkeypatch, graphiti)

    # both source and target resolution must run, in order, through the configured hook
    assert [call['resolved_name'] for call in captured] == ['Alice', 'Bob']
    for call in captured:
        assert call['args'] == ()
        assert call['kwargs'].get('identity_gate_hook') is graphiti.identity_gate_hook
        assert 'identity_gate_edges' not in call['kwargs']


async def test_add_triplet_no_hook_uses_legacy_resolver_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_add_triplet_wiring(monkeypatch, graphiti)

    assert [call['resolved_name'] for call in captured] == ['Alice', 'Bob']
    for call in captured:
        assert call['args'] == ()
        assert 'identity_gate_hook' not in call['kwargs']
        assert 'identity_gate_edges' not in call['kwargs']


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
    # expose the actual objects the flow passed through, for identity assertions
    captured['extracted_edges_bulk'] = extracted_edges_bulk
    captured['episode_edges'] = edges
    return captured


async def test_extract_and_dedupe_nodes_bulk_wiring(monkeypatch):
    graphiti = _make_full_graphiti(RecordingHook())
    captured = await _run_bulk_dedupe_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('identity_gate_hook') is graphiti.identity_gate_hook
    # ordinary request-local pass-through: the exact episode edge list object the
    # bulk extractor produced is forwarded unchanged
    assert captured['kwargs'].get('extracted_edges') is captured['extracted_edges_bulk']
    assert captured['extracted_edges_bulk'][0] is captured['episode_edges']
    assert captured['extracted_edges_bulk'][0][0] is captured['episode_edges'][0]


async def test_extract_and_dedupe_nodes_bulk_no_hook_uses_legacy_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_bulk_dedupe_wiring(monkeypatch, graphiti)

    assert 'identity_gate_hook' not in captured['kwargs']
    assert 'extracted_edges' not in captured['kwargs']
    assert captured['extracted_edges_bulk'][0] is captured['episode_edges']
    assert captured['extracted_edges_bulk'][0][0] is captured['episode_edges'][0]


async def test_dedupe_nodes_bulk_passes_through_hook_and_edges(monkeypatch):
    episode = _make_episode()
    nodes = [_make_node('Alice')]
    edges = [_make_edge(nodes[0], _make_node('Bob'), 'Alice likes Bob')]
    captured: list[dict] = []

    async def fake_resolve(clients, extracted_nodes, ep, previous_episodes, entity_types, **kwargs):
        captured.append(kwargs)
        return extracted_nodes, {}, []

    monkeypatch.setattr(bulk_utils, 'resolve_extracted_nodes', fake_resolve)

    hook = RecordingHook()
    await dedupe_nodes_bulk(
        _make_clients()[0],
        [nodes],
        [(episode, [])],
        identity_gate_hook=hook,
        extracted_edges=[edges],
    )

    assert captured[0].get('identity_gate_hook') is hook
    assert captured[0].get('identity_gate_edges') == edges


async def test_dedupe_nodes_bulk_no_hook_uses_legacy_resolver_signature(monkeypatch):
    episode = _make_episode()
    nodes = [_make_node('Alice')]
    captured: list[dict] = []

    async def fake_resolve(clients, extracted_nodes, ep, previous_episodes, entity_types, **kwargs):
        captured.append(kwargs)
        return extracted_nodes, {}, []

    monkeypatch.setattr(bulk_utils, 'resolve_extracted_nodes', fake_resolve)

    await dedupe_nodes_bulk(_make_clients()[0], [nodes], [(episode, [])])

    # no hook configured: the legacy resolver shape is used with no identity kwargs at all
    assert 'identity_gate_hook' not in captured[0]
    assert 'identity_gate_edges' not in captured[0]


def test_no_menhir_imports_in_identity_gate_mechanism():
    """Structural check: identity-gate code must not import any Menhir module."""
    repo_root = Path(__file__).resolve().parents[1]
    targets = [
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
