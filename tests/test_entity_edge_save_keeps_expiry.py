"""Shape of the Neo4j entity-edge save that keeps a stored expiry (offline).

The live behaviour, including the concurrent case, is in
tests/test_entity_edge_save_keeps_expiry_int.py.
"""

import pytest

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.models.edges.edge_db_queries import (
    get_entity_edge_save_bulk_query,
    get_entity_edge_save_query,
)

NEO4J_QUERIES = [
    ('$edge_data', get_entity_edge_save_query(GraphProvider.NEO4J)),
    ('$edge_data', get_entity_edge_save_query(GraphProvider.NEO4J, has_aoss=True)),
    ('edge', get_entity_edge_save_bulk_query(GraphProvider.NEO4J)),
    ('edge', get_entity_edge_save_bulk_query(GraphProvider.NEO4J, has_aoss=True)),
]


@pytest.mark.parametrize(('row', 'query'), NEO4J_QUERIES)
def test_lock_then_read_then_replace_then_restore(row, query) -> None:
    order = [
        'MERGE (source)-[e:RELATES_TO',
        'SET e._graphiti_save_lock = true',
        'e.expired_at AS stored_expired_at, e.invalid_at AS stored_invalid_at',
        f'SET e = {row}\n',
        'CASE WHEN stored_expired_at IS NOT NULL THEN [1] ELSE [] END',
        f'WHEN {row}.expired_at IS NULL OR stored_expired_at < {row}.expired_at',
        f'WHEN {row}.invalid_at IS NULL',
        f'(stored_invalid_at IS NOT NULL AND stored_invalid_at < {row}.invalid_at)',
        'RETURN ',
    ]
    positions = [query.find(part) for part in order]
    assert all(pos >= 0 for pos in positions), dict(zip(order, positions, strict=True))
    assert positions == sorted(positions)
    assert query.count('SET e = ') == 1
    vector = query.find('setRelationshipVectorProperty')
    if vector >= 0:
        assert vector > positions[-2]


@pytest.mark.parametrize(
    'provider', [GraphProvider.FALKORDB, GraphProvider.NEPTUNE, GraphProvider.KUZU]
)
def test_other_providers_keep_the_upstream_save(provider) -> None:
    # Only the Neo4j save carries the guard; the other providers are unchanged (documented).
    for query in (get_entity_edge_save_query(provider), get_entity_edge_save_bulk_query(provider)):
        assert '_graphiti_save_lock' not in query
        assert 'stored_expired_at' not in query
