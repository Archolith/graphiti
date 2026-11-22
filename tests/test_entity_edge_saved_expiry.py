"""Neo4j entity-edge saves report the expiry they kept onto the in-memory edges (menhir#259).

No database: results are faked with the shapes the drivers return.
"""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from neo4j.time import DateTime as Neo4jDateTime

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.falkordb.operations.entity_edge_ops import FalkorEntityEdgeOperations
from graphiti_core.driver.neo4j.operations.entity_edge_ops import Neo4jEntityEdgeOperations
from graphiti_core.edges import EntityEdge, apply_saved_expiry, entity_edge_save_records
from graphiti_core.models.edges.edge_db_queries import (
    get_entity_edge_save_bulk_query,
    get_entity_edge_save_query,
)

T0 = datetime(2024, 3, 1, tzinfo=timezone.utc)
T1 = datetime(2024, 3, 28, tzinfo=timezone.utc)
T2 = datetime(2024, 4, 9, tzinfo=timezone.utc)
E = datetime(2024, 4, 5, tzinfo=timezone.utc)


def _edge(uuid: str = 'e1', **kw) -> EntityEdge:
    fields = dict(
        uuid=uuid,
        group_id='g',
        source_node_uuid='s',
        target_node_uuid='t',
        created_at=T0,
        name='REL',
        fact='f',
        episodes=['ep'],
        expired_at=None,
        invalid_at=None,
    )
    fields.update(kw)
    return EntityEdge(**fields)


def _rec(uuid='e1', expired_at=E, invalid_at=T1) -> dict:
    return {'uuid': uuid, 'expired_at': expired_at, 'invalid_at': invalid_at}


# ---------------------------------------------------------------- entity_edge_save_records


async def test_records_from_eager_result() -> None:
    recs = [_rec()]
    assert await entity_edge_save_records(SimpleNamespace(records=recs)) == recs


async def test_records_from_tuple() -> None:
    recs = [_rec()]
    assert await entity_edge_save_records((recs, None, None)) == recs


async def test_records_from_async_data() -> None:
    recs = [_rec()]
    obj = SimpleNamespace(data=AsyncMock(return_value=recs))
    assert await entity_edge_save_records(obj) == recs


async def test_records_async_data_non_list_is_empty() -> None:
    obj = SimpleNamespace(data=AsyncMock(return_value=None))
    assert await entity_edge_save_records(obj) == []


@pytest.mark.parametrize(
    'result',
    [MagicMock(), None, SimpleNamespace(data=lambda: [_rec()]), ([],)[1:], 'x'],
    ids=['magicmock', 'none', 'sync-data', 'empty-tuple', 'str'],
)
async def test_records_unrecognised_shapes_are_empty(result) -> None:
    assert await entity_edge_save_records(result) == []


# ---------------------------------------------------------------- apply_saved_expiry


def test_stale_live_edge_gets_stored_expiry_and_logs(caplog) -> None:
    edge = _edge()
    with caplog.at_level(logging.INFO, logger='graphiti_core.edges'):
        apply_saved_expiry([edge], [_rec()])
    assert (edge.expired_at, edge.invalid_at) == (E, T1)
    msgs = [r for r in caplog.records if r.levelno == logging.INFO and 'e1' in r.getMessage()]
    assert len(msgs) == 1


def test_equal_values_change_nothing_and_do_not_log(caplog) -> None:
    edge = _edge(expired_at=E, invalid_at=T1)
    with caplog.at_level(logging.INFO, logger='graphiti_core.edges'):
        apply_saved_expiry([edge], [_rec()])
    assert (edge.expired_at, edge.invalid_at) == (E, T1)
    assert not [r for r in caplog.records if r.name == 'graphiti_core.edges']


def test_naive_and_aware_same_instant_are_equal(caplog) -> None:
    # ensure_utc treats a naive datetime as UTC, so these are the same instant: no restore.
    naive = E.replace(tzinfo=None)
    edge = _edge(expired_at=naive, invalid_at=T1.astimezone(timezone(timedelta(hours=-5))))
    with caplog.at_level(logging.INFO, logger='graphiti_core.edges'):
        apply_saved_expiry([edge], [_rec()])
    assert edge.expired_at == naive  # untouched object, not replaced
    assert not [r for r in caplog.records if r.name == 'graphiti_core.edges']


def test_uuid_without_record_unchanged() -> None:
    edge = _edge('other')
    apply_saved_expiry([edge], [_rec('e1')])
    assert (edge.expired_at, edge.invalid_at) == (None, None)


@pytest.mark.parametrize('missing', ['uuid', 'expired_at', 'invalid_at'])
def test_record_missing_a_key_changes_nothing(missing) -> None:
    rec = _rec()
    del rec[missing]
    edge = _edge()
    other = _edge('e2')
    # a good record first and a bad one after: still nothing is applied
    apply_saved_expiry([edge, other], [_rec('e2'), rec])
    assert (edge.expired_at, edge.invalid_at) == (None, None)
    assert (other.expired_at, other.invalid_at) == (None, None)


def test_neo4j_datetime_values_are_converted() -> None:
    nd_e = Neo4jDateTime(2024, 4, 5, 0, 0, 0, tzinfo=timezone.utc)
    nd_t = Neo4jDateTime(2024, 3, 28, 0, 0, 0, tzinfo=timezone.utc)
    edge = _edge()
    apply_saved_expiry([edge], [_rec(expired_at=nd_e, invalid_at=nd_t)])
    assert isinstance(edge.expired_at, datetime) and isinstance(edge.invalid_at, datetime)
    assert (edge.expired_at, edge.invalid_at) == (E, T1)


def test_stored_live_overrides_in_memory_expired() -> None:
    # the stored values are authoritative in both directions
    edge = _edge(expired_at=E, invalid_at=T1)
    apply_saved_expiry([edge], [_rec(expired_at=None, invalid_at=T2)])
    assert (edge.expired_at, edge.invalid_at) == (None, T2)


def test_copies_of_one_uuid_take_the_last_record() -> None:
    a, b = _edge(), _edge()
    apply_saved_expiry(
        [a, b], [_rec(expired_at=T2, invalid_at=T2), _rec(expired_at=E, invalid_at=T1)]
    )
    for edge in (a, b):
        assert (edge.expired_at, edge.invalid_at) == (E, T1)


# ---------------------------------------------------------------- queries


def test_neo4j_save_queries_return_the_stored_expiry() -> None:
    for q in (
        get_entity_edge_save_query(GraphProvider.NEO4J),
        get_entity_edge_save_bulk_query(GraphProvider.NEO4J),
    ):
        tail = q[q.rindex('RETURN') :]
        assert 'uuid' in tail and 'expired_at AS expired_at' in tail
        assert 'invalid_at AS invalid_at' in tail


# ---------------------------------------------------------------- Neo4j ops


def _eager(*records):
    return SimpleNamespace(records=list(records), summary=None, keys=[])


async def test_ops_save_executor_applies_returned_expiry() -> None:
    edge = _edge()
    executor = MagicMock()
    executor.execute_query = AsyncMock(return_value=_eager(_rec()))
    await Neo4jEntityEdgeOperations().save(executor, edge)
    assert (edge.expired_at, edge.invalid_at) == (E, T1)


async def test_ops_save_tx_applies_returned_expiry() -> None:
    edge = _edge()
    tx = MagicMock()
    tx.run = AsyncMock(return_value=SimpleNamespace(data=AsyncMock(return_value=[_rec()])))
    await Neo4jEntityEdgeOperations().save(MagicMock(), edge, tx=tx)
    assert (edge.expired_at, edge.invalid_at) == (E, T1)


async def test_ops_save_bulk_executor_applies_returned_expiry() -> None:
    a, b, c = _edge('a'), _edge('b'), _edge('c')
    executor = MagicMock()
    executor.execute_query = AsyncMock(
        return_value=_eager(_rec('a'), _rec('b', expired_at=None, invalid_at=T2))
    )
    await Neo4jEntityEdgeOperations().save_bulk(executor, [a, b, c])
    assert (a.expired_at, a.invalid_at) == (E, T1)
    assert (b.expired_at, b.invalid_at) == (None, T2)
    assert (c.expired_at, c.invalid_at) == (None, None)


async def test_ops_save_bulk_tx_applies_returned_expiry() -> None:
    a = _edge('a')
    tx = MagicMock()
    tx.run = AsyncMock(return_value=SimpleNamespace(data=AsyncMock(return_value=[_rec('a')])))
    await Neo4jEntityEdgeOperations().save_bulk(MagicMock(), [a], tx=tx)
    assert (a.expired_at, a.invalid_at) == (E, T1)


# ---------------------------------------------------------------- FalkorDB unaffected


async def test_falkor_save_and_bulk_do_not_raise_on_magicmock_result() -> None:
    ops = FalkorEntityEdgeOperations()
    executor = MagicMock()
    executor.execute_query = AsyncMock(return_value=MagicMock())
    edge = _edge()
    await ops.save(executor, edge)
    await ops.save_bulk(executor, [edge])
    assert (edge.expired_at, edge.invalid_at) == (None, None)
