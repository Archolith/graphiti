"""Copies of one stored edge resolved by different mentions must save as one state.

Mentions resolve in parallel, each against its own fetched copy of a stored edge, and the
save is a full replace per row. Without reconciliation the last copy saved wins, so a live
copy can overwrite a copy another mention expired (lost supersession).
"""

import ast
import inspect
import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import graphiti_core.graphiti as graphiti_module
import graphiti_core.utils.maintenance.edge_operations as edge_ops
from graphiti_core.edge_expiry import EdgeExpiryContext, EdgeExpiryDecision
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_config import SearchResults
from graphiti_core.utils.maintenance.edge_operations import (
    reconcile_edge_copies,
    resolve_extracted_edges,
)

GROUP_ID = 'reconcile_test_group'
FIXED_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TRIP_START = datetime(2026, 3, 27, tzinfo=timezone.utc)
TRIP_END = datetime(2026, 4, 1, tzinfo=timezone.utc)
MILAN_START = datetime(2026, 3, 28, tzinfo=timezone.utc)
STORED_UUID = 'stored-rome-trip'


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(edge_ops, 'utc_now', lambda: FIXED_NOW)


def _edge(
    fact: str,
    *,
    uuid: str | None = None,
    valid_at: datetime | None = TRIP_START,
    invalid_at: datetime | None = TRIP_END,
    expired_at: datetime | None = None,
    episodes: list[str] | None = None,
) -> EntityEdge:
    return EntityEdge(
        uuid=uuid if uuid is not None else str(uuid4()),
        source_node_uuid='source_uuid',
        target_node_uuid='target_uuid',
        name='TRAVELED_TO',
        group_id=GROUP_ID,
        fact=fact,
        episodes=episodes if episodes is not None else [],
        created_at=FIXED_NOW - timedelta(days=30),
        valid_at=valid_at,
        invalid_at=invalid_at,
        expired_at=expired_at,
    )


def _stored_copy() -> EntityEdge:
    """A fresh fetch of the stored trip, as each mention's search returns it."""
    return _edge(
        'Alice traveled to Rome from Mar 27 to Apr 1', uuid=STORED_UUID, episodes=['episode_0']
    )


def _state(edge: EntityEdge) -> tuple:
    return edge.expired_at, edge.invalid_at, tuple(edge.episodes)


# --- reconcile_edge_copies ------------------------------------------------------------------


@pytest.mark.parametrize('live_first', [True, False])
def test_expired_copy_wins_in_either_order(live_first):
    live = _stored_copy()
    live.episodes.append('episode_1')
    expired = _stored_copy()
    expired.invalid_at, expired.expired_at = MILAN_START, FIXED_NOW
    edges = [live, expired] if live_first else [expired, live]

    reconcile_edge_copies(edges)

    for edge in edges:
        assert _state(edge) == (FIXED_NOW, MILAN_START, ('episode_0', 'episode_1'))


def test_earliest_expiry_and_end_are_kept():
    first = _stored_copy()
    first.invalid_at, first.expired_at = MILAN_START + timedelta(days=1), FIXED_NOW
    second = _stored_copy()
    second.invalid_at = MILAN_START
    second.expired_at = FIXED_NOW - timedelta(hours=1)

    reconcile_edge_copies([first, second])

    assert (
        _state(first)
        == _state(second)
        == (FIXED_NOW - timedelta(hours=1), MILAN_START, ('episode_0',))
    )


def test_naive_and_aware_datetimes_compare_as_utc():
    aware = _stored_copy()
    aware.invalid_at = datetime(2026, 3, 29, tzinfo=timezone.utc)
    naive = _stored_copy()
    naive.invalid_at = datetime(2026, 3, 28)

    reconcile_edge_copies([aware, naive])

    assert aware.invalid_at == naive.invalid_at == datetime(2026, 3, 28)


def test_single_copies_and_distinct_edges_are_untouched():
    stored = _stored_copy()
    other = _edge('Alice likes pasta', invalid_at=None, episodes=['episode_9'])
    before = (_state(stored), _state(other))

    reconcile_edge_copies([stored, other])

    assert (_state(stored), _state(other)) == before


def test_identical_live_copies_stay_live():
    copies = [_stored_copy(), _stored_copy()]

    reconcile_edge_copies(copies)

    assert [_state(edge) for edge in copies] == [(None, TRIP_END, ('episode_0',))] * 2


def test_open_ended_copy_takes_the_contradiction_end():
    """A stored edge with no end: one mention invalidates it, another restates it live."""
    live = _stored_copy()
    live.invalid_at = None
    invalidated = _stored_copy()
    invalidated.invalid_at, invalidated.expired_at = MILAN_START, FIXED_NOW

    reconcile_edge_copies([invalidated, live])

    assert _state(live) == _state(invalidated) == (FIXED_NOW, MILAN_START, ('episode_0',))


# --- through the real batch resolver --------------------------------------------------------


class _WorldEndHook:
    def __init__(self):
        self.contexts: list[EdgeExpiryContext] = []

    async def decide_edge_expiry(self, context: EdgeExpiryContext) -> EdgeExpiryDecision:
        self.contexts.append(context)
        return EdgeExpiryDecision.WORLD_END


async def _resolve_two_mentions(monkeypatch, *, contradicting_first: bool, hook):
    """Two differently worded mentions; each search returns a fresh copy of the stored trip.

    The contradicting mention's LLM call marks the stored trip a duplicate and the newer Milan
    fact a contradiction; the other marks only the duplicate.
    """

    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    milan = _edge('Alice lives in Milan', uuid='milan', valid_at=MILAN_START, invalid_at=None)

    async def fake_search(clients, query, group_ids, config, search_filter):
        if search_filter.edge_uuids is not None:
            return SearchResults(edges=[_stored_copy()])
        return SearchResults(edges=[_stored_copy(), milan])

    contradicting = _edge('Alice was in Rome until she moved to Milan', invalid_at=None)
    restating = _edge('Alice had a Rome trip in late March', invalid_at=None)
    mentions = [contradicting, restating] if contradicting_first else [restating, contradicting]
    responses = {
        contradicting.fact: {'duplicate_facts': [0], 'contradicted_facts': [1]},
        restating.fact: {'duplicate_facts': [0], 'contradicted_facts': []},
    }

    async def generate_response(messages, **kwargs):
        text = str(messages)
        return next(resp for fact, resp in responses.items() if fact in text)

    llm = MagicMock()
    llm.generate_response = AsyncMock(side_effect=generate_response)
    monkeypatch.setattr(edge_ops, 'semaphore_gather', immediate_gather)
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    monkeypatch.setattr(EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))
    monkeypatch.setattr(edge_ops, 'search', fake_search)

    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=llm, embedder=MagicMock(), cross_encoder=MagicMock()
    )
    nodes = [
        EntityNode(uuid='source_uuid', name='Alice', group_id=GROUP_ID, labels=['Entity']),
        EntityNode(uuid='target_uuid', name='Rome', group_id=GROUP_ID, labels=['Entity']),
    ]
    episode = EpisodicNode(
        uuid='episode_1',
        name='Episode',
        group_id=GROUP_ID,
        source=EpisodeType.message,
        source_description='desc',
        content='Episode content',
        valid_at=FIXED_NOW,
    )
    return await resolve_extracted_edges(
        clients,  # type: ignore[arg-type]
        mentions,
        episode,
        nodes,
        {},
        {},
        edge_expiry_hook=hook,
    )


@pytest.mark.parametrize('contradicting_first', [True, False])
async def test_live_restatement_never_overwrites_a_supersession(monkeypatch, contradicting_first):
    hook = _WorldEndHook()
    resolved, invalidated, new_edges = await _resolve_two_mentions(
        monkeypatch, contradicting_first=contradicting_first, hook=hook
    )

    copies = [edge for edge in resolved + invalidated if edge.uuid == STORED_UUID]
    assert len(copies) == 2
    assert len({id(edge) for edge in copies}) == 2  # separate objects, both saved
    for edge in copies:
        assert _state(edge) == (FIXED_NOW, MILAN_START, ('episode_0', 'episode_1'))
    assert new_edges == []
    assert len(hook.contexts) == 2


@pytest.mark.parametrize('contradicting_first', [True, False])
async def test_control_without_reconcile_copies_disagree(monkeypatch, contradicting_first):
    """The counterexample is real: without reconciliation one copy is live, one expired."""
    monkeypatch.setattr(edge_ops, 'reconcile_edge_copies', lambda edges: None)
    resolved, invalidated, _ = await _resolve_two_mentions(
        monkeypatch, contradicting_first=contradicting_first, hook=_WorldEndHook()
    )

    states = sorted(
        (edge.expired_at is None, edge.invalid_at)
        for edge in resolved + invalidated
        if edge.uuid == STORED_UUID
    )
    assert states == [(False, MILAN_START), (True, TRIP_END)]


async def test_without_hook_upstream_result_is_unchanged(monkeypatch):
    """No hook: path 1 expires both copies before path 2, so no truncation date appears."""
    resolved, invalidated, _ = await _resolve_two_mentions(
        monkeypatch, contradicting_first=False, hook=None
    )

    copies = [edge for edge in resolved + invalidated if edge.uuid == STORED_UUID]
    assert len(copies) == 2
    assert {_state(edge) for edge in copies} == {(FIXED_NOW, TRIP_END, ('episode_0', 'episode_1'))}


# --- every multi-result save site reconciles ------------------------------------------------


def _calls_reconcile(func) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == 'reconcile_edge_copies'
        for node in ast.walk(tree)
    )


def test_batch_resolver_and_bulk_combine_reconcile():
    assert _calls_reconcile(resolve_extracted_edges)
    assert _calls_reconcile(graphiti_module.Graphiti._resolve_nodes_and_edges_bulk)
