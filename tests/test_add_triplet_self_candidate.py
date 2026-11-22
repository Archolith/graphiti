"""add_triplet never offers an edge's own stored copy as an invalidation candidate (menhir#251).

Updating an edge by an existing uuid: the graph-wide search returns the stored copy (same uuid,
older fact). If it were passed as an invalidation candidate and came back invalidated, two rows
with one uuid and different facts would reach the save and the last row would win.
"""

from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.graphiti import Graphiti
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode
from tests.helpers_test import group_id

pytest_plugins = ('pytest_asyncio', 'tests.helpers_test')


@pytest.mark.asyncio
async def test_stored_copy_of_the_incoming_edge_is_not_an_invalidation_candidate(
    mock_embedder,
) -> None:
    llm = Mock(spec=LLMClient)
    llm.config = Mock()
    ce = Mock(spec=CrossEncoderClient)
    ce.config = Mock()
    graph_driver = Mock()
    graph_driver.__class__ = GraphDriver
    graphiti = Graphiti(
        graph_driver=graph_driver, llm_client=llm, embedder=mock_embedder, cross_encoder=ce
    )
    now = datetime.now()
    source = EntityNode(
        name='Alice', group_id=group_id, labels=['Person'], created_at=now, name_embedding=[0.1]
    )
    target = EntityNode(
        name='Bob', group_id=group_id, labels=['Person'], created_at=now, name_embedding=[0.1]
    )

    def make_edge(fact: str) -> EntityEdge:
        return EntityEdge(
            uuid='edge-1',
            source_node_uuid=source.uuid,
            target_node_uuid=target.uuid,
            name='WORKS_WITH',
            fact=fact,
            group_id=group_id,
            created_at=now,
            valid_at=now,
            fact_embedding=[0.1],
        )

    incoming = make_edge('Alice works with Bob at Acme')
    stored = make_edge('Alice works with Bob')  # same uuid and endpoints, older fact
    other = EntityEdge(
        uuid='edge-other',
        source_node_uuid='x',
        target_node_uuid='y',
        name='KNOWS',
        fact='Carol knows Dan',
        group_id=group_id,
        created_at=now,
        fact_embedding=[0.1],
    )

    # first search: same-pair duplicate candidates (none); second: graph-wide candidates
    search_mock = AsyncMock(side_effect=[Mock(edges=[]), Mock(edges=[stored, other])])
    seen: dict[str, list[EntityEdge]] = {}

    async def fake_resolve(_llm, edge, related, existing, *_args, **_kwargs):
        seen['existing'] = list(existing)
        # Worst case: every candidate offered for invalidation comes back invalidated.
        return edge, list(existing), []

    saved: dict[str, list[EntityEdge]] = {}

    async def fake_bulk(_driver, _ep, _epe, _nodes, edges, _embedder):
        saved['edges'] = list(edges)

    with (
        patch(
            'graphiti_core.graphiti.EntityNode.get_by_uuid',
            new=AsyncMock(side_effect=[source, target]),
        ),
        patch('graphiti_core.graphiti.EntityEdge.get_by_uuid', new=AsyncMock(return_value=stored)),
        patch('graphiti_core.graphiti.search', new=search_mock),
        patch('graphiti_core.graphiti.resolve_extracted_edge', new=fake_resolve),
        patch('graphiti_core.graphiti.create_entity_edge_embeddings', new=AsyncMock()),
        patch('graphiti_core.graphiti.create_entity_node_embeddings', new=AsyncMock()),
        patch('graphiti_core.graphiti.add_nodes_and_edges_bulk', new=fake_bulk),
    ):
        result = await graphiti.add_triplet(source, incoming, target)

    assert search_mock.await_count == 2
    assert [e.uuid for e in seen['existing']] == ['edge-other']
    assert [e.uuid for e in saved['edges']].count('edge-1') == 1
    assert [e.uuid for e in result.edges].count('edge-1') == 1
    assert [e.fact for e in saved['edges'] if e.uuid == 'edge-1'] == [
        'Alice works with Bob at Acme'
    ]
