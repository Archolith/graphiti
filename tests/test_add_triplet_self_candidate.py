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


@pytest.mark.asyncio
@pytest.mark.parametrize('pair_hits', [['stored'], ['stored', 'other']])
async def test_stored_copy_from_the_same_pair_search_is_never_invalidated(
    mock_embedder, pair_hits
) -> None:
    # The common update path: the stored copy shares the endpoints, so the same-pair search
    # returns it, and the LLM marks it contradicted. Real resolve_extracted_edge, LLM stubbed.
    llm = Mock(spec=LLMClient)
    llm.config = Mock()
    ce = Mock(spec=CrossEncoderClient)
    ce.config = Mock()
    graph_driver = Mock()
    graph_driver.__class__ = GraphDriver
    graphiti = Graphiti(
        graph_driver=graph_driver, llm_client=llm, embedder=mock_embedder, cross_encoder=ce
    )
    now = datetime(2024, 3, 1)
    later = datetime(2024, 4, 1)
    source = EntityNode(
        name='Alice', group_id=group_id, labels=['Person'], created_at=now, name_embedding=[0.1]
    )
    target = EntityNode(
        name='Bob', group_id=group_id, labels=['Person'], created_at=now, name_embedding=[0.1]
    )

    def make_edge(uuid: str, fact: str, valid_at: datetime) -> EntityEdge:
        return EntityEdge(
            uuid=uuid,
            source_node_uuid=source.uuid,
            target_node_uuid=target.uuid,
            name='WORKS_WITH',
            fact=fact,
            group_id=group_id,
            created_at=now,
            valid_at=valid_at,
            fact_embedding=[0.1],
            episodes=['ep-0'],
        )

    incoming = make_edge('edge-1', 'Alice works with Bob at Acme', later)
    stored = make_edge('edge-1', 'Alice works with Bob at Initech', now)
    other = make_edge('edge-other', 'Alice manages Bob', now)
    hits = {'stored': stored, 'other': other}
    search_mock = AsyncMock(
        side_effect=[Mock(edges=[hits[h] for h in pair_hits]), Mock(edges=[stored, other])]
    )
    # Every same-pair hit comes back contradicted.
    answer = {'duplicate_facts': [], 'contradicted_facts': list(range(len(pair_hits)))}
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
        patch(
            'graphiti_core.utils.maintenance.edge_operations.generate_prompt_response',
            new=AsyncMock(return_value=answer),
        ),
        patch(
            'graphiti_core.utils.maintenance.edge_operations._extract_edge_timestamps',
            new=AsyncMock(),
        ),
        patch('graphiti_core.graphiti.create_entity_edge_embeddings', new=AsyncMock()),
        patch('graphiti_core.graphiti.create_entity_node_embeddings', new=AsyncMock()),
        patch('graphiti_core.graphiti.add_nodes_and_edges_bulk', new=fake_bulk),
    ):
        result = await graphiti.add_triplet(source, incoming, target)

    own = [e for e in saved['edges'] if e.uuid == 'edge-1']
    assert [e.fact for e in own] == ['Alice works with Bob at Acme']
    assert own[0].expired_at is None
    assert [e.uuid for e in result.edges].count('edge-1') == 1
    if 'other' in pair_hits:
        # A real contradiction from another edge still invalidates it.
        assert [e.uuid for e in saved['edges'] if e.expired_at is not None] == ['edge-other']


@pytest.mark.asyncio
async def test_old_copy_is_still_invalidated_when_the_update_resolves_to_another_edge(
    mock_embedder,
) -> None:
    # The update duplicates a different edge, and the LLM marks the update's own stored copy
    # contradicted. Only the other edge is saved as resolved, so the old copy is expired as
    # upstream; no uuid reaches the save twice.
    llm = Mock(spec=LLMClient)
    llm.config = Mock()
    ce = Mock(spec=CrossEncoderClient)
    ce.config = Mock()
    graph_driver = Mock()
    graph_driver.__class__ = GraphDriver
    graphiti = Graphiti(
        graph_driver=graph_driver, llm_client=llm, embedder=mock_embedder, cross_encoder=ce
    )
    now = datetime(2024, 3, 1)
    later = datetime(2024, 4, 1)
    source = EntityNode(
        name='Alice', group_id=group_id, labels=['Person'], created_at=now, name_embedding=[0.1]
    )
    target = EntityNode(
        name='Bob', group_id=group_id, labels=['Person'], created_at=now, name_embedding=[0.1]
    )

    def make_edge(uuid: str, fact: str, valid_at: datetime) -> EntityEdge:
        return EntityEdge(
            uuid=uuid,
            source_node_uuid=source.uuid,
            target_node_uuid=target.uuid,
            name='WORKS_WITH',
            fact=fact,
            group_id=group_id,
            created_at=now,
            valid_at=valid_at,
            fact_embedding=[0.1],
            episodes=['ep-0'],
        )

    incoming = make_edge('edge-1', 'Alice works with Bob at Acme', later)
    stored = make_edge('edge-1', 'Alice works with Bob at Initech', now)
    other = make_edge('edge-other', 'Alice and Bob work together at Acme', later)
    search_mock = AsyncMock(side_effect=[Mock(edges=[other, stored]), Mock(edges=[])])
    answer = {'duplicate_facts': [0], 'contradicted_facts': [1]}
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
        patch(
            'graphiti_core.utils.maintenance.edge_operations.generate_prompt_response',
            new=AsyncMock(return_value=answer),
        ),
        patch(
            'graphiti_core.utils.maintenance.edge_operations._extract_edge_timestamps',
            new=AsyncMock(),
        ),
        patch('graphiti_core.graphiti.create_entity_edge_embeddings', new=AsyncMock()),
        patch('graphiti_core.graphiti.create_entity_node_embeddings', new=AsyncMock()),
        patch('graphiti_core.graphiti.add_nodes_and_edges_bulk', new=fake_bulk),
    ):
        await graphiti.add_triplet(source, incoming, target)

    uuids = [e.uuid for e in saved['edges']]
    assert uuids == ['edge-other', 'edge-1']
    old = saved['edges'][1]
    assert old.fact == 'Alice works with Bob at Initech'
    assert old.expired_at is not None
