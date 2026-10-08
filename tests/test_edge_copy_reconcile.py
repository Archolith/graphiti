"""Copies of one stored edge resolved by different mentions must save as one state.

Mentions resolve in parallel, each against its own fetched copy of a stored edge, and the
save is a full replace per row. Without reconciliation the last copy saved wins, so a live
copy can overwrite a copy another mention expired (lost supersession).
"""

import ast
import inspect
import logging
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
from graphiti_core.graphiti import Graphiti
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
STORED_ATTRIBUTES = {'source_note': 'fetched'}


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
    attributes: dict | None = None,
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
        attributes=dict(attributes) if attributes is not None else {},
    )


def _stored_copy(invalid_at: datetime | None = TRIP_END) -> EntityEdge:
    """A fresh fetch of the stored trip, as each mention's search returns it."""
    return _edge(
        'Alice traveled to Rome from Mar 27 to Apr 1',
        uuid=STORED_UUID,
        invalid_at=invalid_at,
        episodes=['episode_0'],
        attributes=STORED_ATTRIBUTES,
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

    reconcile_edge_copies(edges, [])

    for edge in edges:
        assert _state(edge) == (FIXED_NOW, MILAN_START, ('episode_0', 'episode_1'))


def test_earliest_expiry_and_end_are_kept():
    first = _stored_copy()
    first.invalid_at, first.expired_at = MILAN_START + timedelta(days=1), FIXED_NOW
    second = _stored_copy()
    second.invalid_at = MILAN_START
    second.expired_at = FIXED_NOW - timedelta(hours=1)

    reconcile_edge_copies([first, second], [])

    assert (
        _state(first)
        == _state(second)
        == (FIXED_NOW - timedelta(hours=1), MILAN_START, ('episode_0',))
    )


def test_end_is_the_earliest_end_not_the_earliest_expired_copys_end():
    expired_first = _stored_copy()
    expired_first.invalid_at = MILAN_START + timedelta(days=2)
    expired_first.expired_at = FIXED_NOW - timedelta(hours=2)
    earliest_end = _stored_copy()
    earliest_end.invalid_at, earliest_end.expired_at = MILAN_START, FIXED_NOW

    reconcile_edge_copies([expired_first], [earliest_end])

    for edge in (expired_first, earliest_end):
        assert _state(edge) == (FIXED_NOW - timedelta(hours=2), MILAN_START, ('episode_0',))


def test_naive_and_aware_datetimes_compare_as_utc():
    aware = _stored_copy()
    aware.invalid_at, aware.expired_at = datetime(2026, 3, 29, tzinfo=timezone.utc), FIXED_NOW
    naive = _stored_copy()
    naive.invalid_at, naive.expired_at = datetime(2026, 3, 28), FIXED_NOW

    reconcile_edge_copies([aware, naive], [])

    assert aware.invalid_at == naive.invalid_at == datetime(2026, 3, 28)


def test_single_copies_and_distinct_edges_are_untouched():
    stored = _stored_copy()
    other = _edge('Alice likes pasta', invalid_at=None, episodes=['episode_9'])
    before = (_state(stored), _state(other), stored.attributes, other.attributes)

    reconcile_edge_copies([stored], [other])

    assert (_state(stored), _state(other), stored.attributes, other.attributes) == before


def test_identical_live_copies_stay_live():
    copies = [_stored_copy(), _stored_copy()]

    reconcile_edge_copies(copies, [])

    assert [_state(edge) for edge in copies] == [(None, TRIP_END, ('episode_0',))] * 2


def test_open_ended_copy_takes_the_contradiction_end():
    """A stored edge with no end: one mention invalidates it, another restates it live."""
    live = _stored_copy(invalid_at=None)
    invalidated = _stored_copy(invalid_at=None)
    invalidated.invalid_at, invalidated.expired_at = MILAN_START, FIXED_NOW

    reconcile_edge_copies([live], [invalidated])

    assert _state(live) == _state(invalidated) == (FIXED_NOW, MILAN_START, ('episode_0',))


@pytest.mark.parametrize('invalidated_first', [True, False])
def test_attributes_come_from_the_resolved_copy(invalidated_first):
    resolved = _stored_copy()
    resolved.attributes = {}  # resolution recomputed (here: cleared) the attributes
    invalidated = [_stored_copy(), _stored_copy()]
    for edge in invalidated:
        edge.invalid_at, edge.expired_at = MILAN_START, FIXED_NOW

    reconcile_edge_copies([resolved], invalidated[::-1] if invalidated_first else invalidated)

    assert [edge.attributes for edge in [resolved, *invalidated]] == [{}, {}, {}]
    invalidated[0].attributes['x'] = 1
    assert resolved.attributes == {}  # copies do not share one dict


def test_only_invalidated_copies_keep_their_fetched_attributes():
    copies = [_stored_copy(), _stored_copy()]
    copies[0].invalid_at, copies[0].expired_at = MILAN_START, FIXED_NOW

    reconcile_edge_copies([], copies)

    assert [edge.attributes for edge in copies] == [STORED_ATTRIBUTES, STORED_ATTRIBUTES]


def test_a_uuid_shared_by_different_edges_is_not_reconciled(caplog):
    stored = _stored_copy()
    other = _edge('Alice lives in Milan', uuid=STORED_UUID, invalid_at=None)
    other.expired_at = FIXED_NOW
    before = (_state(stored), _state(other))

    with caplog.at_level(logging.WARNING, logger=edge_ops.logger.name):
        reconcile_edge_copies([stored], [other])

    assert (_state(stored), _state(other)) == before
    assert 'not copies of one edge' in caplog.text


# --- through the real batch resolver --------------------------------------------------------


class _WorldEndHook:
    def __init__(self):
        self.contexts: list[EdgeExpiryContext] = []

    async def decide_edge_expiry(self, context: EdgeExpiryContext) -> EdgeExpiryDecision:
        self.contexts.append(context)
        return EdgeExpiryDecision.WORLD_END


async def _resolve_mentions(
    monkeypatch, mentions, responses, *, hook, stored_invalid_at: datetime | None = TRIP_END
):
    """Resolve ``mentions`` in one batch; each search returns a fresh copy of the stored trip.

    ``responses`` maps a mention's fact to the dedupe LLM answer for it. Related edges are
    ``[stored]`` (index 0); invalidation candidates are ``[milan]`` (index 1).
    """

    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    milan = _edge('Alice lives in Milan', uuid='milan', valid_at=MILAN_START, invalid_at=None)

    async def fake_search(clients, query, group_ids, config, search_filter):
        if search_filter.edge_uuids is not None:
            return SearchResults(edges=[_stored_copy(stored_invalid_at)])
        return SearchResults(edges=[_stored_copy(stored_invalid_at), milan])

    async def generate_response(messages, **kwargs):
        text = str(messages)
        return next(resp for fact, resp in responses.items() if fact in text)

    llm = MagicMock()
    llm.generate_response = AsyncMock(side_effect=generate_response)
    monkeypatch.setattr(edge_ops, 'semaphore_gather', immediate_gather)
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    monkeypatch.setattr(edge_ops, '_extract_edge_timestamps', AsyncMock(return_value=None))
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


async def _resolve_two_mentions(
    monkeypatch,
    *,
    contradicting_first: bool,
    hook,
    stored_invalid_at: datetime | None = TRIP_END,
):
    """Both mentions restate the stored trip; one also contradicts it with the newer Milan fact."""
    contradicting = _edge('Alice was in Rome until she moved to Milan', invalid_at=None)
    restating = _edge('Alice had a Rome trip in late March', invalid_at=None)
    mentions = [contradicting, restating] if contradicting_first else [restating, contradicting]
    responses = {
        contradicting.fact: {'duplicate_facts': [0], 'contradicted_facts': [1]},
        restating.fact: {'duplicate_facts': [0], 'contradicted_facts': []},
    }
    return await _resolve_mentions(
        monkeypatch, mentions, responses, hook=hook, stored_invalid_at=stored_invalid_at
    )


def _copies(resolved, invalidated) -> list[EntityEdge]:
    return [edge for edge in resolved + invalidated if edge.uuid == STORED_UUID]


@pytest.mark.parametrize('contradicting_first', [True, False])
async def test_live_restatement_never_overwrites_a_supersession(monkeypatch, contradicting_first):
    hook = _WorldEndHook()
    resolved, invalidated, new_edges = await _resolve_two_mentions(
        monkeypatch, contradicting_first=contradicting_first, hook=hook
    )

    copies = _copies(resolved, invalidated)
    assert len(copies) == 2
    assert len({id(edge) for edge in copies}) == 2  # separate objects, both saved
    for edge in copies:
        assert _state(edge) == (FIXED_NOW, MILAN_START, ('episode_0', 'episode_1'))
    assert new_edges == []
    assert len(hook.contexts) == 2


@pytest.mark.parametrize('contradicting_first', [True, False])
async def test_control_without_reconcile_copies_disagree(monkeypatch, contradicting_first):
    """The counterexample is real: without reconciliation one copy is live, one expired."""
    monkeypatch.setattr(edge_ops, 'reconcile_edge_copies', lambda resolved, invalidated: None)
    resolved, invalidated, _ = await _resolve_two_mentions(
        monkeypatch, contradicting_first=contradicting_first, hook=_WorldEndHook()
    )

    states = sorted(
        (edge.expired_at is None, edge.invalid_at) for edge in _copies(resolved, invalidated)
    )
    assert states == [(False, MILAN_START), (True, TRIP_END)]


@pytest.mark.parametrize('contradicting_first', [True, False])
@pytest.mark.parametrize('reconcile', [True, False])
async def test_without_hook_an_open_ended_stored_edge_keeps_its_supersession(
    monkeypatch, contradicting_first, reconcile
):
    """Upstream (no hook): one mention truncates the open-ended stored edge, one restates it."""
    if not reconcile:
        monkeypatch.setattr(edge_ops, 'reconcile_edge_copies', lambda resolved, invalidated: None)
    resolved, invalidated, _ = await _resolve_two_mentions(
        monkeypatch, contradicting_first=contradicting_first, hook=None, stored_invalid_at=None
    )

    states = {_state(edge) for edge in _copies(resolved, invalidated)}
    if reconcile:
        assert states == {(FIXED_NOW, MILAN_START, ('episode_0', 'episode_1'))}
    else:
        assert states == {
            (FIXED_NOW, MILAN_START, ('episode_0', 'episode_1')),
            (None, None, ('episode_0', 'episode_1')),
        }


@pytest.mark.parametrize('contradicting_first', [True, False])
async def test_invalidated_copy_and_restated_copy_merge(monkeypatch, contradicting_first):
    """One mention invalidates the stored trip (invalidated list); another restates it
    (resolved list). Both copies save the supersession, both episodes and one attribute set."""
    moved = _edge('Alice moved to Milan', valid_at=MILAN_START, invalid_at=None)
    restating = _edge('Alice had a Rome trip in late March', invalid_at=None)
    mentions = [moved, restating] if contradicting_first else [restating, moved]
    responses = {
        moved.fact: {'duplicate_facts': [], 'contradicted_facts': [0]},
        restating.fact: {'duplicate_facts': [0], 'contradicted_facts': []},
    }
    resolved, invalidated, new_edges = await _resolve_mentions(
        monkeypatch, mentions, responses, hook=_WorldEndHook()
    )

    resolved_copies = [edge for edge in resolved if edge.uuid == STORED_UUID]
    invalidated_copies = [edge for edge in invalidated if edge.uuid == STORED_UUID]
    assert len(resolved_copies) == 1 and len(invalidated_copies) == 1
    for edge in resolved_copies + invalidated_copies:
        assert _state(edge) == (FIXED_NOW, MILAN_START, ('episode_0', 'episode_1'))
        assert edge.attributes == {}
    assert [edge.uuid for edge in new_edges] == [moved.uuid]


# --- add_episode_bulk combines episodes before one save ------------------------------------


async def test_bulk_reconciles_copies_across_episodes(monkeypatch):
    """Episode 1 restates the stored trip; episode 2 invalidates it. One combined save."""
    episodes = [
        EpisodicNode(
            uuid=f'episode_{i}',
            name=f'Episode {i}',
            group_id=GROUP_ID,
            source=EpisodeType.message,
            source_description='desc',
            content='content',
            valid_at=FIXED_NOW,
        )
        for i in (1, 2)
    ]
    live = _stored_copy()
    live.episodes.append('episode_1')
    live.attributes = {}
    expired = _stored_copy()
    expired.invalid_at, expired.expired_at = MILAN_START, FIXED_NOW
    per_episode = {'episode_1': ([live], [], []), 'episode_2': ([], [expired], [])}

    async def fake_resolve_edges(clients, edges, episode, *args, **kwargs):
        return per_episode[episode.uuid]

    monkeypatch.setattr(
        graphiti_module, 'resolve_extracted_nodes', AsyncMock(return_value=([], {}))
    )
    monkeypatch.setattr(
        graphiti_module, 'extract_attributes_from_nodes', AsyncMock(return_value=[])
    )
    monkeypatch.setattr(graphiti_module, 'resolve_edge_pointers', lambda edges, uuid_map: edges)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edges', fake_resolve_edges)

    graphiti = object.__new__(Graphiti)
    graphiti.clients = SimpleNamespace()  # type: ignore[assignment]
    graphiti.edge_expiry_hook = None
    graphiti.identity_gate_hook = None
    graphiti.candidate_filter_hook = None
    graphiti.node_pre_resolution_hook = None

    _, resolved, invalidated, _ = await graphiti._resolve_nodes_and_edges_bulk(
        {episode.uuid: [] for episode in episodes},
        {episode.uuid: [] for episode in episodes},
        [(episode, []) for episode in episodes],
        None,
        None,
        {},
        episodes,
    )

    assert resolved == [live] and invalidated == [expired]
    for edge in (live, expired):
        assert _state(edge) == (FIXED_NOW, MILAN_START, ('episode_0', 'episode_1'))
        assert edge.attributes == {}


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
