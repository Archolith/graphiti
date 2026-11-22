"""Neo4j entity-edge saves keep core fields and merge unreconciled copies (live).

menhir#260: the driver ops row had no reference_time, so a save erased a stored one.
Same-named attributes replaced core fields via dict.update.
menhir#262: unreconciled copies of one edge in a batch left the stored invalid_at to row order.

Needs a disposable Neo4j: set GRAPHITI_SAVE_TEST_NEO4J_URI (no default, so this never runs
against a shared instance). Optional GRAPHITI_SAVE_TEST_NEO4J_USER / _PASSWORD.
"""

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock
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
REF = datetime(2024, 3, 30, tzinfo=timezone.utc)
T1 = datetime(2024, 3, 28, tzinfo=timezone.utc)
T2 = datetime(2024, 4, 2, tzinfo=timezone.utc)
EXPIRED = datetime(2024, 4, 5, tzinfo=timezone.utc)
EMBEDDING = [0.1, 0.2, 0.3]


@pytest.fixture
def group() -> str:
    return f'save-rows-{uuid4().hex}'


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
        reference_time=REF,
        attributes={},
    )
    fields.update(overrides)
    return EntityEdge(**fields)


async def _stored(driver, uuid: str) -> dict:
    records, _, _ = await driver.execute_query(
        'MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() RETURN properties(e) AS props', uuid=uuid
    )
    assert len(records) == 1
    props = dict(records[0]['props'])
    for key in ('expired_at', 'invalid_at', 'valid_at', 'created_at', 'reference_time'):
        value = props.get(key)
        if value is not None and hasattr(value, 'to_native'):
            props[key] = value.to_native()
    return props


async def _save_ops_single(edge: EntityEdge, driver) -> None:
    await Neo4jEntityEdgeOperations().save(driver, edge)


async def _save_ops_bulk(edge: EntityEdge, driver) -> None:
    await Neo4jEntityEdgeOperations().save_bulk(driver, [edge])


async def _save_bulk_tx(edge: EntityEdge, driver) -> None:
    await add_nodes_and_edges_bulk(driver, [], [], [], [edge], Mock())


SAVES = pytest.mark.parametrize(
    'save',
    [_save_ops_single, _save_ops_bulk, _save_bulk_tx],
    ids=['ops-save', 'ops-bulk', 'bulk-tx'],
)


@SAVES
@pytest.mark.asyncio
async def test_save_keeps_reference_time(driver, group, save) -> None:
    # Old driver-ops rows had no reference_time, so the full-replace save erased it.
    user, rome = await _nodes(driver, group)
    edge = _edge(user, rome)

    await save(edge, driver)
    props = await _stored(driver, edge.uuid)
    assert props['reference_time'] == REF

    await save(edge.model_copy(update={'episodes': ['ep-1', 'ep-2']}), driver)  # a resave
    props = await _stored(driver, edge.uuid)
    assert props['reference_time'] == REF
    assert props['episodes'] == ['ep-1', 'ep-2']


@SAVES
@pytest.mark.asyncio
async def test_attribute_named_like_a_core_field_does_not_replace_it(driver, group, save) -> None:
    # Old driver rows did dict.update(attributes), so stale same-named attributes won.
    user, rome = await _nodes(driver, group)
    edge = _edge(
        user,
        rome,
        attributes={
            'reference_time': '2020-01-01T00:00:00+00:00',
            'fact': 'stale fact',
            'name': 'STALE',
            'colour': 'red',
        },
    )

    await save(edge, driver)

    props = await _stored(driver, edge.uuid)
    assert props['reference_time'] == REF
    assert props['fact'] == 'The user was in Rome.'
    assert props['name'] == 'VISITED'
    assert props['colour'] == 'red'  # ordinary attributes are still written


BATCH_SAVES = pytest.mark.parametrize(
    'save_batch',
    [
        lambda edges, driver: Neo4jEntityEdgeOperations().save_bulk(driver, edges),
        lambda edges, driver: add_nodes_and_edges_bulk(driver, [], [], [], edges, Mock()),
    ],
    ids=['ops-bulk', 'bulk-tx'],
)


@pytest.mark.parametrize('later_invalid', [T2, None], ids=['later-end', 'no-end'])
@pytest.mark.parametrize('order', ['expiring-first', 'other-first'])
@BATCH_SAVES
@pytest.mark.asyncio
async def test_unreconciled_copies_in_one_batch_keep_the_earliest_expiry(
    driver, group, save_batch, order, later_invalid
) -> None:
    # menhir#262: stored edge is live; one copy expires it (invalid_at T1), the other carries a
    # later invalid_at (or none). The stored result must not depend on row order.
    user, rome = await _nodes(driver, group)
    live = _edge(user, rome)
    await _save_ops_single(live, driver)
    assert (await _stored(driver, live.uuid)).get('expired_at') is None

    expiring = live.model_copy(update={'invalid_at': T1, 'expired_at': EXPIRED})
    other = live.model_copy(update={'invalid_at': later_invalid, 'expired_at': None})
    batch = [expiring, other] if order == 'expiring-first' else [other, expiring]

    await save_batch(batch, driver)

    props = await _stored(driver, live.uuid)
    assert props['expired_at'] == EXPIRED
    assert props['invalid_at'] == T1
