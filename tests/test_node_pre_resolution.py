"""Focused no-DB tests for Phase D5: adaptive request bisection plus the
neutral node pre-resolution seam.

Covered:
- GraphitiRequestTooLargeError bisection (4 -> 2+2, recursive 4 -> 2 -> 1,
  singleton rethrow identity) with subset candidate isolation and preserved
  order
- search / candidate filtering / deterministic resolution run exactly once
- D2 identity-gate forwarding (hook + edge evidence) into every split batch
- node pre-resolution hook: DEFER/RESOLVE, strict result contract, exception
  propagation, withholding from search/filter/LLM, different-UUID duplicate
  bookkeeping
- Graphiti constructor/class-level hook storage and wiring across every
  resolver path (add_episode, bulk extract/dedupe + resolve, add_triplet,
  dedupe_nodes_bulk) with exact legacy shapes when no hook is configured
- no Menhir imports or policy strings in the mechanism
"""

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
from graphiti_core.errors import EdgeNotFoundError, GraphitiRequestTooLargeError, NodeNotFoundError
from graphiti_core.graphiti import Graphiti
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.identity_gate import IdentityGateContext, IdentityGateDecision
from graphiti_core.llm_client import LLMClient
from graphiti_core.node_pre_resolution import (
    NodePreResolutionContext,
    NodePreResolutionHook,
    PreResolutionDecision,
    PreResolutionResult,
)
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.utils.bulk_utils import dedupe_nodes_bulk
from graphiti_core.utils.maintenance import node_operations

GROUP_ID = 'd5_test_group'
NOW = datetime(2026, 9, 14)


def _make_clients():
    driver = MagicMock()
    embedder = MagicMock()
    cross_encoder = MagicMock()
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock()
    return (
        GraphitiClients.model_construct(
            driver=driver,
            embedder=embedder,
            cross_encoder=cross_encoder,
            llm_client=llm_client,
        ),
        llm_client.generate_response,
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


def _make_full_graphiti(**hooks) -> Graphiti:
    """Build a fully-constructed Graphiti without touching DB/network defaults."""
    return Graphiti(
        graph_driver=Mock(spec=GraphDriver),
        llm_client=Mock(spec=LLMClient),
        embedder=Mock(spec=EmbedderClient),
        cross_encoder=Mock(spec=CrossEncoderClient),
        **hooks,
    )


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


class RecordingCandidateFilter:
    """Recording candidate-filter hook stand-in (INCLUDE always)."""

    def __init__(self):
        self.calls: list[Any] = []

    async def filter_candidate(self, context) -> Any:
        from graphiti_core.candidate_filter import CandidateFilterDecision

        self.calls.append(context)
        return CandidateFilterDecision.INCLUDE


class RecordingPreResolution:
    """Pre-resolution hook returning configured results per extracted-node name.

    ``results`` maps node name -> PreResolutionResult. Unlisted names defer.
    """

    def __init__(self, results=None):
        self.results = results or {}
        self.calls: list[NodePreResolutionContext] = []

    async def pre_resolve_node(self, context: NodePreResolutionContext) -> PreResolutionResult:
        self.calls.append(context)
        return self.results.get(
            context.extracted_node.name,
            PreResolutionResult(decision=PreResolutionDecision.DEFER),
        )


class _BisectingLLM:
    """Replace node_operations._resolve_with_llm with a size-gated delegator.

    The real implementation still runs (so identity gating and promotion
    semantics are exercised); ``generate_response`` receives a full-resolution
    payload sized to the current subset. Every LLM attempt whose subset size
    satisfies ``raise_if`` raises a fresh GraphitiRequestTooLargeError instead.

    By default (``candidate_id=None``) each relative response id ``i`` selects
    merged candidate id ``i``, which is truthful for tests whose subsets have
    one candidate per extracted node. An explicit ``candidate_id`` override
    maps every relative id to that single merged candidate id.
    """

    def __init__(self, llm_generate, raise_if, error_name='too-large', candidate_id=None):
        self.llm_generate = llm_generate
        self.raise_if = raise_if
        self.error_name = error_name
        self.candidate_id = candidate_id
        self.calls: list[dict] = []
        self.errors: list[GraphitiRequestTooLargeError] = []
        self._real = node_operations._resolve_with_llm

    async def __call__(self, llm_client, extracted_nodes, indexes, state, *args, **kwargs):
        size = len(state.unresolved_indices)
        subset = tuple(state.unresolved_indices)
        self.calls.append(
            {
                'subset': subset,
                'size': size,
                'candidates': list(indexes.existing_nodes),
                'identity_gate_hook': kwargs.get('identity_gate_hook'),
                'identity_gate_edges': kwargs.get('identity_gate_edges'),
            }
        )
        if self.raise_if(size):
            error = GraphitiRequestTooLargeError(self.error_name)
            self.errors.append(error)
            raise error
        self.llm_generate.return_value = {
            'entity_resolutions': [
                {
                    'id': i,
                    'name': extracted_nodes[idx].name,
                    'duplicate_candidate_id': (
                        self.candidate_id if self.candidate_id is not None else i
                    ),
                }
                for i, idx in enumerate(subset)
            ]
        }
        await self._real(llm_client, extracted_nodes, indexes, state, *args, **kwargs)

    def install(self, monkeypatch):
        monkeypatch.setattr(node_operations, '_resolve_with_llm', self)


def _semantic_candidates(candidate_groups):
    async def fake_search(*_, **__):
        return candidate_groups

    return fake_search


# ---------------------------------------------------------------------------
# Bisection mechanics (direct resolve_extracted_nodes path)
# ---------------------------------------------------------------------------


async def test_no_error_single_llm_call(monkeypatch):
    clients, llm_generate = _make_clients()
    pools = [[_make_node('Joseph')], [_make_node('Joey1')]]
    monkeypatch.setattr(node_operations, '_semantic_candidate_search', _semantic_candidates(pools))
    bisector = _BisectingLLM(llm_generate, raise_if=lambda size: False)
    bisector.install(monkeypatch)

    extracted = [_make_node('Joe'), _make_node('Joey')]
    resolved, uuid_map, duplicates = await node_operations.resolve_extracted_nodes(
        clients, extracted
    )

    # one combined LLM request over the whole unresolved batch, no splits
    assert [call['size'] for call in bisector.calls] == [2]
    assert tuple(bisector.calls[0]['subset']) == (0, 1)
    assert bisector.calls[0]['candidates'] == pools[0] + pools[1]
    assert bisector.errors == []
    assert [node.uuid for node in resolved] == [pools[0][0].uuid, pools[1][0].uuid]
    assert uuid_map[extracted[0].uuid] == pools[0][0].uuid
    assert uuid_map[extracted[1].uuid] == pools[1][0].uuid


async def test_four_way_batch_fails_then_stable_two_plus_two(monkeypatch):
    clients, llm_generate = _make_clients()
    candidates_by_name = {
        'Joseph': _make_node('Joseph'),
        'Josepha': _make_node('Josepha'),
        'Joey1': _make_node('Joey1'),
        'Joey2': _make_node('Joey2'),
    }
    pools = [
        [candidates_by_name['Joseph']],
        [candidates_by_name['Josepha']],
        [candidates_by_name['Joey1']],
        [candidates_by_name['Joey2']],
    ]
    monkeypatch.setattr(node_operations, '_semantic_candidate_search', _semantic_candidates(pools))
    bisector = _BisectingLLM(llm_generate, raise_if=lambda size: size > 2)
    bisector.install(monkeypatch)

    extracted = [_make_node('Joe'), _make_node('Jo'), _make_node('Joey'), _make_node('JoeH')]
    resolved, uuid_map, duplicates = await node_operations.resolve_extracted_nodes(
        clients, extracted
    )

    # initial size-4 request fails; two stable size-2 retries succeed
    assert [call['size'] for call in bisector.calls] == [4, 2, 2]
    assert [tuple(call['subset']) for call in bisector.calls] == [
        (0, 1, 2, 3),
        (0, 1),
        (2, 3),
    ]
    assert len(bisector.errors) == 1
    for i, extracted_node in enumerate(extracted):
        assert uuid_map[extracted_node.uuid] == pools[i][0].uuid


async def test_recursive_split_down_to_singletons(monkeypatch):
    clients, llm_generate = _make_clients()
    candidates_by_name = {f'cand{i}': _make_node(f'cand{i}') for i in range(4)}
    pools = [[candidates_by_name[f'cand{i}']] for i in range(4)]
    monkeypatch.setattr(node_operations, '_semantic_candidate_search', _semantic_candidates(pools))
    bisector = _BisectingLLM(llm_generate, raise_if=lambda size: size > 1)
    bisector.install(monkeypatch)

    extracted = [_make_node('Joe'), _make_node('Jo'), _make_node('Joey'), _make_node('JoeH')]
    resolved, uuid_map, _ = await node_operations.resolve_extracted_nodes(clients, extracted)

    # 4 fails -> [0,1] fails -> 0, 1; then [2,3] fails -> 2, 3
    assert [tuple(call['subset']) for call in bisector.calls] == [
        (0, 1, 2, 3),
        (0, 1),
        (0,),
        (1,),
        (2, 3),
        (2,),
        (3,),
    ]
    for i, extracted_node in enumerate(extracted):
        assert uuid_map[extracted_node.uuid] == pools[i][0].uuid
    assert [node.uuid for node in resolved] == [pool[0].uuid for pool in pools]


async def test_singleton_rethrow_preserves_exception_identity(monkeypatch):
    clients, llm_generate = _make_clients()
    candidate = _make_node('Joseph')
    monkeypatch.setattr(
        node_operations, '_semantic_candidate_search', _semantic_candidates([[candidate]])
    )
    sentinel = GraphitiRequestTooLargeError('singleton-only')

    async def always_too_large(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr(node_operations, '_resolve_with_llm', always_too_large)

    extracted = [_make_node('Joe')]
    with pytest.raises(GraphitiRequestTooLargeError) as exc_info:
        await node_operations.resolve_extracted_nodes(clients, extracted)

    assert exc_info.value is sentinel


async def test_split_batches_use_subset_candidate_pools_in_order(monkeypatch):
    clients, llm_generate = _make_clients()
    candidates_by_name = {f'cand{i}': _make_node(f'cand{i}') for i in range(4)}
    pools = [[candidates_by_name[f'cand{i}'], _make_node(f'extra{i}')] for i in range(4)]
    monkeypatch.setattr(node_operations, '_semantic_candidate_search', _semantic_candidates(pools))
    bisector = _BisectingLLM(llm_generate, raise_if=lambda size: size > 1)
    bisector.install(monkeypatch)

    extracted = [_make_node('Joe'), _make_node('Jo'), _make_node('Joey'), _make_node('JoeH')]
    await node_operations.resolve_extracted_nodes(clients, extracted)

    # initial call merges all subset pools in order; retries carry only their
    # own subset's candidates, preserving pool order
    initial = bisector.calls[0]
    assert initial['candidates'] == [c for pool in pools for c in pool]
    singleton_calls = {tuple(call['subset']): call for call in bisector.calls if call['size'] == 1}
    for i in range(4):
        assert singleton_calls[(i,)]['candidates'] == pools[i]
    pair_calls = {tuple(call['subset']): call for call in bisector.calls if call['size'] == 2}
    assert pair_calls[(0, 1)]['candidates'] == pools[0] + pools[1]
    assert pair_calls[(2, 3)]['candidates'] == pools[2] + pools[3]


async def test_search_filter_deterministic_run_exactly_once_across_splits(monkeypatch):
    clients, llm_generate = _make_clients()
    candidate_exact = _make_node('Joe')  # exact-name match resolves deterministically
    candidate_llm = _make_node('Joseph')
    monkeypatch.setattr(
        node_operations,
        '_semantic_candidate_search',
        _semantic_candidates([[candidate_exact, candidate_llm]] * 2),
    )
    search_calls: list[int] = []
    real_search = node_operations._semantic_candidate_search

    async def counting_search(*args, **kwargs):
        search_calls.append(1)
        return await real_search(*args, **kwargs)

    monkeypatch.setattr(node_operations, '_semantic_candidate_search', counting_search)
    candidate_filter = RecordingCandidateFilter()
    bisector = _BisectingLLM(llm_generate, raise_if=lambda size: size > 1, candidate_id=1)
    bisector.install(monkeypatch)

    extracted = [_make_node('Joe'), _make_node('Jo')]
    resolved, uuid_map, duplicates = await node_operations.resolve_extracted_nodes(
        clients, extracted, candidate_filter_hook=cast(Any, candidate_filter)
    )

    # semantic search issued exactly once for the whole batch, even though the
    # LLM stage bisected down to singletons
    assert len(search_calls) == 1
    # candidate filter ran exactly once per unique merged candidate per node
    # (before any splitting) and never again
    assert len(candidate_filter.calls) == 4
    # deterministic exact match resolved node 0 without LLM involvement;
    # node 1 went through the (split) LLM path to candidate_llm
    assert uuid_map[extracted[0].uuid] == candidate_exact.uuid
    assert uuid_map[extracted[1].uuid] == candidate_llm.uuid
    assert duplicates == [(extracted[0], candidate_exact), (extracted[1], candidate_llm)]
    assert resolved[0] is candidate_exact and resolved[1] is candidate_llm


async def test_identity_gate_forwarded_to_every_split_batch(monkeypatch):
    clients, llm_generate = _make_clients()
    candidates_by_name = {f'cand{i}': _make_node(f'cand{i}') for i in range(4)}
    pools = [[candidates_by_name[f'cand{i}']] for i in range(4)]
    monkeypatch.setattr(node_operations, '_semantic_candidate_search', _semantic_candidates(pools))
    identity_gate = RecordingIdentityGate()
    edges = [_make_edge(_make_node('Alice'), _make_node('Bob'), 'Alice likes Bob')]
    bisector = _BisectingLLM(llm_generate, raise_if=lambda size: size > 1)
    bisector.install(monkeypatch)

    extracted = [_make_node('Joe'), _make_node('Jo'), _make_node('Joey'), _make_node('JoeH')]
    resolved, uuid_map, duplicates = await node_operations.resolve_extracted_nodes(
        clients,
        extracted,
        identity_gate_hook=cast(Any, identity_gate),
        identity_gate_edges=edges,
    )

    # every LLM attempt — initial and split — received the same hook and edge
    # evidence unchanged
    assert bisector.calls, 'expected LLM attempts'
    for call in bisector.calls:
        assert call['identity_gate_hook'] is identity_gate
        assert call['identity_gate_edges'] is edges
    # the gate itself was consulted once per LLM-proposed merge, across splits
    assert len(identity_gate.calls) == 4
    for i, call in enumerate(identity_gate.calls):
        assert call.extracted_node is extracted[i]
        assert call.edges is edges
    for i, extracted_node in enumerate(extracted):
        assert uuid_map[extracted_node.uuid] == pools[i][0].uuid


# ---------------------------------------------------------------------------
# Pre-resolution hook
# ---------------------------------------------------------------------------


async def test_pre_resolution_defer_leaves_ordinary_resolution(monkeypatch):
    clients, llm_generate = _make_clients()
    candidate = _make_node('Joseph')
    monkeypatch.setattr(
        node_operations, '_semantic_candidate_search', _semantic_candidates([[candidate]])
    )
    llm_generate.return_value = {
        'entity_resolutions': [{'id': 0, 'name': 'Joe', 'duplicate_candidate_id': 0}]
    }
    hook = RecordingPreResolution()

    extracted = _make_node('Joe')
    resolved, uuid_map, duplicates = await node_operations.resolve_extracted_nodes(
        clients, [extracted], node_pre_resolution_hook=cast(Any, hook)
    )

    # DEFER: exactly one hook call, ordinary search + LLM path still ran
    assert len(hook.calls) == 1
    assert llm_generate.await_count == 1
    assert uuid_map[extracted.uuid] == candidate.uuid
    assert duplicates == [(extracted, candidate)]
    assert resolved == [candidate]


async def test_pre_resolution_resolve_bypasses_search_filter_and_llm(monkeypatch):
    clients, llm_generate = _make_clients()
    extracted = [_make_node('Joe'), _make_node('Jo')]
    canonical = _make_node('Canonical Joe')
    # genuinely same-UUID pre-resolution: no duplicate pair may be recorded
    canonical.uuid = extracted[0].uuid
    joseph = _make_node('Joseph')
    monkeypatch.setattr(
        node_operations, '_semantic_candidate_search', _semantic_candidates([[joseph]])
    )
    llm_generate.return_value = {
        'entity_resolutions': [{'id': 0, 'name': 'Jo', 'duplicate_candidate_id': 0}]
    }
    candidate_filter = RecordingCandidateFilter()
    identity_gate = RecordingIdentityGate()
    hook = RecordingPreResolution(
        results={
            'Joe': PreResolutionResult(
                decision=PreResolutionDecision.RESOLVE, resolved_node=canonical
            )
        }
    )

    resolved, uuid_map, duplicates = await node_operations.resolve_extracted_nodes(
        clients,
        extracted,
        identity_gate_hook=cast(Any, identity_gate),
        candidate_filter_hook=cast(Any, candidate_filter),
        node_pre_resolution_hook=cast(Any, hook),
    )

    # pre-resolved node never reached search, the candidate filter, the
    # identity gate, or the dedupe LLM (the LLM only saw the deferred node)
    assert resolved[0] is canonical
    assert uuid_map[extracted[0].uuid] == canonical.uuid
    # same-UUID pre-resolution records no duplicate pair; only the deferred
    # node's ordinary merge is recorded
    assert duplicates == [(extracted[1], joseph)]
    assert [call.extracted_node.name for call in candidate_filter.calls] == ['Jo']
    assert [call.extracted_node.name for call in identity_gate.calls] == ['Jo']
    assert uuid_map[extracted[1].uuid] == joseph.uuid


async def test_pre_resolution_different_uuid_records_duplicate_pair_once(monkeypatch):
    clients, llm_generate = _make_clients()
    canonical = _make_node('Canonical Joe')
    monkeypatch.setattr(node_operations, '_semantic_candidate_search', _semantic_candidates([[]]))
    hook = RecordingPreResolution(
        results={
            'Joe': PreResolutionResult(
                decision=PreResolutionDecision.RESOLVE, resolved_node=canonical
            )
        }
    )

    extracted = _make_node('Joe')
    resolved, uuid_map, duplicates = await node_operations.resolve_extracted_nodes(
        clients, [extracted], node_pre_resolution_hook=cast(Any, hook)
    )

    assert resolved == [canonical]
    # the resolution contract maps extracted UUID -> resolved UUID, with no
    # redundant resolved_uuid -> resolved_uuid entry
    assert uuid_map == {extracted.uuid: canonical.uuid}
    assert duplicates == [(extracted, canonical)]
    assert llm_generate.await_count == 0


async def test_pre_resolution_invalid_return_shape_raises_type_error(monkeypatch):
    clients, _ = _make_clients()
    monkeypatch.setattr(node_operations, '_semantic_candidate_search', _semantic_candidates([[]]))

    class BareEntityNodeHook:
        """A bare EntityNode is NOT an implicit RESOLVE."""

        async def pre_resolve_node(self, context):
            return _make_node('Joseph')

    class DeferWithNodeHook:
        """DEFER combined with a payload node is invalid."""

        async def pre_resolve_node(self, context):
            return PreResolutionResult(
                decision=PreResolutionDecision.DEFER, resolved_node=_make_node('Joseph')
            )

    class ResolveWithoutNodeHook:
        """RESOLVE without an EntityNode payload is invalid."""

        async def pre_resolve_node(self, context):
            return PreResolutionResult(decision=PreResolutionDecision.RESOLVE, resolved_node=None)

    class PlainStringHook:
        async def pre_resolve_node(self, context):
            return 'Joseph'

    for bad_hook in (
        BareEntityNodeHook(),
        DeferWithNodeHook(),
        ResolveWithoutNodeHook(),
        PlainStringHook(),
    ):
        with pytest.raises(TypeError, match='node_pre_resolution_hook'):
            await node_operations.resolve_extracted_nodes(
                clients,
                [_make_node('Joe')],
                node_pre_resolution_hook=cast(Any, bad_hook),
            )


async def test_pre_resolution_exception_propagates(monkeypatch):
    clients, _ = _make_clients()
    monkeypatch.setattr(node_operations, '_semantic_candidate_search', _semantic_candidates([[]]))
    sentinel = RuntimeError('hook exploded')

    class ExplodingHook:
        async def pre_resolve_node(self, context):
            raise sentinel

    with pytest.raises(RuntimeError) as exc_info:
        await node_operations.resolve_extracted_nodes(
            clients, [_make_node('Joe')], node_pre_resolution_hook=cast(Any, ExplodingHook())
        )
    assert exc_info.value is sentinel


async def test_pre_resolution_context_carries_request_local_inputs(monkeypatch):
    clients, _ = _make_clients()
    episode = _make_episode()
    previous_episodes = [_make_episode('prev')]
    entity_types = {'Person': type('Person', (), {})}
    pre_res_edges = [_make_edge(_make_node('Alice'), _make_node('Bob'), 'Alice likes Bob')]
    monkeypatch.setattr(node_operations, '_semantic_candidate_search', _semantic_candidates([[]]))
    hook = RecordingPreResolution()

    extracted = _make_node('Joe')
    await node_operations.resolve_extracted_nodes(
        clients,
        [extracted],
        episode=episode,
        previous_episodes=previous_episodes,
        entity_types=entity_types,
        node_pre_resolution_edges=pre_res_edges,
        node_pre_resolution_hook=cast(Any, hook),
    )

    context = hook.calls[0]
    assert context.extracted_node is extracted
    assert context.clients is clients
    assert context.episode is episode
    assert context.previous_episodes is previous_episodes
    assert context.entity_types is entity_types
    # edge evidence comes from the dedicated pre-resolution channel only
    assert context.edges is pre_res_edges


async def test_pre_resolution_edges_independent_of_identity_gate_edges(monkeypatch):
    clients, llm_generate = _make_clients()
    identity_edges = [_make_edge(_make_node('Alice'), _make_node('Bob'), 'Alice likes Bob')]
    pre_res_edges = [_make_edge(_make_node('Alice'), _make_node('Bob'), 'Alice knows Bob')]
    candidate = _make_node('Joseph')
    monkeypatch.setattr(
        node_operations, '_semantic_candidate_search', _semantic_candidates([[candidate]])
    )
    llm_generate.return_value = {
        'entity_resolutions': [{'id': 0, 'name': 'Joe', 'duplicate_candidate_id': 0}]
    }
    identity_gate = RecordingIdentityGate()
    bisector = _BisectingLLM(llm_generate, raise_if=lambda size: False)
    bisector.install(monkeypatch)
    hook = RecordingPreResolution()

    extracted = _make_node('Joe')
    await node_operations.resolve_extracted_nodes(
        clients,
        [extracted],
        identity_gate_hook=cast(Any, identity_gate),
        identity_gate_edges=identity_edges,
        node_pre_resolution_hook=cast(Any, hook),
        node_pre_resolution_edges=pre_res_edges,
    )

    # pre-resolution context saw only its own channel
    assert hook.calls[0].edges is pre_res_edges
    # the LLM (identity-gate) channel still received identity_gate_edges unchanged
    for call in bisector.calls:
        assert call['identity_gate_edges'] is identity_edges
    assert identity_gate.calls[0].edges is identity_edges


def test_frozen_pre_resolution_context_rejects_reassignment():
    from graphiti_core.graphiti_types import GraphitiClients as Clients

    context = NodePreResolutionContext(
        extracted_node=_make_node('Joe'),
        clients=Clients.model_construct(),
        episode=None,
        previous_episodes=[],
        entity_types=None,
        edges=[],
    )
    with pytest.raises(AttributeError):
        cast(Any, context).extracted_node = _make_node('Bob')


def test_pre_resolution_hook_is_runtime_checkable_protocol():
    assert isinstance(RecordingPreResolution(), NodePreResolutionHook)


# ---------------------------------------------------------------------------
# Constructor / class-level storage
# ---------------------------------------------------------------------------


def test_graphiti_constructor_stores_node_pre_resolution_hook():
    hook = RecordingPreResolution()
    graphiti = _make_full_graphiti(node_pre_resolution_hook=hook)
    assert graphiti.node_pre_resolution_hook is hook


def test_graphiti_defaults_have_no_node_pre_resolution_hook():
    default_graphiti = _make_full_graphiti()
    assert default_graphiti.node_pre_resolution_hook is None
    assert default_graphiti.identity_gate_hook is None
    assert default_graphiti.candidate_filter_hook is None
    bare = Graphiti.__new__(Graphiti)
    assert bare.node_pre_resolution_hook is None


# ---------------------------------------------------------------------------
# Public wiring
# ---------------------------------------------------------------------------


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


async def test_add_episode_wiring_passes_node_pre_resolution_hook(monkeypatch):
    graphiti = _make_full_graphiti(node_pre_resolution_hook=RecordingPreResolution())
    captured = await _run_add_episode_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('node_pre_resolution_hook') is graphiti.node_pre_resolution_hook
    # pre-resolution edge evidence travels its own channel (combined-extraction edges)
    assert captured['kwargs'].get('node_pre_resolution_edges') is captured['precomputed_edges']
    # identity channel stays independent and inactive without the identity hook
    assert 'identity_gate_hook' not in captured['kwargs']
    assert 'identity_gate_edges' not in captured['kwargs']
    assert 'candidate_filter_hook' not in captured['kwargs']


async def test_add_episode_no_hook_uses_legacy_resolver_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_add_episode_wiring(monkeypatch, graphiti)

    assert 'node_pre_resolution_hook' not in captured['kwargs']
    assert 'node_pre_resolution_edges' not in captured['kwargs']
    assert 'identity_gate_hook' not in captured['kwargs']
    assert 'candidate_filter_hook' not in captured['kwargs']


async def test_add_episode_composes_all_three_hooks(monkeypatch):
    graphiti = _make_full_graphiti(
        node_pre_resolution_hook=RecordingPreResolution(),
        identity_gate_hook=RecordingIdentityGate(),
        candidate_filter_hook=RecordingCandidateFilter(),
    )
    captured = await _run_add_episode_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('node_pre_resolution_hook') is graphiti.node_pre_resolution_hook
    assert captured['kwargs'].get('identity_gate_hook') is graphiti.identity_gate_hook
    assert captured['kwargs'].get('candidate_filter_hook') is graphiti.candidate_filter_hook
    # both edge-evidence channels carry the same request-local edges here, but
    # through independently named kwargs
    assert (
        captured['kwargs'].get('identity_gate_edges')
        is captured['kwargs'].get('node_pre_resolution_edges')
        is captured['precomputed_edges']
    )


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


async def test_add_triplet_wiring_passes_node_pre_resolution_hook(monkeypatch):
    graphiti = _make_full_graphiti(node_pre_resolution_hook=RecordingPreResolution())
    captured = await _run_add_triplet_wiring(monkeypatch, graphiti)

    assert [call['resolved_name'] for call in captured] == ['Alice', 'Bob']
    for call in captured:
        assert call['args'] == ()
        assert call['kwargs'].get('node_pre_resolution_hook') is graphiti.node_pre_resolution_hook
        assert 'identity_gate_hook' not in call['kwargs']


async def test_add_triplet_no_hook_uses_legacy_resolver_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_add_triplet_wiring(monkeypatch, graphiti)

    for call in captured:
        assert call['args'] == ()
        assert 'node_pre_resolution_hook' not in call['kwargs']


async def _run_bulk_dedupe_wiring(monkeypatch, graphiti: Graphiti) -> dict:
    """Run _extract_and_dedupe_nodes_bulk with extraction/dedupe stubbed; capture kwargs."""
    episode = _make_episode()
    nodes = [_make_node('Alice')]
    edges = [_make_edge(nodes[0], _make_node('Bob'), 'Alice likes Bob')]
    # episode-indexed shape, matching what dedupe_nodes_bulk receives
    extracted_edges_bulk = [edges]
    captured: dict = {}

    async def fake_dedupe_bulk(clients, extracted_nodes, episode_context, entity_types, **kwargs):
        captured['kwargs'] = kwargs
        return {episode.uuid: nodes}, {}

    monkeypatch.setattr(graphiti_module, 'dedupe_nodes_bulk', fake_dedupe_bulk)
    monkeypatch.setattr(
        graphiti_module,
        'extract_nodes_and_edges_bulk',
        AsyncMock(return_value=([nodes], extracted_edges_bulk)),
    )

    await graphiti._extract_and_dedupe_nodes_bulk([(episode, [])], None, None, None, None)
    captured['extracted_edges_bulk'] = extracted_edges_bulk
    return captured


async def test_extract_and_dedupe_nodes_bulk_wiring(monkeypatch):
    graphiti = _make_full_graphiti(node_pre_resolution_hook=RecordingPreResolution())
    captured = await _run_bulk_dedupe_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('node_pre_resolution_hook') is graphiti.node_pre_resolution_hook
    # dedicated pre-resolution edge channel carries the episode-indexed bulk edges
    assert captured['kwargs'].get('node_pre_resolution_edges') is captured['extracted_edges_bulk']
    assert 'identity_gate_hook' not in captured['kwargs']
    assert 'identity_gate_edges' not in captured['kwargs']


async def test_extract_and_dedupe_nodes_bulk_no_hook_uses_legacy_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_bulk_dedupe_wiring(monkeypatch, graphiti)

    assert 'node_pre_resolution_hook' not in captured['kwargs']
    assert 'node_pre_resolution_edges' not in captured['kwargs']
    assert 'identity_gate_hook' not in captured['kwargs']


async def test_extract_and_dedupe_nodes_bulk_composes_all_hooks(monkeypatch):
    graphiti = _make_full_graphiti(
        node_pre_resolution_hook=RecordingPreResolution(),
        identity_gate_hook=RecordingIdentityGate(),
        candidate_filter_hook=RecordingCandidateFilter(),
    )
    captured = await _run_bulk_dedupe_wiring(monkeypatch, graphiti)

    assert captured['kwargs'].get('node_pre_resolution_hook') is graphiti.node_pre_resolution_hook
    assert captured['kwargs'].get('identity_gate_hook') is graphiti.identity_gate_hook
    assert captured['kwargs'].get('candidate_filter_hook') is graphiti.candidate_filter_hook


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
    graphiti = _make_full_graphiti(node_pre_resolution_hook=RecordingPreResolution())
    captured = await _run_bulk_resolve_wiring(monkeypatch, graphiti)

    assert len(captured) == 1
    assert captured[0].get('node_pre_resolution_hook') is graphiti.node_pre_resolution_hook
    # dedicated per-episode pre-resolution edge channel is forwarded
    assert 'node_pre_resolution_edges' in captured[0]
    assert 'identity_gate_hook' not in captured[0]


async def test_resolve_nodes_and_edges_bulk_no_hook_uses_legacy_signature(monkeypatch):
    graphiti = _make_full_graphiti()
    captured = await _run_bulk_resolve_wiring(monkeypatch, graphiti)

    assert 'node_pre_resolution_hook' not in captured[0]
    assert 'node_pre_resolution_edges' not in captured[0]
    assert 'identity_gate_hook' not in captured[0]
    assert 'identity_gate_edges' not in captured[0]


async def _run_dedupe_bulk_passthrough(monkeypatch, **hook_kwargs) -> list[dict]:
    nodes = [_make_node('Alice')]
    episode = _make_episode()
    captured: list[dict] = []

    async def fake_resolve_nodes(
        clients, extracted_nodes, ep, previous_episodes, entity_types, **kwargs
    ):
        captured.append({'kwargs': kwargs})
        return extracted_nodes, {}, []

    monkeypatch.setattr(bulk_utils, 'resolve_extracted_nodes', fake_resolve_nodes)

    await dedupe_nodes_bulk(
        _make_clients()[0],
        [nodes],
        [(episode, [])],
        **hook_kwargs,
    )
    return captured


async def test_dedupe_nodes_bulk_passes_through_node_pre_resolution_hook(monkeypatch):
    hook = RecordingPreResolution()
    edges = [[_make_edge(_make_node('Alice'), _make_node('Bob'), 'Alice likes Bob')]]
    captured = await _run_dedupe_bulk_passthrough(
        monkeypatch, node_pre_resolution_hook=hook, node_pre_resolution_edges=edges
    )

    assert captured[0]['kwargs'].get('node_pre_resolution_hook') is hook
    # per-episode slicing of the dedicated pre-resolution edge channel
    assert captured[0]['kwargs'].get('node_pre_resolution_edges') == edges[0]


async def test_dedupe_nodes_bulk_no_hook_uses_legacy_resolver_signature(monkeypatch):
    captured = await _run_dedupe_bulk_passthrough(monkeypatch)

    assert 'node_pre_resolution_hook' not in captured[0]['kwargs']
    assert 'node_pre_resolution_edges' not in captured[0]['kwargs']
    assert 'identity_gate_hook' not in captured[0]['kwargs']
    assert 'identity_gate_edges' not in captured[0]['kwargs']
    assert 'candidate_filter_hook' not in captured[0]['kwargs']


# ---------------------------------------------------------------------------
# Mechanism hygiene
# ---------------------------------------------------------------------------


def test_no_menhir_imports_in_d5_mechanism():
    """Structural check: bisection/pre-resolution code must not import Menhir."""
    repo_root = Path(__file__).resolve().parents[1]
    for module_file in (
        repo_root / 'graphiti_core' / 'node_pre_resolution.py',
        repo_root / 'graphiti_core' / 'errors.py',
    ):
        source = module_file.read_text(encoding='utf-8')
        assert 'menhir' not in source.lower()
    node_operations_path = (
        repo_root / 'graphiti_core' / 'utils' / 'maintenance' / 'node_operations.py'
    )
    source = node_operations_path.read_text(encoding='utf-8')
    assert 'menhir' not in source.lower()


def test_exception_is_public_graphiti_error():
    from graphiti_core.errors import GraphitiError

    assert issubclass(GraphitiRequestTooLargeError, GraphitiError)
