"""A stale save of an edge loaded from the store never recreates a deleted edge (menhir#261).

Unit tests, no database: query shape, the loaded-from-store mark, the Neo4j bulk row split and
the single-save query choice. The live behaviour is in test_entity_edge_save_after_delete_int.py.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.neo4j.operations.entity_edge_ops import Neo4jEntityEdgeOperations
from graphiti_core.driver.record_parsers import entity_edge_from_record
from graphiti_core.edges import (
    EntityEdge,
    entity_edge_save_row,
    get_entity_edge_from_record,
    save_entity_edge_rows_neo4j,
)
from graphiti_core.models.edges.edge_db_queries import (
    get_entity_edge_save_bulk_query,
    get_entity_edge_save_query,
)

NOW = datetime(2024, 3, 1, tzinfo=timezone.utc)


def _edge(uuid: str = 'e1', loaded: bool = False) -> EntityEdge:
    edge = EntityEdge(
        uuid=uuid,
        group_id='g',
        source_node_uuid='s',
        target_node_uuid='t',
        created_at=NOW,
        name='VISITED',
        fact='fact',
        fact_embedding=[0.1, 0.2],
        episodes=['ep'],
    )
    edge._loaded_from_store = loaded
    return edge


def _record() -> dict[str, Any]:
    return {
        'uuid': 'e1',
        'source_node_uuid': 's',
        'target_node_uuid': 't',
        'fact': 'fact',
        'fact_embedding': [0.1],
        'name': 'VISITED',
        'group_id': 'g',
        'episodes': ['ep'],
        'created_at': NOW,
        'expired_at': None,
        'valid_at': None,
        'invalid_at': None,
        'reference_time': None,
        'attributes': {'uuid': 'e1', 'custom': 1},
    }


def _order(query: str, parts: list[str]) -> list[int]:
    positions = []
    for part in parts:
        assert part in query, f'missing {part!r}'
        positions.append(query.index(part))
    return positions


# ---------------------------------------------------------------- query shape

SINGLE_PARTS = [
    'MATCH (source:Entity {uuid: $edge_data.source_uuid})',
    'MATCH (target:Entity {uuid: $edge_data.target_uuid})',
    '[()-[stored:RELATES_TO {uuid: $edge_data.uuid}]->() | stored] AS stored_copies',
    'WHERE size(stored_copies) > 0',
    'FOREACH (stored IN stored_copies | SET stored._graphiti_save_lock = true)',
    'MERGE (source)-[e:RELATES_TO {uuid: $edge_data.uuid}]->(target)',
    'stored_expired_at',  # the keep-stored-expiry guard
    'FOREACH (stored IN stored_copies | REMOVE stored._graphiti_save_lock)',
    'RETURN e.uuid AS uuid, e.expired_at AS expired_at, e.invalid_at AS invalid_at',
]
BULK_PARTS = [
    'UNWIND $entity_edges AS edge',
    'MATCH (source:Entity {uuid: edge.source_node_uuid})',
    'MATCH (target:Entity {uuid: edge.target_node_uuid})',
    '[()-[stored:RELATES_TO {uuid: edge.uuid}]->() | stored] AS stored_copies',
    'WHERE size(stored_copies) > 0',
    'FOREACH (stored IN stored_copies | SET stored._graphiti_save_lock = true)',
    'MERGE (source)-[e:RELATES_TO {uuid: edge.uuid}]->(target)',
    'stored_expired_at',
    'FOREACH (stored IN stored_copies | REMOVE stored._graphiti_save_lock)',
    'RETURN edge.uuid AS uuid, e.expired_at AS expired_at, e.invalid_at AS invalid_at',
]


@pytest.mark.parametrize('has_aoss', [False, True])
@pytest.mark.parametrize(
    ('builder', 'parts'),
    [(get_entity_edge_save_query, SINGLE_PARTS), (get_entity_edge_save_bulk_query, BULK_PARTS)],
    ids=['single', 'bulk'],
)
def test_stored_neo4j_query_parts_in_order(builder, parts, has_aoss) -> None:
    query = builder(GraphProvider.NEO4J, has_aoss=has_aoss, stored=True)
    positions = _order(query, parts)
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)
    # the lock is set before the guard reads, and the vector call sits between unlock and RETURN
    vector = 'db.create.setRelationshipVectorProperty'
    if has_aoss:
        assert vector not in query
    else:
        assert positions[-2] < query.index(vector) < positions[-1]
    assert query.count('SET stored._graphiti_save_lock') == 1
    assert query.count('REMOVE stored._graphiti_save_lock') == 1


@pytest.mark.parametrize('has_aoss', [False, True])
@pytest.mark.parametrize(
    'builder', [get_entity_edge_save_query, get_entity_edge_save_bulk_query], ids=['single', 'bulk']
)
def test_default_neo4j_query_has_no_stored_copy_lock(builder, has_aoss) -> None:
    default = builder(GraphProvider.NEO4J, has_aoss=has_aoss)
    assert default == builder(GraphProvider.NEO4J, has_aoss=has_aoss, stored=False)
    assert 'stored_copies' not in default
    assert 'stored._graphiti_save_lock' not in default
    assert default != builder(GraphProvider.NEO4J, has_aoss=has_aoss, stored=True)


@pytest.mark.parametrize('provider', [GraphProvider.FALKORDB, GraphProvider.NEPTUNE])
@pytest.mark.parametrize(
    'builder', [get_entity_edge_save_query, get_entity_edge_save_bulk_query], ids=['single', 'bulk']
)
def test_other_providers_ignore_stored(builder, provider) -> None:
    assert builder(provider, stored=True) == builder(provider, stored=False)
    assert 'stored_copies' not in builder(provider, stored=True)


# ------------------------------------------------------- loaded-from-store mark


def test_direct_construction_is_not_loaded() -> None:
    assert _edge()._loaded_from_store is False


def test_record_builders_mark_loaded() -> None:
    assert entity_edge_from_record(_record())._loaded_from_store is True
    assert get_entity_edge_from_record(_record(), GraphProvider.NEO4J)._loaded_from_store is True


def test_model_copy_keeps_mark_and_model_dump_rebuild_loses_it() -> None:
    loaded = entity_edge_from_record(_record())
    assert loaded.model_copy()._loaded_from_store is True
    assert loaded.model_copy(update={'fact': 'changed'})._loaded_from_store is True
    assert EntityEdge(**loaded.model_dump())._loaded_from_store is False


def test_new_uuid_drops_mark() -> None:
    # A loaded edge given a new uuid is a new edge: its save must not skip the row (add_triplet's
    # endpoint-mismatch branch assigns uuid4()).
    loaded = entity_edge_from_record(_record())
    assert loaded.model_copy(update={'uuid': 'other'})._loaded_from_store is False
    loaded.uuid = 'other'
    assert loaded._loaded_from_store is False
    loaded.uuid = 'e1'
    assert loaded._loaded_from_store is True


async def test_bulk_split_sends_reassigned_uuid_to_plain_query() -> None:
    loaded = entity_edge_from_record(_record())
    loaded.uuid = 'fresh'
    run = AsyncMock(return_value=None)
    await save_entity_edge_rows_neo4j(run, [loaded], [entity_edge_save_row(loaded)])
    assert run.await_count == 1
    assert run.await_args.args[0] == get_entity_edge_save_bulk_query(GraphProvider.NEO4J)


# ---------------------------------------------- save_entity_edge_rows_neo4j split


class _Result:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def data(self) -> list[dict[str, Any]]:
        return self._records


class _FakeRun:
    """Records (query, rows) per call; returns a record per row unless uuids are 'deleted'."""

    def __init__(self, missing: set[str] | None = None) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.missing = missing or set()

    async def __call__(self, query: str, entity_edges: list[dict[str, Any]]) -> _Result:
        self.calls.append((query, entity_edges))
        return _Result(
            [
                {'uuid': r['uuid'], 'expired_at': None, 'invalid_at': None}
                for r in entity_edges
                if r['uuid'] not in self.missing
            ]
        )


PLAIN = get_entity_edge_save_bulk_query(GraphProvider.NEO4J)
STORED = get_entity_edge_save_bulk_query(GraphProvider.NEO4J, stored=True)


def _rows(edges: list[EntityEdge]) -> list[dict[str, Any]]:
    return [entity_edge_save_row(e) for e in edges]


def _uuids(rows: list[dict[str, Any]]) -> list[str]:
    return [r['uuid'] for r in rows]


@pytest.mark.asyncio
async def test_new_only_uses_one_plain_call() -> None:
    edges = [_edge('a'), _edge('b')]
    run = _FakeRun()
    await save_entity_edge_rows_neo4j(run, edges, _rows(edges))
    assert [(q, _uuids(r)) for q, r in run.calls] == [(PLAIN, ['a', 'b'])]


@pytest.mark.asyncio
async def test_loaded_only_uses_one_stored_call() -> None:
    edges = [_edge('a', True), _edge('b', True)]
    run = _FakeRun()
    await save_entity_edge_rows_neo4j(run, edges, _rows(edges))
    assert [(q, _uuids(r)) for q, r in run.calls] == [(STORED, ['a', 'b'])]


@pytest.mark.asyncio
async def test_mixed_batch_splits_rows() -> None:
    edges = [_edge('new1'), _edge('old1', True), _edge('new2'), _edge('old2', True)]
    run = _FakeRun()
    await save_entity_edge_rows_neo4j(run, edges, _rows(edges))
    assert [(q, _uuids(r)) for q, r in run.calls] == [
        (PLAIN, ['new1', 'new2']),
        (STORED, ['old1', 'old2']),
    ]


@pytest.mark.asyncio
async def test_uuid_with_loaded_and_new_copy_goes_to_stored_call() -> None:
    edges = [_edge('dup', True), _edge('dup', False), _edge('other')]
    run = _FakeRun()
    await save_entity_edge_rows_neo4j(run, edges, _rows(edges))
    assert [(q, _uuids(r)) for q, r in run.calls] == [
        (PLAIN, ['other']),
        (STORED, ['dup', 'dup']),
    ]


@pytest.mark.asyncio
async def test_empty_rows_run_nothing() -> None:
    run = _FakeRun()
    await save_entity_edge_rows_neo4j(run, [], [])
    assert run.calls == []


@pytest.mark.asyncio
async def test_info_log_names_each_loaded_uuid_without_a_record(caplog) -> None:
    edges = [_edge('gone1', True), _edge('kept', True), _edge('gone2', True), _edge('newone')]
    run = _FakeRun(missing={'gone1', 'gone2', 'newone'})
    with caplog.at_level(logging.INFO, logger='graphiti_core.edges'):
        await save_entity_edge_rows_neo4j(run, edges, _rows(edges))
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    text = '\n'.join(r.getMessage() for r in infos)
    assert 'gone1' in text and 'gone2' in text
    assert 'kept' not in text
    assert 'newone' not in text  # a new edge without a record is not reported as deleted
    assert len(infos) == 2


@pytest.mark.asyncio
async def test_no_info_log_when_all_records_return(caplog) -> None:
    edges = [_edge('a', True), _edge('b')]
    with caplog.at_level(logging.INFO, logger='graphiti_core.edges'):
        await save_entity_edge_rows_neo4j(_FakeRun(), edges, _rows(edges))
    assert [r for r in caplog.records if r.levelno == logging.INFO] == []


# ------------------------------------------------- single save query selection


def _driver() -> MagicMock:
    driver = MagicMock()
    driver.provider = GraphProvider.NEO4J
    driver.graph_operations_interface = None
    driver.execute_query = AsyncMock(return_value=([], None, None))
    return driver


SINGLE_PLAIN = get_entity_edge_save_query(GraphProvider.NEO4J)
SINGLE_STORED = get_entity_edge_save_query(GraphProvider.NEO4J, stored=True)


@pytest.mark.parametrize(('loaded', 'expected'), [(True, SINGLE_STORED), (False, SINGLE_PLAIN)])
@pytest.mark.asyncio
async def test_entity_edge_save_picks_query_by_loaded_mark(loaded, expected) -> None:
    driver = _driver()
    await _edge(loaded=loaded).save(driver)
    assert driver.execute_query.call_args.args[0] == expected


@pytest.mark.parametrize(('loaded', 'expected'), [(True, SINGLE_STORED), (False, SINGLE_PLAIN)])
@pytest.mark.parametrize('use_tx', [False, True])
@pytest.mark.asyncio
async def test_ops_save_picks_query_by_loaded_mark(loaded, expected, use_tx) -> None:
    executor = _driver()
    tx = MagicMock()
    tx.run = AsyncMock(return_value=_Result([]))
    await Neo4jEntityEdgeOperations().save(
        executor, _edge(loaded=loaded), tx=tx if use_tx else None
    )
    target = tx.run if use_tx else executor.execute_query
    assert target.call_args.args[0] == expected


@pytest.mark.asyncio
async def test_single_save_logs_info_for_loaded_edge_without_record(caplog) -> None:
    with caplog.at_level(logging.INFO, logger='graphiti_core.edges'):
        await _edge('gone', True).save(_driver())
        await _edge('fresh', False).save(_driver())
    text = '\n'.join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert 'gone' in text
    assert 'fresh' not in text


@pytest.mark.asyncio
async def test_ops_save_bulk_routes_through_row_split() -> None:
    executor = _driver()
    executor.execute_query = AsyncMock(return_value=_Result([]))
    edges = [_edge('new'), _edge('old', True)]
    await Neo4jEntityEdgeOperations().save_bulk(executor, edges)
    queries = [c.args[0] for c in executor.execute_query.call_args_list]
    assert queries == [PLAIN, STORED]
    assert _uuids(executor.execute_query.call_args_list[1].kwargs['entity_edges']) == ['old']
