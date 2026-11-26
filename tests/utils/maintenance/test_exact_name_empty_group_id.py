"""Regression tests for exact-name candidate lookup with empty-string group_id (Menhir #93).

Menhir maps its logical ``default`` namespace to physical ``group_id == ""``.
Filtering group_ids with truthiness (``if group_id``) silently dropped ``""``
and bypassed exact-name node candidate lookup on the default namespace.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.maintenance.node_operations import _collect_exact_name_candidate_nodes


@pytest.mark.asyncio
async def test_get_by_names_preserves_empty_string_group_id():
    driver = MagicMock()
    driver.graph_operations_interface = None
    driver.provider = GraphProvider.NEO4J
    driver.execute_query = AsyncMock(return_value=([], None, None))

    await EntityNode.get_by_names(driver, ['Alice'], [''])

    driver.execute_query.assert_awaited_once()
    kwargs = driver.execute_query.await_args.kwargs
    assert kwargs['names'] == ['Alice']
    assert kwargs['group_ids'] == ['']


@pytest.mark.asyncio
async def test_get_by_names_still_skips_none_only_group_ids():
    driver = MagicMock()
    driver.graph_operations_interface = None
    driver.provider = GraphProvider.NEO4J
    driver.execute_query = AsyncMock(return_value=([], None, None))

    result = await EntityNode.get_by_names(driver, ['Alice'], [None])  # type: ignore[list-item]

    assert result == []
    driver.execute_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_exact_name_candidates_works_for_empty_group_id(monkeypatch):
    stored = EntityNode(name='Alice', group_id='', labels=['Entity'])
    extracted = EntityNode(name='Alice', group_id='', labels=['Entity'])
    get_by_names = AsyncMock(return_value=[stored])
    monkeypatch.setattr(EntityNode, 'get_by_names', get_by_names)

    clients = SimpleNamespace(driver=MagicMock())
    result = await _collect_exact_name_candidate_nodes(clients, [extracted])

    get_by_names.assert_awaited_once_with(clients.driver, ['Alice'], [''])
    assert result == [[stored]]
