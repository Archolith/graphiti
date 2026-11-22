"""A Neo4j edge save reports the expiry it kept onto the in-memory edge (menhir#259).

A stored edge is expired (expired_at E, invalid_at T1); a stale live in-memory copy is saved
over it. The store keeps E/T1, and the caller's edge object must now say so too.

Needs a disposable Neo4j: set GRAPHITI_SAVE_TEST_NEO4J_URI (same gate as
test_entity_edge_save_keeps_expiry_int.py). Optional _USER / _PASSWORD.
"""

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from graphiti_core.driver.neo4j.operations.entity_edge_ops import Neo4jEntityEdgeOperations
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk

URI = os.getenv('GRAPHITI_SAVE_TEST_NEO4J_URI')
pytestmark = pytest.mark.skipif(not URI, reason='GRAPHITI_SAVE_TEST_NEO4J_URI not set')

CREATED = datetime(2024, 3, 1, tzinfo=timezone.utc)
VALID = datetime(2024, 3, 27, tzinfo=timezone.utc)
T1 = datetime(2024, 3, 28, tzinfo=timezone.utc)
T2 = datetime(2024, 4, 9, tzinfo=timezone.utc)
E = datetime(2024, 4, 5, tzinfo=timezone.utc)
EMBEDDING = [0.1, 0.2, 0.3]


@pytest.fixture
def group() -> str:
    return f'saved-expiry-{uuid4().hex}'


@pytest.fixture
async def driver(group):
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    drv = Neo4jDriver(
        uri=URI or '',
        user=os.getenv('GRAPHITI_SAVE_TEST_NEO4J_USER', 'neo4j'),
        password=os.getenv('GRAPHITI_SAVE_TEST_NEO4J_PASSWORD', 'testpassword'),
    )
    try:
        yield drv
    finally:
        await drv.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)
        await drv.close()


async def _nodes(driver, group: str) -> tuple[EntityNode, EntityNode]:
    user = EntityNode(name='User', group_id=group, labels=['Entity'], created_at=CREATED)
    rome = EntityNode(name='Rome', group_id=group, labels=['Entity'], created_at=CREATED)
    for node in (user, rome):
        node.name_embedding = EMBEDDING
        await node.save(driver)
    return user, rome


def _edge(source: EntityNode, target: EntityNode, **overrides) -> EntityEdge:
    fields: dict[str, Any] = dict(
        uuid='edge-' + source.group_id,
        group_id=source.group_id,
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        created_at=CREATED,
        name='VISITED',
        fact='The user was in Rome.',
        fact_embedding=EMBEDDING,
        episodes=['ep-1'],
        valid_at=VALID,
        invalid_at=None,
        expired_at=None,
    )
    fields.update(overrides)
    return EntityEdge(**fields)


async def _stored(driver, uuid: str) -> tuple[Any, Any]:
    records, _, _ = await driver.execute_query(
        'MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() RETURN e.expired_at AS x, e.invalid_at AS i',
        uuid=uuid,
    )
    assert len(records) == 1
    x, i = records[0]['x'], records[0]['i']
    return (x.to_native() if x is not None else None, i.to_native() if i is not None else None)


async def _save_single(edge: EntityEdge, driver, user, rome) -> None:
    await edge.save(driver)


async def _save_ops(edge: EntityEdge, driver, user, rome) -> None:
    await Neo4jEntityEdgeOperations().save(driver, edge)


async def _save_ops_bulk(edge: EntityEdge, driver, user, rome) -> None:
    await Neo4jEntityEdgeOperations().save_bulk(driver, [edge])


async def _save_bulk_tx(edge: EntityEdge, driver, user, rome) -> None:
    await add_nodes_and_edges_bulk(driver, [], [], [user, rome], [edge], MagicMock())


SAVES = pytest.mark.parametrize(
    'save',
    [_save_single, _save_ops, _save_ops_bulk, _save_bulk_tx],
    ids=['edge.save', 'ops.save', 'ops.save_bulk', 'add_nodes_and_edges_bulk'],
)


@SAVES
@pytest.mark.parametrize('stale_invalid_at', [None, T2], ids=['no-end', 'later-end'])
@pytest.mark.asyncio
async def test_stale_live_save_reports_the_stored_expiry(
    driver, group, save, stale_invalid_at
) -> None:
    user, rome = await _nodes(driver, group)
    base = _edge(user, rome)
    await _save_single(
        base.model_copy(update={'expired_at': E, 'invalid_at': T1}), driver, user, rome
    )
    assert await _stored(driver, base.uuid) == (E, T1)

    stale = base.model_copy(update={'expired_at': None, 'invalid_at': stale_invalid_at})
    await save(stale, driver, user, rome)

    assert await _stored(driver, base.uuid) == (E, T1)
    assert (stale.expired_at, stale.invalid_at) == (E, T1)


@SAVES
@pytest.mark.asyncio
async def test_saving_over_a_live_stored_edge_leaves_the_edge_as_given(driver, group, save) -> None:
    user, rome = await _nodes(driver, group)
    base = _edge(user, rome, invalid_at=T2)
    await _save_single(base, driver, user, rome)
    assert await _stored(driver, base.uuid) == (None, T2)

    again = base.model_copy(update={'episodes': ['ep-1', 'ep-2']})
    await save(again, driver, user, rome)

    assert await _stored(driver, base.uuid) == (None, T2)
    assert (again.expired_at, again.invalid_at) == (None, T2)
