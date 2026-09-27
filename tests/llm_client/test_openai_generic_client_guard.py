"""Phase E tests: guard integration in OpenAIGenericClient (no network).

Covers: pre-send oversize rejection with the exact public exception, disabled /
within-ceiling behavior, provider context-length normalization (structured and
textual) without an unchanged retry, no-hook / optional-hook compatibility,
response-shape variants, and hook isolation across requests.
"""

import json
from types import SimpleNamespace

import openai
import pytest
from pydantic import BaseModel

from graphiti_core.errors import GraphitiRequestTooLargeError
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.errors import EmptyResponseError, RateLimitError
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.llm_client.request_guard import (
    LLMRequestContext,
    LLMResponseMetadata,
    RequestGuard,
)
from graphiti_core.prompts.models import Message


class ResponseModel(BaseModel):
    foo: str


class DummyChatCompletions:
    def __init__(self, content: str = '{"foo": "bar"}', error: Exception | None = None):
        self.create_calls: list[dict] = []
        self._content = content
        self._error = error

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


class DummyClient:
    def __init__(self, completions: DummyChatCompletions, base_url: str | None = None):
        self.chat = SimpleNamespace(completions=completions)
        self.base_url = base_url


def _messages() -> list[Message]:
    return [
        Message(role='system', content='system message'),
        Message(role='user', content='user message'),
    ]


class RecordingResolver:
    def __init__(self, ceiling: int | None):
        self.ceiling = ceiling
        self.contexts: list[LLMRequestContext] = []

    async def resolve_request_ceiling(self, context: LLMRequestContext) -> int | None:
        self.contexts.append(context)
        return self.ceiling


class RecordingLifecycle:
    def __init__(self):
        self.started: list[LLMRequestContext] = []
        self.completed: list[tuple[LLMRequestContext, LLMResponseMetadata]] = []

    def on_request_started(self, context: LLMRequestContext) -> None:
        self.started.append(context)

    def on_request_completed(
        self, context: LLMRequestContext, response: LLMResponseMetadata
    ) -> None:
        self.completed.append((context, response))


class RecordingFailureListener:
    def __init__(self):
        self.failures: list[
            tuple[LLMRequestContext, Exception, str, LLMResponseMetadata | None]
        ] = []

    def on_request_failed(
        self,
        context: LLMRequestContext,
        error: Exception,
        phase: str,
        response: LLMResponseMetadata | None,
    ) -> None:
        self.failures.append((context, error, phase, response))


class RecordingCorrelationProvider:
    def __init__(self, result: object = None):
        self.seen: list[LLMRequestContext] = []
        self.result = result

    def resolve_correlation(self, context: LLMRequestContext) -> str | None:
        self.seen.append(context)
        if self.result is None:
            return f'episode-{len(self.seen)}'
        return self.result


class TamperedContext:
    """Hostile stand-in pretending to be a replacement measured context."""

    model = 'owned-model'
    endpoint = 'http://owned'
    message_count = 1
    total_chars = 1
    estimated_tokens = 0
    largest_messages = ()


def _make_client(
    content: str = '{"foo": "bar"}',
    error: Exception | None = None,
    guard: RequestGuard | None = None,
    base_url: str | None = 'http://127.0.0.1:8080/v1',
    model: str = 'test-model',
):
    completions = DummyChatCompletions(content=content, error=error)
    client = OpenAIGenericClient(
        config=LLMConfig(api_key='test', model=model),
        client=DummyClient(completions, base_url=base_url),
        request_guard=guard,
    )
    return client, completions


# ---------------------------------------------------------------------------
# Pre-send ceiling enforcement
# ---------------------------------------------------------------------------


async def test_oversize_request_is_rejected_before_send():
    resolver = RecordingResolver(ceiling=100)
    listener = RecordingFailureListener()
    client, completions = _make_client(
        guard=RequestGuard(ceiling_resolver=resolver, failure_listener=listener)
    )
    oversized = [
        Message(role='system', content='system message'),
        Message(role='user', content='x' * 10_000),
    ]

    with pytest.raises(GraphitiRequestTooLargeError):
        await client.generate_response(oversized)

    assert completions.create_calls == [], 'oversize request must never reach the provider'
    assert [phase for _, _, phase, _ in listener.failures] == ['ceiling_rejected']
    assert isinstance(listener.failures[0][1], GraphitiRequestTooLargeError)


async def test_oversize_rejection_is_not_retried():
    # Tenacity must not re-attempt: the payload is deterministic and retry prompts
    # only grow it.
    resolver = RecordingResolver(ceiling=10)
    client, completions = _make_client(guard=RequestGuard(ceiling_resolver=resolver))

    with pytest.raises(GraphitiRequestTooLargeError):
        await client.generate_response([Message(role='user', content='x' * 10_000)])

    assert len(completions.create_calls) == 0


async def test_request_within_ceiling_is_sent():
    resolver = RecordingResolver(ceiling=100_000)
    client, completions = _make_client(guard=RequestGuard(ceiling_resolver=resolver))

    result = await client.generate_response(_messages(), response_model=ResponseModel)

    assert result == {'foo': 'bar'}
    assert len(completions.create_calls) == 1


async def test_none_ceiling_means_no_check():
    resolver = RecordingResolver(ceiling=None)
    client, completions = _make_client(guard=RequestGuard(ceiling_resolver=resolver))

    result = await client.generate_response(_messages())

    assert result == {'foo': 'bar'}
    assert len(completions.create_calls) == 1


async def test_zero_ceiling_explicitly_disables_the_check():
    resolver = RecordingResolver(ceiling=0)
    client, completions = _make_client(guard=RequestGuard(ceiling_resolver=resolver))

    result = await client.generate_response(_messages())

    assert result == {'foo': 'bar'}
    assert len(completions.create_calls) == 1


async def test_resolver_receives_request_scoped_context():
    resolver = RecordingResolver(ceiling=100_000)
    client, _ = _make_client(guard=RequestGuard(ceiling_resolver=resolver))

    await client.generate_response(_messages(), group_id='episode-42', prompt_name='extract_nodes')

    context = resolver.contexts[0]
    assert context.group_id == 'episode-42'
    assert context.prompt_name == 'extract_nodes'
    assert context.model == 'test-model'
    assert context.endpoint == 'http://127.0.0.1:8080/v1'
    assert context.message_count == 2
    assert 'user message' not in repr(context)


# ---------------------------------------------------------------------------
# Provider context-length normalization
# ---------------------------------------------------------------------------


def _openai_context_length_error() -> openai.BadRequestError:
    request = SimpleNamespace(
        method='POST', url='http://127.0.0.1:8080/v1/chat/completions', headers={}
    )
    response = SimpleNamespace(status_code=400, headers={}, request=request)
    return openai.BadRequestError(
        message="This model's maximum context length is 4096 tokens",
        response=response,
        body={'error': {'code': 'context_length_exceeded'}},
    )


async def test_provider_context_length_error_normalizes_to_public_exception():
    listener = RecordingFailureListener()
    client, completions = _make_client(
        error=_openai_context_length_error(),
        guard=RequestGuard(failure_listener=listener),
    )

    with pytest.raises(GraphitiRequestTooLargeError) as exc_info:
        await client.generate_response(_messages())

    assert len(completions.create_calls) == 1, 'context-length failure must not be retried'
    assert isinstance(exc_info.value.__cause__, openai.BadRequestError)
    assert [phase for _, _, phase, _ in listener.failures] == ['provider_error']


async def test_textual_context_length_error_normalizes_without_retry():
    client, completions = _make_client(error=RuntimeError('context_length_exceeded'))

    with pytest.raises(GraphitiRequestTooLargeError):
        await client.generate_response(_messages())

    assert len(completions.create_calls) == 1


async def test_unrelated_bad_request_keeps_normal_behavior():
    client, completions = _make_client(error=ValueError('invalid response schema'))

    with pytest.raises(ValueError):
        await client.generate_response(_messages())

    assert len(completions.create_calls) == 1


def test_context_length_error_is_not_retryable_by_the_base_wrapper():
    from graphiti_core.llm_client.client import is_server_or_retry_error

    assert is_server_or_retry_error(GraphitiRequestTooLargeError('too big')) is False


# ---------------------------------------------------------------------------
# No-guard compatibility
# ---------------------------------------------------------------------------


async def test_no_guard_keeps_legacy_behavior_and_wire_shape():
    client, completions = _make_client()

    result = await client.generate_response(
        _messages(), response_model=ResponseModel, group_id='g', prompt_name='p'
    )

    call = completions.create_calls[0]
    assert result == {'foo': 'bar'}
    assert call['response_format']['type'] == 'json_schema'
    assert call['max_tokens'] == 16384
    assert 'max_completion_tokens' not in call


async def test_guard_with_no_hooks_behaves_like_no_guard():
    client, completions = _make_client(guard=RequestGuard())

    result = await client.generate_response(_messages())

    assert result == {'foo': 'bar'}
    assert len(completions.create_calls) == 1


# ---------------------------------------------------------------------------
# Lifecycle hooks and isolation
# ---------------------------------------------------------------------------


async def test_lifecycle_hooks_observe_started_and_completed_with_response_metadata():
    lifecycle = RecordingLifecycle()
    resolver = RecordingResolver(ceiling=100_000)
    client, completions = _make_client(
        guard=RequestGuard(ceiling_resolver=resolver, lifecycle_hook=lifecycle)
    )

    await client.generate_response(_messages())

    assert len(lifecycle.started) == 1
    assert len(lifecycle.completed) == 1
    context, metadata = lifecycle.completed[0]
    assert context is lifecycle.started[0]
    assert isinstance(metadata, LLMResponseMetadata)
    assert metadata.duration_ms is not None and metadata.duration_ms >= 0
    assert metadata.raw_length == len('{"foo": "bar"}')
    assert metadata.raw_preview == '{"foo": "bar"}'


async def test_rejected_requests_do_not_emit_lifecycle_started():
    lifecycle = RecordingLifecycle()
    resolver = RecordingResolver(ceiling=10)
    client, completions = _make_client(
        guard=RequestGuard(ceiling_resolver=resolver, lifecycle_hook=lifecycle)
    )

    with pytest.raises(GraphitiRequestTooLargeError):
        await client.generate_response([Message(role='user', content='x' * 10_000)])

    assert lifecycle.started == []
    assert lifecycle.completed == []


async def test_no_cross_request_context_leakage():
    resolver = RecordingResolver(ceiling=100_000)
    lifecycle = RecordingLifecycle()
    client, _ = _make_client(
        guard=RequestGuard(ceiling_resolver=resolver, lifecycle_hook=lifecycle)
    )

    await client.generate_response(_messages(), group_id='episode-1')
    await client.generate_response(_messages(), group_id='episode-2')

    assert [c.group_id for c in resolver.contexts] == ['episode-1', 'episode-2']
    assert lifecycle.started[0] is not lifecycle.started[1]
    assert lifecycle.started[0].group_id == 'episode-1'
    assert lifecycle.started[1].group_id == 'episode-2'


async def test_sequential_requests_get_independent_contexts():
    lifecycle = RecordingLifecycle()
    client, _ = _make_client(guard=RequestGuard(lifecycle_hook=lifecycle))

    await client.generate_response(_messages())
    await client.generate_response(_messages())

    first, second = lifecycle.started
    assert first is not second
    assert first.estimated_tokens == second.estimated_tokens


# ---------------------------------------------------------------------------
# Response parsing / shape variants
# ---------------------------------------------------------------------------


async def test_prose_wrapped_json_is_normalized():
    client, _ = _make_client(content='Here is your JSON: {"foo": "bar"} — hope it helps!')

    result = await client.generate_response(_messages())

    assert result == {'foo': 'bar'}


async def test_fenced_json_still_parses():
    client, _ = _make_client(content='```json\n{"foo": "bar"}\n```')

    assert await client.generate_response(_messages()) == {'foo': 'bar'}


async def test_array_payload_parses():
    client, _ = _make_client(content='[1, 2, 3]')

    assert await client.generate_response(_messages()) == [1, 2, 3]


async def test_unparseable_output_still_raises_json_decode_error():
    # Parse failures must keep their normal (retryable) classification; asserted at
    # the _generate_response level because the base retry would sleep real backoff.
    client, completions = _make_client(content='not json at all')

    with pytest.raises(json.JSONDecodeError):
        await client._generate_response(_messages())

    assert len(completions.create_calls) == 1


async def test_empty_choices_raises_empty_response_error():
    completions = DummyChatCompletions()
    client = OpenAIGenericClient(
        config=LLMConfig(api_key='test', model='test-model'),
        client=DummyClient(completions),
    )

    class _NoChoices:
        choices = []

    async def _create(**kwargs):
        return _NoChoices()

    completions.create = _create  # type: ignore[method-assign]

    with pytest.raises(EmptyResponseError):
        await client._generate_response(_messages())


# ---------------------------------------------------------------------------
# Provider token-parameter compatibility
# ---------------------------------------------------------------------------


async def test_gpt5_models_use_max_completion_tokens():
    client, completions = _make_client(model='gpt-5-mini')

    await client.generate_response(_messages(), max_tokens=512)

    call = completions.create_calls[0]
    assert call['max_completion_tokens'] == 512
    assert 'max_tokens' not in call


async def test_reasoning_models_use_max_completion_tokens():
    client, completions = _make_client(model='o3')

    await client.generate_response(_messages(), max_tokens=512)

    call = completions.create_calls[0]
    assert call['max_completion_tokens'] == 512
    assert 'max_tokens' not in call


async def test_standard_models_keep_max_tokens():
    client, completions = _make_client(model='qwen3-32b')

    await client.generate_response(_messages(), max_tokens=512)

    call = completions.create_calls[0]
    assert call['max_tokens'] == 512
    assert 'max_completion_tokens' not in call


# ---------------------------------------------------------------------------
# Failure metadata seam (findings 1 + 2)
# ---------------------------------------------------------------------------


class _IdClient(DummyClient):
    pass


def _make_identifying_client(
    content: str, error: Exception | None = None, guard: RequestGuard | None = None
):
    """Client whose fake provider response carries status/id like the OpenAI SDK."""
    completions = DummyChatCompletions(content=content, error=error)

    async def create(**kwargs):
        completions.create_calls.append(kwargs)
        if error is not None:
            raise error
        message = SimpleNamespace(content=content)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            status_code=200,
            id='resp-123',
        )

    completions.create = create  # type: ignore[method-assign]
    client = OpenAIGenericClient(
        config=LLMConfig(api_key='test', model='test-model'),
        client=DummyClient(completions, base_url='http://127.0.0.1:8080/v1'),
        request_guard=guard,
    )
    return client, completions


async def test_parse_failure_exposes_response_metadata_exactly_once():
    listener = RecordingFailureListener()
    unparseable = 'x' * 500 + ' not json'
    client, completions = _make_identifying_client(
        content=unparseable,
        guard=RequestGuard(failure_listener=listener),
    )

    with pytest.raises(json.JSONDecodeError):
        await client._generate_response(_messages())

    assert len(completions.create_calls) == 1
    assert len(listener.failures) == 1
    context, error, phase, metadata = listener.failures[0]
    assert phase == 'provider_error'
    assert isinstance(error, json.JSONDecodeError)
    assert metadata is not None
    assert metadata.status == 200
    assert metadata.response_id == 'resp-123'
    assert metadata.raw_length == len(unparseable)
    # Bounded preview gives a consumer enough to build its own diagnostics record.
    assert metadata.raw_preview.startswith('xxxx')
    assert len(metadata.raw_preview) <= 240
    assert metadata.duration_ms is not None


async def test_empty_response_failure_carries_metadata_exactly_once():
    listener = RecordingFailureListener()
    client, completions = _make_identifying_client(
        content='',
        guard=RequestGuard(failure_listener=listener),
    )

    with pytest.raises(EmptyResponseError):
        await client._generate_response(_messages())

    assert len(listener.failures) == 1
    _, error, phase, metadata = listener.failures[0]
    assert isinstance(error, EmptyResponseError)
    assert phase == 'provider_error'
    assert metadata is not None and metadata.status == 200


async def test_rate_limit_failure_is_observable_exactly_once():
    listener = RecordingFailureListener()
    rate_limit = openai.RateLimitError(
        message='slow down',
        response=SimpleNamespace(status_code=429, headers={}, request=None),
        body=None,
    )
    client, completions = _make_identifying_client(
        content='{"foo": "bar"}',
        error=rate_limit,
        guard=RequestGuard(failure_listener=listener),
    )

    with pytest.raises(RateLimitError) as exc_info:
        await client._generate_response(_messages())

    # Translated Graphiti error preserved, chained from the provider error...
    assert isinstance(exc_info.value, RateLimitError)
    assert exc_info.value.__cause__ is rate_limit
    # ...and the generic seam observed the failure exactly once.
    assert len(listener.failures) == 1
    _, error, phase, metadata = listener.failures[0]
    assert error is rate_limit
    assert phase == 'provider_error'
    assert metadata is None, 'no response exists when the provider call itself fails'


async def test_context_length_failure_observed_exactly_once_before_normalization():
    listener = RecordingFailureListener()
    provider_error = _openai_context_length_error()
    client, completions = _make_identifying_client(
        content='{"foo": "bar"}',
        error=provider_error,
        guard=RequestGuard(failure_listener=listener),
    )

    with pytest.raises(GraphitiRequestTooLargeError):
        await client._generate_response(_messages())

    assert len(listener.failures) == 1
    _, error, phase, _ = listener.failures[0]
    assert error is provider_error
    assert phase == 'provider_error'


async def test_ceiling_rejection_failure_carries_no_response_metadata():
    resolver = RecordingResolver(ceiling=10)
    listener = RecordingFailureListener()
    client, completions = _make_client(
        guard=RequestGuard(ceiling_resolver=resolver, failure_listener=listener)
    )

    with pytest.raises(GraphitiRequestTooLargeError):
        await client.generate_response([Message(role='user', content='x' * 10_000)])

    assert len(completions.create_calls) == 0
    assert len(listener.failures) == 1
    _, error, phase, metadata = listener.failures[0]
    assert phase == 'ceiling_rejected'
    assert metadata is None


# ---------------------------------------------------------------------------
# Correlation seam (narrow; docs correction)
# ---------------------------------------------------------------------------


async def test_correlation_provider_populates_correlation_id_for_all_hooks():
    provider = RecordingCorrelationProvider()
    lifecycle = RecordingLifecycle()
    resolver = RecordingResolver(ceiling=100_000)
    client, completions = _make_client(
        guard=RequestGuard(
            ceiling_resolver=resolver,
            lifecycle_hook=lifecycle,
            correlation_provider=provider,
        )
    )

    await client.generate_response(_messages(), group_id='partition-a', prompt_name='extract_nodes')

    # The raw context group_id is a partition, not an episode key; the provider
    # supplied the opaque per-request correlation id.
    assert provider.seen[0].group_id == 'partition-a'
    assert provider.seen[0].correlation_id is None
    assert resolver.contexts[0].correlation_id == 'episode-1'
    assert lifecycle.started[0].correlation_id == 'episode-1'


async def test_correlation_provider_cannot_alter_measured_facts():
    # HOSTILE: the provider returns a tampered replacement "context" instead of a
    # correlation string. The narrow seam must ignore it: the fork-measured sizing
    # facts stay intact everywhere downstream and correlation_id stays None.
    provider = RecordingCorrelationProvider(result=TamperedContext())
    resolver = RecordingResolver(ceiling=100_000)
    lifecycle = RecordingLifecycle()
    client, completions = _make_client(
        guard=RequestGuard(
            ceiling_resolver=resolver,
            lifecycle_hook=lifecycle,
            correlation_provider=provider,
        )
    )

    result = await client.generate_response(_messages())

    assert result == {'foo': 'bar'}
    assert len(completions.create_calls) == 1
    measured = provider.seen[0]
    for context in [*resolver.contexts, *lifecycle.started]:
        # Measured structural facts are byte-identical to the fork-built context the
        # provider received — the tampered return was ignored wholesale.
        assert (context.model, context.endpoint, context.message_count) == (
            measured.model,
            measured.endpoint,
            measured.message_count,
        )
        assert context.total_chars == measured.total_chars
        assert context.estimated_tokens == measured.estimated_tokens
        assert context.largest_messages == measured.largest_messages
        assert context.estimated_tokens > 0
        assert context.correlation_id is None


async def test_non_string_correlation_result_is_ignored():
    provider = RecordingCorrelationProvider(result=12345)
    resolver = RecordingResolver(ceiling=100_000)
    client, _ = _make_client(
        guard=RequestGuard(ceiling_resolver=resolver, correlation_provider=provider)
    )

    await client.generate_response(_messages())

    assert resolver.contexts[0].correlation_id is None


async def test_provider_correlation_does_not_leak_across_requests():
    provider = RecordingCorrelationProvider()
    resolver = RecordingResolver(ceiling=100_000)
    client, _ = _make_client(
        guard=RequestGuard(ceiling_resolver=resolver, correlation_provider=provider)
    )

    await client.generate_response(_messages())
    await client.generate_response(_messages())

    assert [c.correlation_id for c in resolver.contexts] == ['episode-1', 'episode-2']


# ---------------------------------------------------------------------------
# Adversarial payload extraction (finding 3)
# ---------------------------------------------------------------------------


def test_payload_extraction_first_valid_object_before_later_fragments():
    text = '{"foo": "bar"} trailing junk { "broken"'
    assert OpenAIGenericClient._extract_json_payload(text) == '{"foo": "bar"}'


def test_payload_extraction_skips_invalid_leading_spans():
    text = 'preamble {broken {"foo": "bar"}'
    assert OpenAIGenericClient._extract_json_payload(text) == '{"foo": "bar"}'


def test_payload_extraction_multiple_valid_spans_returns_first():
    text = 'Answer: {"a": 1} and also {"b": 2}'
    assert OpenAIGenericClient._extract_json_payload(text) == '{"a": 1}'


def test_payload_extraction_trailing_malformed_brace_does_not_break_first_payload():
    text = '{"foo": "bar"} } { oops'
    assert OpenAIGenericClient._extract_json_payload(text) == '{"foo": "bar"}'


def test_payload_extraction_first_array_wins():
    text = '[1, 2] {"later": true}'
    assert OpenAIGenericClient._extract_json_payload(text) == '[1, 2]'


def test_payload_extraction_nested_object_returns_outermost_decodable():
    text = 'prefix {"a": {"b": 2}} suffix'
    assert OpenAIGenericClient._extract_json_payload(text) == '{"a": {"b": 2}}'


def test_payload_extraction_returns_original_when_nothing_decodes():
    text = 'no json here at all'
    assert OpenAIGenericClient._extract_json_payload(text) == text


async def test_payload_extraction_survives_end_to_end_with_trailing_fragment():
    client, _ = _make_client(content='{"foo": "bar"} note: {incomplete')

    assert await client.generate_response(_messages()) == {'foo': 'bar'}
