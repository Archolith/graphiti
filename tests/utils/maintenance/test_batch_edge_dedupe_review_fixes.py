"""Regression tests for the PR #34 review fixes (menhir#91 Path B).

Each test names the review finding it covers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import openai
import pytest
from pydantic import ValidationError

from graphiti_core.edge_expiry import EdgeExpiryContext, EdgeExpiryDecision
from graphiti_core.edges import EntityEdge
from graphiti_core.ingestion.change_set import ResolutionTrace
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.prompts import dedupe_edges
from graphiti_core.prompts.dedupe_edges import EdgeDuplicateBatch
from graphiti_core.prompts.lib import validate_prompt_library
from graphiti_core.prompts.models import ChatPrompt
from graphiti_core.search.search_config import SearchResults
from graphiti_core.utils.maintenance import edge_operations as edge_ops
from graphiti_core.utils.maintenance.edge_operations import (
    _can_batch_edge_dedupe,
    _resolve_edge_dedupe_batch_chunk,
    resolve_extracted_edge,
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
) -> EntityEdge:
    return EntityEdge(
        uuid=uuid,
        source_node_uuid=src,
        target_node_uuid=tgt,
        name='RELATES_TO',
        group_id='',
        fact=fact,
        episodes=[],
        created_at=_ts(2026),
        valid_at=valid_at,
        invalid_at=invalid_at,
    )


def _episode() -> EpisodicNode:
    return EpisodicNode(
        uuid='ep-1',
        name='Episode',
        group_id='',
        source='message',
        source_description='chat',
        content='content',
        valid_at=_ts(2026, 6, 1),
    )


def _nodes(*uuids: str) -> list[EntityNode]:
    return [EntityNode(uuid=u, name=u, group_id='', labels=['Entity']) for u in uuids]


def _clients(generate: Any) -> SimpleNamespace:
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(side_effect=generate)
    return SimpleNamespace(
        driver=MagicMock(), llm_client=llm_client, embedder=MagicMock(), cross_encoder=MagicMock()
    )


def _stale_only_search(stale: EntityEdge) -> AsyncMock:
    async def fake_search(_clients, _query, *, search_filter=None, **_kwargs):
        if getattr(search_filter, 'edge_source_node_uuids', None):
            return SearchResults(edges=[])
        return SearchResults(edges=[stale])

    return AsyncMock(side_effect=fake_search)


class _RecordingExpiryHook:
    def __init__(self) -> None:
        self.contexts: list[EdgeExpiryContext] = []

    async def decide_edge_expiry(self, context: EdgeExpiryContext) -> EdgeExpiryDecision:
        self.contexts.append(context)
        return EdgeExpiryDecision.EXPIRE


# F1: an edge whose only candidates the prefilter drops still gets own-end expiry and the hook.
async def test_prefilter_emptied_edge_with_own_end_is_expired_and_hook_consulted(monkeypatch):
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    stale = _edge('stale', 'a', 'c', 'Alice used RabbitMQ', valid_at=_ts(2024), invalid_at=_ts(2025))
    monkeypatch.setattr(edge_ops, 'search', _stale_only_search(stale))
    clients = _clients(AsyncMock(side_effect=AssertionError('no model call expected')))
    hook = _RecordingExpiryHook()
    extracted = _edge(
        'new', 'a', 'b', 'Alice uses Kafka', valid_at=_ts(2026, 6, 1), invalid_at=_ts(2026, 9, 1)
    )

    resolved, invalidated, new_edges = await resolve_extracted_edges(
        clients, [extracted], _episode(), _nodes('a', 'b', 'c'), {}, {}, edge_expiry_hook=hook
    )

    clients.llm_client.generate_response.assert_not_awaited()
    assert [e.uuid for e in resolved] == ['new']
    assert resolved[0].expired_at is not None
    assert [c.resolved_edge.uuid for c in hook.contexts] == ['new']
    assert invalidated == []
    assert stale.invalid_at == _ts(2025)


# F1 on the single-edge path (add_triplet uses resolve_extracted_edge).
async def test_single_edge_path_prefilter_emptied_edge_is_expired():
    stale = _edge('stale', 'a', 'c', 'Alice used RabbitMQ', valid_at=_ts(2024), invalid_at=_ts(2025))
    clients = _clients(AsyncMock(side_effect=AssertionError('no model call expected')))
    extracted = _edge(
        'new', 'a', 'b', 'Alice uses Kafka', valid_at=_ts(2026, 6, 1), invalid_at=_ts(2026, 9, 1)
    )

    resolved, invalidated, _duplicates = await resolve_extracted_edge(
        clients.llm_client, extracted, [], [stale], _episode()
    )

    assert resolved.uuid == 'new'
    assert resolved.expired_at is not None
    assert invalidated == []


# N1: the trace records the candidates the prefilter dropped, so commit repair skips them.
async def test_trace_records_candidates_before_prefilter(monkeypatch):
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    stale = _edge('stale', 'a', 'c', 'Alice used RabbitMQ', valid_at=_ts(2024), invalid_at=_ts(2025))
    monkeypatch.setattr(edge_ops, 'search', _stale_only_search(stale))
    clients = _clients(AsyncMock(return_value={}))
    trace = ResolutionTrace()

    await resolve_extracted_edges(
        clients,
        [_edge('new', 'a', 'b', 'Alice uses Kafka', valid_at=_ts(2026, 6, 1))],
        _episode(),
        _nodes('a', 'b', 'c'),
        {},
        {},
        trace=trace,
    )

    assert trace.seen_invalidation_candidates == {'new': ['stale']}


# F2 / F14: malformed batch entries fail validation instead of being coerced or dropped.
@pytest.mark.parametrize(
    'entry',
    [
        {'request_id': 0},
        {'request_id': 0, 'duplicate_facts': [1.7], 'contradicted_facts': []},
        {'request_id': 0, 'duplicate_facts': [], 'contradicted_facts': '2'},
        {'request_id': 2.9, 'duplicate_facts': [], 'contradicted_facts': []},
        {'request_id': None, 'duplicate_facts': [], 'contradicted_facts': []},
        'junk',
    ],
)
def test_batch_response_rejects_malformed_entries(entry):
    with pytest.raises(ValidationError):
        EdgeDuplicateBatch(edge_resolutions=[entry])


def _chunk_inputs() -> tuple[list[EntityEdge], list[list[EntityEdge]], list[list[EntityEdge]]]:
    extracted = [
        _edge('e0', 'a', 'b', 'New A', valid_at=_ts(2026, 6, 1)),
        _edge('e1', 'a', 'c', 'New B', valid_at=_ts(2026, 6, 1)),
    ]
    related = [[_edge('ca', 'a', 'b', 'Old A', valid_at=_ts(2025))], []]
    existing = [[], [_edge('cb', 'a', 'd', 'Old B', valid_at=_ts(2025))]]
    return extracted, related, existing


# F2 through the chunk: an omitted field rejects the chunk rather than meaning "no decision".
async def test_batch_chunk_with_missing_fields_falls_back():
    extracted, related, existing = _chunk_inputs()
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(
        return_value={
            'edge_resolutions': [
                {'request_id': 0},
                {'request_id': 1, 'duplicate_facts': [], 'contradicted_facts': []},
            ]
        }
    )

    assert await _resolve_edge_dedupe_batch_chunk(llm_client, [0, 1], extracted, related, existing) is None


# F4: an idx outside a request's own range rejects the chunk.
@pytest.mark.parametrize(
    'resolutions',
    [
        # Request 1 has no duplicate candidates, so duplicate idx 0 belongs to another request.
        [
            {'request_id': 0, 'duplicate_facts': [], 'contradicted_facts': []},
            {'request_id': 1, 'duplicate_facts': [0], 'contradicted_facts': []},
        ],
        # Request 0 has one candidate in total; contradicted idx 1 is out of range.
        [
            {'request_id': 0, 'duplicate_facts': [], 'contradicted_facts': [1]},
            {'request_id': 1, 'duplicate_facts': [], 'contradicted_facts': []},
        ],
        [
            {'request_id': 0, 'duplicate_facts': [-1], 'contradicted_facts': []},
            {'request_id': 1, 'duplicate_facts': [], 'contradicted_facts': []},
        ],
    ],
)
async def test_batch_chunk_out_of_range_idx_falls_back(resolutions):
    extracted, related, existing = _chunk_inputs()
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(return_value={'edge_resolutions': resolutions})

    assert await _resolve_edge_dedupe_batch_chunk(llm_client, [0, 1], extracted, related, existing) is None


async def test_batch_chunk_in_range_answers_are_accepted():
    extracted, related, existing = _chunk_inputs()
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(
        return_value={
            'edge_resolutions': [
                {'request_id': 0, 'duplicate_facts': [0], 'contradicted_facts': []},
                {'request_id': 1, 'duplicate_facts': [], 'contradicted_facts': [0]},
            ]
        }
    )

    result = await _resolve_edge_dedupe_batch_chunk(llm_client, [0, 1], extracted, related, existing)

    assert result is not None
    assert result[0].duplicate_facts == [0]
    assert result[1].contradicted_facts == [0]


_REQUEST = httpx.Request('POST', 'https://api.openai.com/v1/chat/completions')


# F6: provider, quota and transport failures propagate; a bad response shape falls back.
@pytest.mark.parametrize(
    'error',
    [
        openai.APIConnectionError(request=_REQUEST),
        openai.APIStatusError(
            'payment required', response=httpx.Response(402, request=_REQUEST), body=None
        ),
        httpx.ConnectError('down', request=_REQUEST),
    ],
)
async def test_batch_chunk_propagates_provider_errors(error):
    extracted, related, existing = _chunk_inputs()
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(side_effect=error)

    with pytest.raises(type(error)):
        await _resolve_edge_dedupe_batch_chunk(llm_client, [0, 1], extracted, related, existing)


async def test_batch_chunk_falls_back_on_non_provider_error():
    extracted, related, existing = _chunk_inputs()
    llm_client = MagicMock()
    llm_client.generate_response = AsyncMock(side_effect=ValueError('unparseable output'))

    assert await _resolve_edge_dedupe_batch_chunk(llm_client, [0, 1], extracted, related, existing) is None


# N4: replacing decide_edge_against_candidates turns batching off.
def test_replacing_decide_function_disables_batching(monkeypatch):
    assert _can_batch_edge_dedupe(None)

    async def replacement(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('not called')

    monkeypatch.setattr(edge_ops, 'decide_edge_against_candidates', replacement)

    assert not _can_batch_edge_dedupe(None)


# N3: a replacement with upstream's signature (no precomputed_decision) still works per-edge.
async def test_upstream_signature_decide_replacement_is_called_without_new_kwarg(monkeypatch):
    original = edge_ops.decide_edge_against_candidates
    seen_kwargs: list[dict[str, Any]] = []

    async def upstream_signature(llm_client, extracted_edge, related_edges, existing_edges, *, clients=None):
        seen_kwargs.append({'clients': clients})
        return await original(llm_client, extracted_edge, related_edges, existing_edges, clients=clients)

    monkeypatch.setattr(edge_ops, 'decide_edge_against_candidates', upstream_signature)
    clients = _clients(AsyncMock(return_value={'duplicate_facts': [], 'contradicted_facts': []}))
    candidate = _edge('c', 'a', 'b', 'Old fact', valid_at=_ts(2025))

    resolved, _invalidated, _duplicates = await resolve_extracted_edge(
        clients.llm_client,
        _edge('new', 'a', 'b', 'New fact', valid_at=_ts(2026, 6, 1)),
        [candidate],
        [],
        _episode(),
    )

    assert resolved.uuid == 'new'
    assert len(seen_kwargs) == 1


# F10: a prompt library written before the batch prompt existed stays valid.
def test_custom_dedupe_edges_prompts_without_batch_method_instantiates():
    class OldPrompts(dedupe_edges.DedupeEdgesPrompts):
        def resolve_edge(self, context: dict[str, Any]) -> ChatPrompt:
            return ChatPrompt.from_messages(dedupe_edges.resolve_edge(context))

    OldPrompts()


def test_duck_typed_library_without_batch_prompt_validates():
    from graphiti_core.prompts.lib import PROMPT_GROUPS, prompt_library

    groups = {}
    for group_name, function_names in PROMPT_GROUPS.items():
        source = getattr(prompt_library, group_name)
        groups[group_name] = SimpleNamespace(
            **{
                name: getattr(source, name)
                for name in function_names
                if (group_name, name) != ('dedupe_edges', 'resolve_edges_batch')
            }
        )

    validate_prompt_library(SimpleNamespace(**groups))
