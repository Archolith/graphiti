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

import graphiti_core.utils.maintenance.edge_operations as edge_ops
from graphiti_core.edges import EntityEdge
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


async def _resolve(monkeypatch, mentions, *, with_schema, stored_factory=None):
    """Resolve ``mentions`` in one batch; each search returns a fresh copy of the stored edge.

    The paraphrase is answered as a duplicate of the stored edge (index 0); attribute
    extraction answers ``rating=2``. The stored edge carries ``rating=1``.
    """

    def fresh_stored() -> EntityEdge:
        return _edge(STORED_FACT, uuid=STORED_UUID, attributes={'rating': 1})

    stored_factory = stored_factory or fresh_stored

    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    async def fake_search(clients, query, group_ids, config, search_filter):
        return SearchResults(edges=[stored_factory()])

    async def generate_response(messages, **kwargs):
        if kwargs.get('prompt_name') == 'extract_edges.extract_attributes':
            return {'rating': 2}
        return {'duplicate_facts': [0], 'contradicted_facts': []}

    llm = MagicMock()
    llm.generate_response = AsyncMock(side_effect=generate_response)
    monkeypatch.setattr(edge_ops, 'utc_now', lambda: FIXED_NOW)
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
        EntityNode(uuid='target_uuid', name='Rome hotel', group_id=GROUP_ID, labels=['Entity']),
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
    edge_types: dict[str, type[BaseModel]] = {'RATED': Rating} if with_schema else {}
    edge_type_map = {('Entity', 'Entity'): ['RATED']} if with_schema else {}
    resolved, invalidated, _ = await resolve_extracted_edges(
        clients,  # type: ignore[arg-type]
        mentions,
        episode,
        nodes,
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
