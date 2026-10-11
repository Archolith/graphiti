"""A Neo4j entity-edge save never makes a stored expired edge live again (Menhir P3c-1).

Writer A fetches a live edge; writer B expires it (a contradiction truncates invalid_at);
A then saves its stale copy. Before the fix A's full-replace save wrote expired_at null and
its own invalid_at, so the edge came back live.

Needs a disposable Neo4j: set GRAPHITI_SAVE_TEST_NEO4J_URI (no default, so this never runs
against a shared instance). Optional GRAPHITI_SAVE_TEST_NEO4J_USER / _PASSWORD.
"""

import asyncio
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.neo4j.operations.entity_edge_ops import Neo4jEntityEdgeOperations
from graphiti_core.edges import EntityEdge
from graphiti_core.models.edges.edge_db_queries import get_entity_edge_save_query
from graphiti_core.nodes import EntityNode

URI = os.getenv('GRAPHITI_SAVE_TEST_NEO4J_URI')
pytestmark = pytest.mark.skipif(not URI, reason='GRAPHITI_SAVE_TEST_NEO4J_URI not set')

CREATED = datetime(2024, 3, 1, tzinfo=timezone.utc)
VALID = datetime(2024, 3, 27, tzinfo=timezone.utc)
OWN_END = datetime(2024, 4, 1, tzinfo=timezone.utc)
TRUNCATED = datetime(2024, 3, 28, tzinfo=timezone.utc)
EXPIRED = datetime(2024, 4, 5, tzinfo=timezone.utc)
LATER_EXPIRED = datetime(2024, 4, 9, tzinfo=timezone.utc)
EMBEDDING = [0.1, 0.2, 0.3]


@pytest.fixture
def group() -> str:
    return f'save-expiry-{uuid4().hex}'


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
        invalid_at=OWN_END,
        expired_at=None,
        attributes={'time_expiry': 'world_end', 'time_world_end': OWN_END.isoformat()},
    )
    fields.update(overrides)
    return EntityEdge(**fields)


async def _stored(driver, uuid: str) -> dict:
    records, _, _ = await driver.execute_query(
        'MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() RETURN properties(e) AS props', uuid=uuid
    )
    assert len(records) == 1
    props = dict(records[0]['props'])
    for key in ('expired_at', 'invalid_at', 'valid_at', 'created_at'):
        if props.get(key) is not None:
            props[key] = props[key].to_native()
    return props


def _expired_copy(edge: EntityEdge) -> EntityEdge:
    return edge.model_copy(update={'invalid_at': TRUNCATED, 'expired_at': EXPIRED})


async def _save_single(edge: EntityEdge, driver) -> None:
    await edge.save(driver)


async def _save_bulk(edge: EntityEdge, driver) -> None:
    await Neo4jEntityEdgeOperations().save_bulk(driver, [edge])


SAVES = pytest.mark.parametrize('save', [_save_single, _save_bulk], ids=['single', 'bulk'])


@SAVES
@pytest.mark.asyncio
async def test_stale_live_save_keeps_the_stored_expiry(driver, group, save) -> None:
    user, rome = await _nodes(driver, group)
    live = _edge(user, rome)
    await save(live, driver)
    stale = live.model_copy(update={'episodes': ['ep-1', 'ep-2']})  # A fetched before B

    await save(_expired_copy(live), driver)  # B: contradiction truncates and expires
    await save(stale, driver)  # A: stale full-replace save

    props = await _stored(driver, live.uuid)
    assert props['expired_at'] == EXPIRED
    assert props['invalid_at'] == TRUNCATED
    assert props['episodes'] == ['ep-1', 'ep-2']  # the rest of A's save still applies
    assert props['time_world_end'] == OWN_END.isoformat()
    assert '_graphiti_save_lock' not in props


@SAVES
@pytest.mark.asyncio
async def test_saves_that_carry_an_expiry_are_unchanged(driver, group, save) -> None:
    user, rome = await _nodes(driver, group)
    live = _edge(user, rome)
    await save(live, driver)
    props = await _stored(driver, live.uuid)
    assert props.get('expired_at') is None and props['invalid_at'] == OWN_END
    assert '_graphiti_save_lock' not in props

    await save(_expired_copy(live), driver)  # live -> expired: written as given
    props = await _stored(driver, live.uuid)
    assert (props['expired_at'], props['invalid_at']) == (EXPIRED, TRUNCATED)

    # expired -> expired: last writer wins, as before
    await save(live.model_copy(update={'invalid_at': OWN_END, 'expired_at': LATER_EXPIRED}), driver)
    props = await _stored(driver, live.uuid)
    assert (props['expired_at'], props['invalid_at']) == (LATER_EXPIRED, OWN_END)


@pytest.mark.asyncio
async def test_live_save_waits_for_a_concurrent_expiry_and_keeps_it(driver, group) -> None:
    # B holds an uncommitted expiry. A's save must take the lock before reading the stored
    # expiry; reading first would see the pre-B value and write the edge back live.
    user, rome = await _nodes(driver, group)
    live = _edge(user, rome)
    await live.save(driver)

    async with driver.client.session() as session:
        tx = await session.begin_transaction()
        await tx.run(
            'MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() '
            'SET e.expired_at = $expired_at, e.invalid_at = $invalid_at',
            uuid=live.uuid,
            expired_at=EXPIRED,
            invalid_at=TRUNCATED,
        )
        stale_save = asyncio.create_task(live.save(driver))
        await asyncio.sleep(1.5)
        assert not stale_save.done(), 'the save did not wait for the uncommitted expiry'
        await tx.commit()
    await asyncio.wait_for(stale_save, timeout=30)

    props = await _stored(driver, live.uuid)
    assert (props['expired_at'], props['invalid_at']) == (EXPIRED, TRUNCATED)


@pytest.mark.asyncio
async def test_new_edge_is_created_as_given(driver, group) -> None:
    user, rome = await _nodes(driver, group)
    fresh = _edge(user, rome, uuid='fresh-' + user.group_id)
    await driver.execute_query(
        get_entity_edge_save_query(GraphProvider.NEO4J),
        edge_data={
            'uuid': fresh.uuid,
            'source_uuid': user.uuid,
            'target_uuid': rome.uuid,
            'group_id': fresh.group_id,
            'fact': fresh.fact,
            'fact_embedding': EMBEDDING,
            'episodes': fresh.episodes,
            'created_at': CREATED,
            'expired_at': None,
            'valid_at': VALID,
            'invalid_at': OWN_END,
        },
    )
    props = await _stored(driver, fresh.uuid)
    assert props.get('expired_at') is None and props['invalid_at'] == OWN_END
    assert '_graphiti_save_lock' not in props
