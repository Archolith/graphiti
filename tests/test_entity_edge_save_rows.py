"""Entity-edge save rows (offline): every save path builds its row with entity_edge_save_row.

Pins three Archolith fork fixes:
- menhir#260: driver ops omitted reference_time from the row (full-replace save erased it).
- menhir#262: unreconciled copies of one edge in a batch left the stored expiry to row order.
- core fields must never be replaced by same-named attributes (stale string copies).

The stored-state behaviour is in tests/test_entity_edge_save_rows_int.py.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from graphiti_core.driver.driver import GraphDriver, GraphProvider
from graphiti_core.driver.falkordb.operations.entity_edge_ops import FalkorEntityEdgeOperations
from graphiti_core.driver.neo4j.operations.entity_edge_ops import Neo4jEntityEdgeOperations
from graphiti_core.driver.neptune.operations.entity_edge_ops import NeptuneEntityEdgeOperations
from graphiti_core.edges import EntityEdge, entity_edge_save_row
from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk_tx

CREATED = datetime(2024, 3, 1, tzinfo=timezone.utc)
VALID = datetime(2024, 3, 27, tzinfo=timezone.utc)
REF = datetime(2024, 3, 30, tzinfo=timezone.utc)
T_INVALID = datetime(2024, 3, 28, tzinfo=timezone.utc)
T_EXPIRED = datetime(2024, 4, 5, tzinfo=timezone.utc)

OPS = [
    pytest.param(Neo4jEntityEdgeOperations, id='neo4j'),
    pytest.param(FalkorEntityEdgeOperations, id='falkordb'),
    pytest.param(NeptuneEntityEdgeOperations, id='neptune'),
]


def _edge(**overrides: Any) -> EntityEdge:
    fields: dict[str, Any] = dict(
        uuid='edge-1',
        group_id='g',
        source_node_uuid='src',
        target_node_uuid='tgt',
        created_at=CREATED,
        name='VISITED',
        fact='The user was in Rome.',
        fact_embedding=[0.1, 0.2],
        episodes=['ep-1'],
        valid_at=VALID,
        invalid_at=None,
        expired_at=None,
        reference_time=REF,
        attributes={},
    )
    fields.update(overrides)
    return EntityEdge(**fields)


def _copies(order: str) -> list[EntityEdge]:
    """Two unreconciled copies of one edge: one expired by a contradiction, one still live."""
    expired = _edge(invalid_at=T_INVALID, expired_at=T_EXPIRED, episodes=['ep-1'])
    live = _edge(episodes=['ep-2'])
    return [expired, live] if order == 'expired-first' else [live, expired]


ORDERS = pytest.mark.parametrize('order', ['expired-first', 'live-first'])


def _executor() -> MagicMock:
    executor = MagicMock()
    executor.execute_query = AsyncMock(return_value=([], None, None))
    return executor


def _tx() -> MagicMock:
    tx = MagicMock()
    tx.run = AsyncMock()
    return tx


# --- entity_edge_save_row ---------------------------------------------------------------


def test_row_includes_reference_time() -> None:
    # Old driver rows had no reference_time key at all (menhir#260).
    assert entity_edge_save_row(_edge())['reference_time'] == REF


def test_attributes_never_override_core_fields() -> None:
    edge = _edge(
        invalid_at=T_INVALID,
        expired_at=T_EXPIRED,
        attributes={
            'reference_time': '2020-01-01T00:00:00+00:00',
            'expired_at': 'stale-string',
            'invalid_at': 'stale-string',
            'fact': 'stale fact',
            'uuid': 'stale-uuid',
        },
    )
    row = entity_edge_save_row(edge)
    assert row['reference_time'] == REF
    assert row['expired_at'] == T_EXPIRED
    assert row['invalid_at'] == T_INVALID
    assert row['fact'] == edge.fact
    assert row['uuid'] == edge.uuid


def test_ordinary_attributes_are_included() -> None:
    row = entity_edge_save_row(_edge(attributes={'time_expiry': 'world_end', 'n': 3}))
    assert row['time_expiry'] == 'world_end'
    assert row['n'] == 3


def test_with_attributes_false_excludes_them() -> None:
    row = entity_edge_save_row(
        _edge(attributes={'time_expiry': 'world_end'}), with_attributes=False
    )
    assert 'time_expiry' not in row
    assert row['reference_time'] == REF


def test_custom_endpoint_keys() -> None:
    row = entity_edge_save_row(_edge(), source_key='source_uuid', target_key='target_uuid')
    assert row['source_uuid'] == 'src'
    assert row['target_uuid'] == 'tgt'
    assert 'source_node_uuid' not in row
    assert 'target_node_uuid' not in row
    default = entity_edge_save_row(_edge())
    assert default['source_node_uuid'] == 'src'
    assert default['target_node_uuid'] == 'tgt'


# --- driver ops (neo4j / falkordb / neptune) --------------------------------------------

HOSTILE = {
    'reference_time': '2020-01-01T00:00:00+00:00',
    'expired_at': 'stale-string',
    'invalid_at': 'stale-string',
    'extra': 'kept',
}


@pytest.mark.parametrize('use_tx', [False, True], ids=['executor', 'tx'])
@pytest.mark.parametrize('ops_cls', OPS)
@pytest.mark.asyncio
async def test_ops_save_row_has_reference_time_and_core_wins(ops_cls, use_tx) -> None:
    edge = _edge(invalid_at=T_INVALID, expired_at=T_EXPIRED, attributes=dict(HOSTILE))
    executor, tx = _executor(), _tx()

    await ops_cls().save(executor, edge, tx=tx if use_tx else None)

    sink = tx.run if use_tx else executor.execute_query
    row = sink.call_args.kwargs['edge_data']
    assert row['reference_time'] == REF
    assert row['expired_at'] == T_EXPIRED
    assert row['invalid_at'] == T_INVALID
    assert row['extra'] == 'kept'
    assert row['source_uuid'] == 'src'


@pytest.mark.parametrize('use_tx', [False, True], ids=['executor', 'tx'])
@pytest.mark.parametrize('ops_cls', OPS)
@pytest.mark.asyncio
async def test_ops_save_bulk_rows_have_reference_time_and_core_wins(ops_cls, use_tx) -> None:
    edge = _edge(invalid_at=T_INVALID, expired_at=T_EXPIRED, attributes=dict(HOSTILE))
    executor, tx = _executor(), _tx()

    await ops_cls().save_bulk(executor, [edge], tx=tx if use_tx else None)

    sink = tx.run if use_tx else executor.execute_query
    (row,) = sink.call_args.kwargs['entity_edges']
    assert row['reference_time'] == REF
    assert row['expired_at'] == T_EXPIRED
    assert row['invalid_at'] == T_INVALID
    assert row['extra'] == 'kept'


@ORDERS
@pytest.mark.parametrize('use_tx', [False, True], ids=['executor', 'tx'])
@pytest.mark.parametrize('ops_cls', OPS)
@pytest.mark.asyncio
async def test_ops_save_bulk_reconciles_copies_of_one_edge(ops_cls, use_tx, order) -> None:
    # menhir#262: without reconcile the live copy's null expiry is written alongside (or after)
    # the expired copy's, so the stored result depended on row order.
    executor, tx = _executor(), _tx()

    await ops_cls().save_bulk(executor, _copies(order), tx=tx if use_tx else None)

    sink = tx.run if use_tx else executor.execute_query
    rows = sink.call_args.kwargs['entity_edges']
    assert len(rows) == 2
    for row in rows:
        assert row['expired_at'] == T_EXPIRED
        assert row['invalid_at'] == T_INVALID
        assert row['episodes'] == ['ep-1', 'ep-2'] or row['episodes'] == ['ep-2', 'ep-1']


# --- kuzu ops ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kuzu_save_passes_reference_time() -> None:
    try:
        from graphiti_core.driver.kuzu.operations.entity_edge_ops import KuzuEntityEdgeOperations
    except ImportError as exc:  # kuzu is not installable on every machine
        pytest.skip(f'kuzu ops module not importable: {exc}')
    executor = _executor()

    await KuzuEntityEdgeOperations().save(executor, _edge())

    assert executor.execute_query.call_args.kwargs['reference_time'] == REF


# --- add_nodes_and_edges_bulk_tx --------------------------------------------------------


@ORDERS
@pytest.mark.asyncio
async def test_bulk_tx_reconciles_copies_of_one_edge(order) -> None:
    # menhir#262 on the add_nodes_and_edges_bulk path.
    driver = Mock(spec=GraphDriver)
    driver.provider = GraphProvider.NEO4J
    driver.graph_operations_interface = None
    tx = _tx()

    await add_nodes_and_edges_bulk_tx(tx, [], [], [], _copies(order), Mock(), driver=driver)

    batches = [
        c.kwargs['entity_edges'] for c in tx.run.call_args_list if 'entity_edges' in c.kwargs
    ]
    assert len(batches) == 1
    rows = batches[0]
    assert len(rows) == 2
    for row in rows:
        assert row['expired_at'] == T_EXPIRED
        assert row['invalid_at'] == T_INVALID
        assert row['reference_time'] == REF
