"""A default-group ('') edge search sees only default-group edges, on a real Neo4j (fork).

Counterexample for upstream's [''] -> None: the same fact stored in '' and in another group;
the search that resolve_extracted_edges runs for a default-group edge (EDGE_HYBRID_SEARCH_RRF,
group_ids=[edge.group_id]) must not return the other group's edge, or that edge can be judged a
duplicate of, or invalidated by, a default-group episode.

Needs a disposable Neo4j: set GRAPHITI_SAVE_TEST_NEO4J_URI (same gate as the other *_int tests).
"""

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from graphiti_core.search.search import search
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
from graphiti_core.search.search_filters import SearchFilters

URI = os.getenv('GRAPHITI_SAVE_TEST_NEO4J_URI')
pytestmark = pytest.mark.skipif(not URI, reason='GRAPHITI_SAVE_TEST_NEO4J_URI not set')

NOW = datetime(2024, 3, 1, tzinfo=timezone.utc)
EMBEDDING = [0.1, 0.2, 0.3]
FACT = 'Acme stores session state in Redis'


@pytest.fixture
async def driver():
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    drv = Neo4jDriver(
        uri=URI or '',
        user=os.getenv('GRAPHITI_SAVE_TEST_NEO4J_USER', 'neo4j'),
        password=os.getenv('GRAPHITI_SAVE_TEST_NEO4J_PASSWORD', 'testpassword'),
    )
    await drv.build_indices_and_constraints()
    created: list[str] = []
    drv.created_uuids = created  # type: ignore[attr-defined]
    try:
        yield drv
    finally:
        await drv.execute_query('MATCH (n) WHERE n.uuid IN $u DETACH DELETE n', u=created)
        await drv.close()


async def _edge(driver, group: str) -> EntityEdge:
    acme = EntityNode(name='Acme', group_id=group, labels=['Entity'], created_at=NOW)
    redis = EntityNode(name='Redis', group_id=group, labels=['Entity'], created_at=NOW)
    for node in (acme, redis):
        node.name_embedding = EMBEDDING
        await node.save(driver)
        driver.created_uuids.append(node.uuid)
    edge = EntityEdge(
        group_id=group,
        source_node_uuid=acme.uuid,
        target_node_uuid=redis.uuid,
        name='STORES_STATE_IN',
        fact=FACT,
        fact_embedding=EMBEDDING,
        episodes=[],
        created_at=NOW,
        valid_at=NOW,
    )
    await edge.save(driver)
    return edge


async def test_default_group_search_excludes_other_groups(driver):
    default_edge = await _edge(driver, '')
    other_edge = await _edge(driver, f'other-{uuid4().hex[:8]}')
    clients = MagicMock()
    clients.driver = driver
    clients.max_coroutines = None

    def found(group_ids):
        return search(
            clients,
            FACT,
            group_ids,
            EDGE_HYBRID_SEARCH_RRF,
            SearchFilters(),
            query_vector=EMBEDDING,
        )

    default_uuids = {e.uuid for e in (await found([''])).edges}
    assert default_edge.uuid in default_uuids
    assert other_edge.uuid not in default_uuids

    mixed_uuids = {e.uuid for e in (await found(['', other_edge.group_id])).edges}
    assert {default_edge.uuid, other_edge.uuid} <= mixed_uuids

    other_uuids = {e.uuid for e in (await found([other_edge.group_id])).edges}
    assert other_uuids >= {other_edge.uuid}
    assert default_edge.uuid not in other_uuids
