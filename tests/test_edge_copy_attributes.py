"""Copies of one stored edge: recomputed attributes win over fetched ones.

An exact-fact mention returns its fetched copy through the fast path with the stored
attributes untouched; a paraphrased mention of the same stored edge recomputes (or clears)
them. Reconciliation must give every copy the recomputed attributes, in either order.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import BaseModel

import graphiti_core.graphiti as graphiti_module
import graphiti_core.utils.maintenance.edge_operations as edge_ops
from graphiti_core.edges import EntityEdge
from graphiti_core.graphiti import Graphiti
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_config import SearchResults
from graphiti_core.utils.maintenance.edge_operations import (
    reconcile_edge_copies,
    resolve_extracted_edges,
)

GROUP_ID = 'reconcile_attributes_group'
FIXED_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
STORED_UUID = 'stored-rating'
STORED_FACT = 'Alice rated the Rome hotel'
PARAPHRASE = 'Alice gave the hotel in Rome a rating'


class Rating(BaseModel):
    rating: int


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


def _stored(*, mark: bool = False) -> EntityEdge:
    edge = _edge(STORED_FACT, uuid=STORED_UUID, attributes={'rating': 1})
    edge.valid_at = FIXED_NOW - timedelta(days=30)
    edge._attributes_resolved = mark
    return edge


def _episode(index: int) -> EpisodicNode:
    return EpisodicNode(
        uuid=f'episode_{index}',
        name=f'Episode {index}',
        group_id=GROUP_ID,
        source=EpisodeType.message,
        source_description='desc',
        content='Episode content',
        valid_at=FIXED_NOW,
    )


def _schema(with_schema: bool) -> tuple[dict[str, type[BaseModel]], dict]:
    if not with_schema:
        return {}, {}
    return {'RATED': Rating}, {('Entity', 'Entity'): ['RATED']}


def _patch(monkeypatch, *, fake_search=None, contradicting_fact=None):
    """Patch resolution; returns the list of dedupe (non-attribute) prompts the LLM answered.

    Every search returns a fresh copy of the stored edge unless ``fake_search`` is given. A
    mention is answered as a duplicate of the stored edge (index 0), or, when its fact is
    ``contradicting_fact``, as contradicting it. Attribute extraction answers ``rating=2``.
    """
    dedupe_prompts: list[str] = []

    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    async def fresh_search(clients, query, group_ids, config, search_filter):
        return SearchResults(edges=[_stored()])

    async def generate_response(messages, **kwargs):
        if kwargs.get('prompt_name') == 'extract_edges.extract_attributes':
            return {'rating': 2}
        dedupe_prompts.append(str(messages))
        if contradicting_fact is not None and contradicting_fact in str(messages):
            return {'duplicate_facts': [], 'contradicted_facts': [0]}
        return {'duplicate_facts': [0], 'contradicted_facts': []}

    llm = MagicMock()
    llm.generate_response = AsyncMock(side_effect=generate_response)
    monkeypatch.setattr(edge_ops, 'utc_now', lambda: FIXED_NOW)
    monkeypatch.setattr(edge_ops, 'semaphore_gather', immediate_gather)
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    monkeypatch.setattr(edge_ops, '_extract_edge_timestamps', AsyncMock(return_value=None))
    monkeypatch.setattr(EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))
    monkeypatch.setattr(edge_ops, 'search', fake_search or fresh_search)
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=llm, embedder=MagicMock(), cross_encoder=MagicMock()
    )
    return clients, dedupe_prompts


NODES = [
    EntityNode(uuid='source_uuid', name='Alice', group_id=GROUP_ID, labels=['Entity']),
    EntityNode(uuid='target_uuid', name='Rome hotel', group_id=GROUP_ID, labels=['Entity']),
]


async def _resolve(monkeypatch, mentions, *, with_schema, stored_factory=None):
    """Resolve ``mentions`` in one episode; returns every copy of the stored edge."""
    fake_search = None
    if stored_factory is not None:

        async def fake_search(clients, query, group_ids, config, search_filter):
            return SearchResults(edges=[stored_factory()])

    clients, _ = _patch(monkeypatch, fake_search=fake_search)
    edge_types, edge_type_map = _schema(with_schema)
    resolved, invalidated, _ = await resolve_extracted_edges(
        clients,  # type: ignore[arg-type]
        mentions,
        _episode(1),
        NODES,
        edge_types,
        edge_type_map,
    )
    return [edge for edge in resolved + invalidated if edge.uuid == STORED_UUID]


def _mentions(exact_first: bool) -> list[EntityEdge]:
    exact = _edge(STORED_FACT)
    paraphrase = _edge(PARAPHRASE)
    return [exact, paraphrase] if exact_first else [paraphrase, exact]


@pytest.mark.parametrize('exact_first', [True, False])
async def test_recomputed_attributes_beat_the_fast_path_copy(monkeypatch, exact_first):
    copies = await _resolve(monkeypatch, _mentions(exact_first), with_schema=True)

    assert len(copies) == 2
    assert len({id(edge) for edge in copies}) == 2  # separate objects, both saved
    assert [edge.attributes for edge in copies] == [{'rating': 2}, {'rating': 2}]
    assert all(edge.episodes == ['episode_0', 'episode_1'] for edge in copies)


@pytest.mark.parametrize('exact_first', [True, False])
async def test_cleared_attributes_beat_the_fast_path_copy(monkeypatch, exact_first):
    """No schema applies: the paraphrase clears attributes; the stale ones must not return."""
    copies = await _resolve(monkeypatch, _mentions(exact_first), with_schema=False)

    assert len(copies) == 2
    assert [edge.attributes for edge in copies] == [{}, {}]


@pytest.mark.parametrize('exact_first', [True, False])
async def test_a_mark_left_from_an_earlier_resolution_is_ignored(monkeypatch, exact_first):
    """A reused object that an earlier resolution marked must not count as recomputed now."""

    def reused_stored() -> EntityEdge:
        edge = _edge(STORED_FACT, uuid=STORED_UUID, attributes={'rating': 1})
        edge._attributes_resolved = True
        return edge

    copies = await _resolve(
        monkeypatch, _mentions(exact_first), with_schema=True, stored_factory=reused_stored
    )

    assert [edge.attributes for edge in copies] == [{'rating': 2}, {'rating': 2}]


async def test_the_exact_mention_takes_the_fast_path(monkeypatch):
    """Guards the premise of the tests above: only the paraphrase reaches the LLM dedupe."""
    clients, dedupe_prompts = _patch(monkeypatch)
    edge_types, edge_type_map = _schema(True)

    await resolve_extracted_edges(
        clients,  # type: ignore[arg-type]
        _mentions(exact_first=True),
        _episode(1),
        NODES,
        edge_types,
        edge_type_map,
    )

    assert len(dedupe_prompts) == 1 and PARAPHRASE in dedupe_prompts[0]


@pytest.mark.parametrize('paraphrase_first', [True, False])
async def test_a_stale_mark_on_an_invalidation_candidate_is_ignored(monkeypatch, paraphrase_first):
    """A reused object, marked by an earlier resolution, arrives only as an invalidation
    candidate of a newer contradicting mention. Its stale attributes must not win."""
    newer = 'Alice now rates the Rome hotel differently'

    async def fake_search(clients, query, group_ids, config, search_filter):
        if query != newer:
            return SearchResults(edges=[_stored()])
        if search_filter.edge_uuids is not None:
            return SearchResults(edges=[])  # no duplicate candidates for the newer mention
        return SearchResults(edges=[_stored(mark=True)])

    clients, _ = _patch(monkeypatch, fake_search=fake_search, contradicting_fact=newer)
    edge_types, edge_type_map = _schema(True)
    paraphrase = _edge(PARAPHRASE)
    contradicting = _edge(newer)
    contradicting.valid_at = FIXED_NOW
    mentions = [paraphrase, contradicting] if paraphrase_first else [contradicting, paraphrase]

    resolved, invalidated, _ = await resolve_extracted_edges(
        clients,  # type: ignore[arg-type]
        mentions,
        _episode(1),
        NODES,
        edge_types,
        edge_type_map,
    )

    invalidated_copies = [edge for edge in invalidated if edge.uuid == STORED_UUID]
    resolved_copies = [edge for edge in resolved if edge.uuid == STORED_UUID]
    assert len(invalidated_copies) == 1 and len(resolved_copies) == 1
    assert [edge.attributes for edge in resolved_copies + invalidated_copies] == [
        {'rating': 2},
        {'rating': 2},
    ]


@pytest.mark.parametrize('paraphrase_first', [True, False])
async def test_bulk_episodes_keep_the_recomputed_attributes(monkeypatch, paraphrase_first):
    """add_episode_bulk: one episode paraphrases the stored edge, another states it exactly.
    Real per-episode resolution, then the cross-episode reconcile, in both episode orders."""
    clients, _ = _patch(monkeypatch)
    monkeypatch.setattr(
        graphiti_module, 'resolve_extracted_nodes', AsyncMock(return_value=([], {}))
    )
    monkeypatch.setattr(
        graphiti_module, 'extract_attributes_from_nodes', AsyncMock(return_value=NODES)
    )
    episodes = [_episode(1), _episode(2)]
    first, second = _edge(PARAPHRASE), _edge(STORED_FACT)
    if not paraphrase_first:
        first, second = second, first
    edge_types, edge_type_map = _schema(True)

    graphiti = object.__new__(Graphiti)
    graphiti.clients = clients  # type: ignore[assignment]
    graphiti.edge_expiry_hook = None
    graphiti.identity_gate_hook = None
    graphiti.candidate_filter_hook = None
    graphiti.node_pre_resolution_hook = None

    _, resolved, invalidated, _ = await graphiti._resolve_nodes_and_edges_bulk(
        {episode.uuid: [] for episode in episodes},
        {'episode_1': [first], 'episode_2': [second]},
        [(episode, []) for episode in episodes],
        None,
        edge_types,
        edge_type_map,
        episodes,
    )

    copies = [edge for edge in resolved + invalidated if edge.uuid == STORED_UUID]
    assert len(copies) == 2
    assert [edge.attributes for edge in copies] == [{'rating': 2}, {'rating': 2}]
    assert all(edge.episodes == ['episode_0', 'episode_1', 'episode_2'] for edge in copies)


def test_without_a_recomputed_copy_attributes_are_untouched():
    first = _edge(STORED_FACT, uuid=STORED_UUID, attributes={'rating': 1})
    second = _edge(STORED_FACT, uuid=STORED_UUID, attributes={'rating': 3})

    reconcile_edge_copies([first], [second])

    assert (first.attributes, second.attributes) == ({'rating': 1}, {'rating': 3})


def test_the_last_recomputed_copy_wins():
    copies = [
        _edge(STORED_FACT, uuid=STORED_UUID, attributes={'rating': rating}) for rating in (1, 2, 3)
    ]
    copies[0]._attributes_resolved = True
    copies[1]._attributes_resolved = True

    reconcile_edge_copies(copies, [])

    assert [edge.attributes for edge in copies] == [{'rating': 2}] * 3
    copies[0].attributes['rating'] = 9
    assert copies[1].attributes == {'rating': 2}  # copies do not share one dict


def test_the_mark_is_never_serialized():
    edge = _edge(STORED_FACT, uuid=STORED_UUID)
    edge._attributes_resolved = True

    assert '_attributes_resolved' not in edge.model_dump()
    assert '_attributes_resolved' not in dict(edge)
    assert EntityEdge(**edge.model_dump())._attributes_resolved is False
