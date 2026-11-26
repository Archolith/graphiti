"""The default group ('') is a search scope, not "every group" (fork).

Upstream's search() turned group_ids=[''] into None, so a default-group search, including the
duplicate and invalidation candidate searches for a default-group edge, saw every group.
"""

from unittest.mock import MagicMock, patch

import pytest

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.neo4j.operations.search_ops import _build_neo4j_fulltext_query
from graphiti_core.driver.relate.operations.search_ops import _build_relate_fulltext_query
from graphiti_core.search import search as search_module
from graphiti_core.search.search_config import SearchResults
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
from graphiti_core.search.search_filters import SearchFilters
from graphiti_core.search.search_utils import fulltext_query


def _driver():
    driver = MagicMock()
    driver.provider = GraphProvider.NEO4J
    driver.fulltext_syntax = ''
    driver.search_interface = None
    return driver


@pytest.mark.parametrize(
    ('group_ids', 'expected'),
    [([''], ['']), (['', 'other'], ['', 'other']), (['a'], ['a']), ([], None), (None, None)],
)
async def test_search_keeps_default_group_scope(group_ids, expected):
    clients = MagicMock()
    clients.driver = _driver()
    clients.max_coroutines = None
    seen: list = []

    async def fake_edge_search(driver, cross_encoder, query, query_vector, group_ids, *a, **k):
        seen.append(group_ids)
        return [], []

    with patch.object(search_module, 'edge_search', side_effect=fake_edge_search):
        result = await search_module.search(
            clients,
            'fact',
            group_ids,
            EDGE_HYBRID_SEARCH_RRF,
            SearchFilters(),
            query_vector=[0.1, 0.2],
        )

    assert isinstance(result, SearchResults)
    assert seen == [expected]


@pytest.mark.parametrize(
    'build',
    [
        lambda q, g: fulltext_query(q, g, _driver()),
        lambda q, g: _build_neo4j_fulltext_query(q, g),
        lambda q, g: _build_relate_fulltext_query(q, g),
    ],
)
def test_lucene_group_clause_omitted_when_default_group_present(build):
    # Lucene drops group_id:"", so ['', 'other'] would narrow to 'other' alone; the Cypher
    # `group_id IN $group_ids` filter scopes these lists instead.
    assert build('redis', ['']) == '(redis)'
    assert build('redis', ['', 'other']) == '(redis)'
    assert build('redis', ['other']) == 'group_id:"other" AND (redis)'
    assert build('redis', None) == '(redis)'
