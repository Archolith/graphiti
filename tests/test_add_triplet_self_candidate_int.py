"""add_triplet updating an edge by its uuid keeps the new fact (menhir#251), on live Neo4j.

Real Neo4j search (the same-pair search returns the edge's stored copy), real resolution and
save; only the LLM answer is stubbed to mark every candidate contradicted.

Needs a disposable Neo4j: set GRAPHITI_SAVE_TEST_NEO4J_URI (no default, so this never runs
against a shared instance). Optional GRAPHITI_SAVE_TEST_NEO4J_USER / _PASSWORD.
"""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.graphiti import Graphiti
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode

URI = os.getenv('GRAPHITI_SAVE_TEST_NEO4J_URI')
pytestmark = pytest.mark.skipif(not URI, reason='GRAPHITI_SAVE_TEST_NEO4J_URI not set')

EARLY = datetime(2024, 3, 1, tzinfo=timezone.utc)
LATER = datetime(2024, 4, 1, tzinfo=timezone.utc)
EMBEDDING = [0.1, 0.2, 0.3]


class _FixedEmbedder(EmbedderClient):
    async def create(self, input_data):  # type: ignore[override]
        return EMBEDDING

    async def create_batch(self, input_data_list):  # type: ignore[override]
        return [EMBEDDING for _ in input_data_list]


@pytest.fixture
def group() -> str:
    return f'triplet-self-{uuid4().hex}'


@pytest.fixture
async def graphiti(group):
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    driver = Neo4jDriver(
        uri=URI or '',
        user=os.getenv('GRAPHITI_SAVE_TEST_NEO4J_USER', 'neo4j'),
        password=os.getenv('GRAPHITI_SAVE_TEST_NEO4J_PASSWORD', 'testpassword'),
    )
    llm = Mock(spec=LLMClient)
    llm.config = Mock()
    ce = Mock(spec=CrossEncoderClient)
    ce.config = Mock()
    g = Graphiti(graph_driver=driver, llm_client=llm, embedder=_FixedEmbedder(), cross_encoder=ce)
    await g.build_indices_and_constraints()
    try:
        yield g
    finally:
        await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)
        await driver.close()


def _edge(source: EntityNode, target: EntityNode, uuid: str, fact: str, valid_at) -> EntityEdge:
    return EntityEdge(
        uuid=uuid,
        group_id=source.group_id,
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        created_at=EARLY,
        name='LIVES_IN',
        fact=fact,
        fact_embedding=EMBEDDING,
        episodes=['ep-1'],
        valid_at=valid_at,
    )


async def _stored(driver, uuid: str) -> list[dict]:
    records, _, _ = await driver.execute_query(
        'MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() RETURN e.fact AS fact, e.expired_at AS expired',
        uuid=uuid,
    )
    return [dict(r) for r in records]


async def test_update_by_uuid_keeps_new_fact_when_llm_contradicts_stored_copy(
    graphiti, group
) -> None:
    driver = graphiti.driver
    user = EntityNode(name='User', group_id=group, labels=['Entity'], created_at=EARLY)
    city = EntityNode(name='City', group_id=group, labels=['Entity'], created_at=EARLY)
    for node in (user, city):
        node.name_embedding = EMBEDDING
        await node.save(driver)
    uuid = 'edge-' + uuid4().hex
    await _edge(user, city, uuid, 'The user lives in Rome.', EARLY).save(driver)

    search_calls: list[list[str]] = []
    from graphiti_core import graphiti as graphiti_module

    real_search = graphiti_module.search

    async def recording_search(*args, **kwargs):
        result = await real_search(*args, **kwargs)
        search_calls.append([e.uuid for e in result.edges])
        return result

    incoming = _edge(user, city, uuid, 'The user lives in Milan.', LATER)
    answer = {'duplicate_facts': [], 'contradicted_facts': [0]}
    with (
        patch('graphiti_core.graphiti.search', new=recording_search),
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
        # get_by_uuid does not load name_embedding; the node save needs one.
        patch(
            'graphiti_core.graphiti.EntityNode.get_by_uuid',
            new=AsyncMock(side_effect=[user.model_copy(), city.model_copy()]),
        ),
    ):
        result = await graphiti.add_triplet(user, incoming, city)

    # The real same-pair search returned the stored copy: this is the path under test.
    assert search_calls[0] == [uuid]
    assert await _stored(driver, uuid) == [{'fact': 'The user lives in Milan.', 'expired': None}]
    own = [e for e in result.edges if e.uuid == uuid]
    assert len(own) == 1
    assert own[0].fact == 'The user lives in Milan.'
    assert own[0].expired_at is None
