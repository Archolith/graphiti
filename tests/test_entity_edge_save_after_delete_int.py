"""A stale save of a loaded edge never recreates an edge another writer deleted (menhir#261).

Writer A loads an edge; writer B deletes it; A saves its stale copy. Upstream's MERGE recreated
the edge. The edge now skips the save when it was loaded from the store and no stored copy is left.

Needs a disposable Neo4j: set GRAPHITI_SAVE_TEST_NEO4J_URI (no default, so this never runs
against a shared instance). Optional GRAPHITI_SAVE_TEST_NEO4J_USER / _PASSWORD.
"""

import asyncio
import logging
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
OWN_END = datetime(2024, 4, 1, tzinfo=timezone.utc)
TRUNCATED = datetime(2024, 3, 28, tzinfo=timezone.utc)
EXPIRED = datetime(2024, 4, 5, tzinfo=timezone.utc)
EMBEDDING = [0.1, 0.2, 0.3]
WAIT = 30


@pytest.fixture
def group() -> str:
    return f'save-delete-{uuid4().hex}'


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
        uuid='edge-' + uuid4().hex,
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
    )
    fields.update(overrides)
    return EntityEdge(**fields)


async def _count(driver, uuid: str) -> int:
    records, _, _ = await driver.execute_query(
        'MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() RETURN count(e) AS n', uuid=uuid
    )
    return records[0]['n']


async def _props(driver, uuid: str) -> dict:
    records, _, _ = await driver.execute_query(
        'MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() RETURN properties(e) AS props', uuid=uuid
    )
    assert len(records) == 1
    props = dict(records[0]['props'])
    for key in ('expired_at', 'invalid_at'):
        if props.get(key) is not None:
            props[key] = props[key].to_native()
    return props


async def _delete(driver, uuid: str) -> None:
    await driver.execute_query('MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() DELETE e', uuid=uuid)


async def _save_single(edge: EntityEdge, driver) -> None:
    await edge.save(driver)


async def _save_ops(edge: EntityEdge, driver) -> None:
    await Neo4jEntityEdgeOperations().save(driver, edge)


async def _save_ops_bulk(edge: EntityEdge, driver) -> None:
    await Neo4jEntityEdgeOperations().save_bulk(driver, [edge])


async def _save_add_bulk(edge: EntityEdge, driver) -> None:
    await add_nodes_and_edges_bulk(driver, [], [], [], [edge], MagicMock())


SAVES = pytest.mark.parametrize(
    'save',
    [_save_single, _save_ops, _save_ops_bulk, _save_add_bulk],
    ids=['edge.save', 'ops.save', 'ops.save_bulk', 'add_nodes_and_edges_bulk'],
)


@SAVES
async def test_loaded_edge_given_new_uuid_is_written(driver, group, save) -> None:
    # add_triplet's endpoint-mismatch branch gives a loaded edge uuid4(); it must be created.
    _, _, _, loaded = await _saved_loaded(driver, group)
    loaded.uuid = 'edge-' + uuid4().hex
    await save(loaded, driver)
    assert await _count(driver, loaded.uuid) == 1
    copy = (await _saved_loaded(driver, group))[3].model_copy(
        update={'uuid': 'edge-' + uuid4().hex}
    )
    await save(copy, driver)
    assert await _count(driver, copy.uuid) == 1


async def _saved_loaded(
    driver, group: str
) -> tuple[EntityNode, EntityNode, EntityEdge, EntityEdge]:
    user, rome = await _nodes(driver, group)
    original = _edge(user, rome)
    await original.save(driver)
    loaded = await EntityEdge.get_by_uuid(driver, original.uuid)
    await loaded.load_fact_embedding(driver)  # get_by_uuid does not return the vector
    assert loaded._loaded_from_store is True
    assert loaded.fact_embedding is not None
    return user, rome, original, loaded


@SAVES
@pytest.mark.asyncio
async def test_stale_save_after_delete_does_not_recreate(driver, group, save, caplog) -> None:
    _, _, _, loaded = await _saved_loaded(driver, group)
    await _delete(driver, loaded.uuid)
    assert await _count(driver, loaded.uuid) == 0

    with caplog.at_level(logging.INFO):
        await save(loaded, driver)  # no exception

    assert await _count(driver, loaded.uuid) == 0
    assert any(
        r.levelno == logging.INFO and loaded.uuid in r.getMessage() for r in caplog.records
    ), 'no INFO log names the skipped uuid'


@SAVES
@pytest.mark.asyncio
async def test_loaded_edge_not_deleted_applies_change_and_leaves_no_lock(
    driver, group, save
) -> None:
    _, _, _, loaded = await _saved_loaded(driver, group)
    loaded.fact = 'The user lived in Rome.'

    await save(loaded, driver)

    props = await _props(driver, loaded.uuid)
    assert props['fact'] == 'The user lived in Rome.'
    assert '_graphiti_save_lock' not in props


@SAVES
@pytest.mark.asyncio
async def test_loaded_edge_expired_meanwhile_stays_expired(driver, group, save) -> None:
    _, _, _, loaded = await _saved_loaded(driver, group)
    await driver.execute_query(
        'MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() '
        'SET e.expired_at = $expired_at, e.invalid_at = $invalid_at',
        uuid=loaded.uuid,
        expired_at=EXPIRED,
        invalid_at=TRUNCATED,
    )
    assert loaded.expired_at is None  # stale-live

    await save(loaded, driver)

    props = await _props(driver, loaded.uuid)
    assert (props['expired_at'], props['invalid_at']) == (EXPIRED, TRUNCATED)
    assert '_graphiti_save_lock' not in props
    assert loaded.expired_at == EXPIRED
    assert loaded.invalid_at == TRUNCATED


@pytest.mark.asyncio
async def test_add_nodes_and_edges_bulk_mixed_batch(driver, group) -> None:
    user, rome, _, loaded = await _saved_loaded(driver, group)
    new = _edge(user, rome)
    await _delete(driver, loaded.uuid)

    await add_nodes_and_edges_bulk(driver, [], [], [], [new, loaded], MagicMock())

    assert await _count(driver, new.uuid) == 1
    assert await _count(driver, loaded.uuid) == 0
    assert '_graphiti_save_lock' not in await _props(driver, new.uuid)


@pytest.mark.asyncio
async def test_ops_save_bulk_mixed_batch(driver, group) -> None:
    user, rome, _, loaded = await _saved_loaded(driver, group)
    new = _edge(user, rome)
    await _delete(driver, loaded.uuid)

    await Neo4jEntityEdgeOperations().save_bulk(driver, [loaded, new])

    assert await _count(driver, new.uuid) == 1
    assert await _count(driver, loaded.uuid) == 0


# ---------------------------------------------------------------------- race


async def _race(driver, group: str, save, as_new: bool) -> tuple[Any, int]:
    """T1 holds the stored relationship's lock; the stale save starts and must block; T1 deletes
    the relationship and commits. Returns (exception or None, relationships left)."""
    user, rome, original, loaded = await _saved_loaded(driver, group)
    stale = EntityEdge(**loaded.model_dump()) if as_new else loaded
    assert stale._loaded_from_store is (not as_new)

    outcome: Any = None
    async with driver.client.session() as session:
        tx = await session.begin_transaction()
        await tx.run('MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() SET e.held = true', uuid=stale.uuid)
        task = asyncio.create_task(save(stale, driver))
        await asyncio.sleep(2)
        assert not task.done(), 'the save did not block on the held relationship'
        await tx.run('MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() DELETE e', uuid=stale.uuid)
        await tx.commit()
    try:
        await asyncio.wait_for(task, timeout=WAIT)
    except Exception as exc:  # recorded and printed; the caller asserts on the outcome
        outcome = exc
    return outcome, await _count(driver, stale.uuid)


@SAVES
@pytest.mark.asyncio
async def test_race_delete_commits_while_stale_save_waits_on_lock(driver, group, save) -> None:
    outcome, left = await _race(driver, group, save, as_new=False)
    print(
        f'\nRACE loaded edge: outcome={type(outcome).__name__ if outcome else "returned"}: {outcome!r}'
    )
    assert left == 0  # raised or returned, the edge is not recreated


@SAVES
@pytest.mark.asyncio
async def test_race_control_new_edge_object_of_same_uuid(driver, group, save) -> None:
    # Control: documents upstream MERGE behaviour for an edge that is not marked as loaded.
    outcome, left = await _race(driver, group, save, as_new=True)
    print(
        f'\nRACE control new edge: outcome={type(outcome).__name__ if outcome else "returned"}: {outcome!r} left={left}'
    )
    # Observed: the plain MERGE save fails once the lock holder deletes the relationship (the
    # transaction hits a deleted entity); the edge is not recreated here either. The recreation
    # upstream shows is the non-racing case (delete committed before the save), which the
    # counterexample tests above cover for loaded edges.
    assert left == 0
    assert outcome is None or type(outcome).__name__ == 'ClientError', repr(outcome)
