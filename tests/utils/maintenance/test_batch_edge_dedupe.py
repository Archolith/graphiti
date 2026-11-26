"""Tests for Menhir #91 Path B: batched edge deduplication, temporal prefilter (1.6),
and exact-edge pre-search bypass (1.7).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from graphiti_core.edges import EntityEdge
from graphiti_core.llm_client.client import RateLimitError
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.prompts import dedupe_edges
from graphiti_core.search.search_config import SearchResults
from graphiti_core.utils.maintenance import edge_operations as edge_ops
from graphiti_core.utils.maintenance.edge_operations import (
    _chunk_edge_dedupe_indices,
    _prefilter_invalidation_candidates,
    resolve_extracted_edges,
)


def _ts(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _edge(
    uuid: str,
    src: str,
    tgt: str,
    fact: str,
    *,
    valid_at: datetime | None = None,
    invalid_at: datetime | None = None,
    group_id: str = 'g1',
    name: str = 'RELATES_TO',
) -> EntityEdge:
    return EntityEdge(
        uuid=uuid,
        source_node_uuid=src,
        target_node_uuid=tgt,
        name=name,
        group_id=group_id,
        fact=fact,
        episodes=[],
        created_at=_ts(2026, 1, 1),
        valid_at=valid_at,
        invalid_at=invalid_at,
    )


def _episode(uuid: str = 'ep-1', valid_at: datetime | None = None) -> EpisodicNode:
    return EpisodicNode(
        uuid=uuid,
        name='Episode',
        group_id='g1',
        source='message',
        source_description='chat',
        content='content',
        valid_at=valid_at or _ts(2026, 6, 1),
    )


def _nodes(*uuids: str) -> list[EntityNode]:
    return [EntityNode(uuid=u, name=u, group_id='g1', labels=['Entity']) for u in uuids]


def test_resolve_edges_batch_prompt_is_static_first_and_clears_cache_floor():
    tiktoken = pytest.importorskip('tiktoken')
    enc = tiktoken.get_encoding('o200k_base')

    ctx_a = {
        'edge_requests': [
            {
                'request_id': 0,
                'existing_edges': [{'idx': 0, 'fact': 'Alice lives in Austin'}],
                'edge_invalidation_candidates': [],
                'new_edge': 'Alice moved to Seattle',
            }
        ]
    }
    ctx_b = {
        'edge_requests': [
            {
                'request_id': 0,
                'existing_edges': [],
                'edge_invalidation_candidates': [{'idx': 0, 'fact': 'API runs on v1'}],
                'new_edge': 'API upgraded to v2',
            },
            {
                'request_id': 1,
                'existing_edges': [{'idx': 0, 'fact': 'Bob speaks English'}],
                'edge_invalidation_candidates': [],
                'new_edge': 'Bob speaks Spanish',
            },
        ]
    }

    sys_a, usr_a = dedupe_edges.resolve_edges_batch(ctx_a)
    sys_b, usr_b = dedupe_edges.resolve_edges_batch(ctx_b)

    assert sys_a.role == 'system'
    assert usr_a.role == 'user'
    assert sys_a.content == sys_b.content
    assert '<EDGE REQUESTS>' not in sys_a.content
    assert 'Alice lives in Austin' not in sys_a.content
    assert '<EDGE REQUESTS>' in usr_a.content
    assert 'contains 1 independent requests with request_id 0 through 0.' in usr_a.content
    assert 'contains 2 independent requests with request_id 0 through 1.' in usr_b.content
    assert len(enc.encode(sys_a.content)) >= 1024


def test_temporal_prefilter_drops_provably_ended_invalidation_candidates_only():
    new_edge = _edge('new', 'a', 'b', 'Alice uses Kafka', valid_at=_ts(2026, 6, 1))
    ended_before = _edge(
        'old-ended',
        'a',
        'c',
        'Alice used RabbitMQ',
        valid_at=_ts(2025, 1, 1),
        invalid_at=_ts(2026, 1, 1),
    )
    ended_exact = _edge(
        'old-exact',
        'a',
        'd',
        'Alice used SQS',
        valid_at=_ts(2025, 6, 1),
        invalid_at=_ts(2026, 6, 1),
    )
    still_active = _edge(
        'active', 'a', 'e', 'Alice uses Redis', valid_at=_ts(2025, 6, 1), invalid_at=None
    )
    overlapping_end = _edge(
        'overlap',
        'a',
        'f',
        'Alice uses NATS',
        valid_at=_ts(2025, 6, 1),
        invalid_at=_ts(2026, 9, 1),
    )

    kept = _prefilter_invalidation_candidates(
        new_edge, [], [ended_before, ended_exact, still_active, overlapping_end]
    )
    assert [e.uuid for e in kept] == ['active', 'overlap']


def test_temporal_prefilter_keeps_candidate_when_duplicate_has_earlier_or_missing_valid_at():
    new_edge = _edge('new', 'a', 'b', 'fact', valid_at=_ts(2026, 6, 1))
    earlier_related = _edge('rel-early', 'a', 'b', 'earlier dup', valid_at=_ts(2025, 6, 1))
    undated_related = _edge('rel-none', 'a', 'b', 'undated dup', valid_at=None)
    candidate = _edge(
        'cand', 'a', 'c', 'old', valid_at=_ts(2025, 1, 1), invalid_at=_ts(2026, 1, 1)
    )

    # Candidate ended in Jan 2026, AFTER rel-early's June 2025 start -> must NOT be dropped.
    assert _prefilter_invalidation_candidates(new_edge, [earlier_related], [candidate]) == [
        candidate
    ]
    # Undated related edge could resolve with unknown valid_at -> must NOT drop.
    assert _prefilter_invalidation_candidates(new_edge, [undated_related], [candidate]) == [
        candidate
    ]
    # Undated new edge -> must NOT drop.
    undated_new = _edge('new-none', 'a', 'b', 'fact', valid_at=None)
    assert _prefilter_invalidation_candidates(undated_new, [], [candidate]) == [candidate]


@pytest.mark.asyncio
async def test_temporal_prefilter_emptying_candidates_skips_llm_call(monkeypatch):
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    ended = _edge(
        'ended', 'a', 'c', 'old fact', valid_at=_ts(2024, 1, 1), invalid_at=_ts(2025, 1, 1)
    )

    async def fake_search(_clients, _query, *, search_filter=None, **_kwargs):
        if getattr(search_filter, 'edge_source_node_uuids', None):
            return SearchResults(edges=[])
        return SearchResults(edges=[ended])

    monkeypatch.setattr(edge_ops, 'search', AsyncMock(side_effect=fake_search))
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock()
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=llm_client, embedder=MagicMock(), cross_encoder=MagicMock()
    )

    extracted = _edge('new', 'a', 'b', 'new fact', valid_at=_ts(2026, 6, 1))
    resolved, invalidated, new_edges = await resolve_extracted_edges(
        clients, [extracted], _episode(), _nodes('a', 'b', 'c'), {}, {}
    )

    llm_client.generate_response.assert_not_awaited()
    assert [e.uuid for e in resolved] == ['new']
    assert [e.uuid for e in new_edges] == ['new']
    assert invalidated == []


@pytest.mark.asyncio
async def test_override_exact_match_skips_both_searches_and_embeddings(monkeypatch):
    embed_mock = AsyncMock(return_value=None)
    search_mock = AsyncMock(return_value=SearchResults())
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', embed_mock)
    monkeypatch.setattr(edge_ops, 'search', search_mock)

    stored = _edge('stored-1', 'a', 'b', 'Alice works at Acme')
    extracted = _edge('new-1', 'a', 'b', '  alice WORKS at acme  ')
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock()
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=llm_client, embedder=MagicMock(), cross_encoder=MagicMock()
    )

    resolved, invalidated, new_edges = await resolve_extracted_edges(
        clients,
        [extracted],
        _episode('ep-2'),
        _nodes('a', 'b'),
        {},
        {},
        existing_edges_override=[stored],
    )

    # Pre-search embedding for extracted_edges is skipped (only post-resolve save embeddings run).
    assert embed_mock.await_count == 2
    assert all(
        call.args[1] != [extracted] for call in embed_mock.await_args_list
    )
    search_mock.assert_not_awaited()
    llm_client.generate_response.assert_not_awaited()
    assert [e.uuid for e in resolved] == ['stored-1']
    assert 'ep-2' in resolved[0].episodes
    assert invalidated == []
    assert new_edges == []


@pytest.mark.asyncio
async def test_resolve_extracted_edges_batches_multiple_semantic_edges_into_one_llm_call(
    monkeypatch,
):
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))

    cand_dup = _edge('dup-0', 'a', 'b', 'Alice joined Acme in 2020', valid_at=_ts(2020, 1, 1))
    cand_old = _edge('old-1', 'a', 'c', 'Service uses Redis', valid_at=_ts(2024, 1, 1))
    cand_other = _edge('other-2', 'b', 'c', 'Bob runs marathons', valid_at=_ts(2025, 1, 1))

    async def fake_search(_clients, query, *, search_filter=None, **_kwargs):
        is_pair = bool(getattr(search_filter, 'edge_source_node_uuids', None))
        if 'started at Acme' in query:
            return SearchResults(edges=[cand_dup] if is_pair else [])
        if 'migrated to Postgres' in query:
            return SearchResults(edges=[] if is_pair else [cand_old])
        return SearchResults(edges=[cand_other] if is_pair else [])

    monkeypatch.setattr(edge_ops, 'search', AsyncMock(side_effect=fake_search))

    calls: list[dict] = []

    async def fake_generate(messages, **kwargs):
        calls.append({'prompt_name': kwargs.get('prompt_name'), 'messages': messages})
        return {
            'edge_resolutions': [
                {'request_id': 0, 'duplicate_facts': [0], 'contradicted_facts': []},
                {'request_id': 1, 'duplicate_facts': [], 'contradicted_facts': [0]},
                {'request_id': 2, 'duplicate_facts': [], 'contradicted_facts': []},
            ]
        }

    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(side_effect=fake_generate)
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=llm_client, embedder=MagicMock(), cross_encoder=MagicMock()
    )

    e0 = _edge('e0', 'a', 'b', 'Alice started at Acme in 2020', valid_at=_ts(2026, 6, 1))
    e1 = _edge('e1', 'a', 'c', 'Service migrated to Postgres from Redis', valid_at=_ts(2026, 6, 1))
    e2 = _edge('e2', 'b', 'c', 'Bob cycles on weekends', valid_at=_ts(2026, 6, 1))

    resolved, invalidated, new_edges = await resolve_extracted_edges(
        clients, [e0, e1, e2], _episode('ep-9'), _nodes('a', 'b', 'c'), {}, {}
    )

    assert len(calls) == 1
    assert calls[0]['prompt_name'] == 'dedupe_edges.resolve_edges_batch'
    assert [e.uuid for e in resolved] == ['dup-0', 'e1', 'e2']
    assert [e.uuid for e in new_edges] == ['e1', 'e2']
    assert [e.uuid for e in invalidated] == ['old-1']
    assert cand_old.invalid_at == _ts(2026, 6, 1)
    assert 'ep-9' in cand_dup.episodes


@pytest.mark.asyncio
async def test_batch_edge_dedupe_falls_back_cleanly_on_missing_request_id(monkeypatch):
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    cand_a = _edge('ca', 'a', 'b', 'Existing A', valid_at=_ts(2025, 1, 1))
    cand_b = _edge('cb', 'a', 'c', 'Existing B', valid_at=_ts(2025, 1, 1))

    async def fake_search(_clients, query, *, search_filter=None, **_kwargs):
        if not getattr(search_filter, 'edge_source_node_uuids', None):
            return SearchResults()
        return SearchResults(edges=[cand_a] if 'New A' in query else [cand_b])

    monkeypatch.setattr(edge_ops, 'search', AsyncMock(side_effect=fake_search))

    prompt_names: list[str | None] = []

    async def fake_generate(_messages, **kwargs):
        pname = kwargs.get('prompt_name')
        prompt_names.append(pname)
        if pname == 'dedupe_edges.resolve_edges_batch':
            # Missing request_id=1 -> must trigger per-edge fallback for this chunk.
            return {
                'edge_resolutions': [
                    {'request_id': 0, 'duplicate_facts': [0], 'contradicted_facts': []}
                ]
            }
        return {'duplicate_facts': [0], 'contradicted_facts': []}

    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(side_effect=fake_generate)
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=llm_client, embedder=MagicMock(), cross_encoder=MagicMock()
    )

    e0 = _edge('e0', 'a', 'b', 'New A', valid_at=_ts(2026, 6, 1))
    e1 = _edge('e1', 'a', 'c', 'New B', valid_at=_ts(2026, 6, 1))

    resolved, _inv, _new = await resolve_extracted_edges(
        clients, [e0, e1], _episode(), _nodes('a', 'b', 'c'), {}, {}
    )

    assert prompt_names[0] == 'dedupe_edges.resolve_edges_batch'
    assert prompt_names.count('dedupe_edges.resolve_edge') == 2
    assert [e.uuid for e in resolved] == ['ca', 'cb']


@pytest.mark.asyncio
async def test_batch_edge_dedupe_propagates_rate_limit_error(monkeypatch):
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    cand = _edge('c1', 'a', 'b', 'Existing', valid_at=_ts(2025, 1, 1))

    async def fake_search(_clients, _query, *, search_filter=None, **_kwargs):
        return SearchResults(edges=[cand] if getattr(search_filter, 'edge_source_node_uuids', None) else [])

    monkeypatch.setattr(edge_ops, 'search', AsyncMock(side_effect=fake_search))

    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(side_effect=RateLimitError('429'))
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=llm_client, embedder=MagicMock(), cross_encoder=MagicMock()
    )

    e0 = _edge('e0', 'a', 'b', 'New 0', valid_at=_ts(2026, 6, 1))
    e1 = _edge('e1', 'a', 'b', 'New 1', valid_at=_ts(2026, 6, 1))

    with pytest.raises(RateLimitError):
        await resolve_extracted_edges(
            clients, [e0, e1], _episode(), _nodes('a', 'b'), {}, {}
        )


def test_chunk_edge_dedupe_indices_respects_max_items_and_max_chars():
    edges = [_edge(f'e{i}', 'a', 'b', f'Fact number {i}') for i in range(5)]
    related = [[_edge(f'r{i}', 'a', 'b', f'Candidate {i}')] for i in range(5)]
    inv = [[] for _ in range(5)]

    chunks, singles = _chunk_edge_dedupe_indices(
        [0, 1, 2, 3, 4], edges, related, inv, max_items=2, max_chars=24_000
    )
    assert chunks == [[0, 1], [2, 3]]
    assert singles == [4]

    # Huge single edge is routed to singles instead of breaking the char budget.
    big_edges = [
        _edge('e0', 'a', 'b', 'X' * 500),
        _edge('e1', 'a', 'b', 'small 1'),
        _edge('e2', 'a', 'b', 'small 2'),
    ]
    chunks2, singles2 = _chunk_edge_dedupe_indices(
        [0, 1, 2], big_edges, [[], [], []], [[], [], []], max_items=12, max_chars=400
    )
    assert singles2 == [0]
    assert chunks2 == [[1, 2]]


@pytest.mark.asyncio
async def test_batch_edge_dedupe_opt_out_flag_uses_single_edge_prompt(monkeypatch):
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    cand = _edge('c1', 'a', 'b', 'Existing', valid_at=_ts(2025, 1, 1))

    async def fake_search(_clients, _query, *, search_filter=None, **_kwargs):
        return SearchResults(edges=[cand] if getattr(search_filter, 'edge_source_node_uuids', None) else [])

    monkeypatch.setattr(edge_ops, 'search', AsyncMock(side_effect=fake_search))
    prompt_names: list[str | None] = []

    async def fake_generate(_messages, **kwargs):
        prompt_names.append(kwargs.get('prompt_name'))
        return {'duplicate_facts': [], 'contradicted_facts': []}

    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(side_effect=fake_generate)
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=llm_client, embedder=MagicMock(), cross_encoder=MagicMock()
    )

    e0 = _edge('e0', 'a', 'b', 'New 0', valid_at=_ts(2026, 6, 1))
    e1 = _edge('e1', 'a', 'b', 'New 1', valid_at=_ts(2026, 6, 1))

    await resolve_extracted_edges(
        clients, [e0, e1], _episode(), _nodes('a', 'b'), {}, {}, batch_edge_dedupe=False
    )
    assert prompt_names == ['dedupe_edges.resolve_edge', 'dedupe_edges.resolve_edge']
