"""Phase E tests: request sizing, ceiling semantics, and context-length classification.

Covers the generic mechanism in ``graphiti_core.llm_client.request_guard``:
conservative estimation, disabled/no-ceiling semantics, structural diagnostics
without prompt content, and narrow provider context-limit classification.
"""

import pytest
from pydantic import BaseModel

from graphiti_core.errors import GraphitiError, GraphitiRequestTooLargeError
from graphiti_core.llm_client.request_guard import (
    CHARS_PER_TOKEN,
    LLMRequestContext,
    RequestGuard,
    build_request_context,
    describe_request_size,
    enforce_request_ceiling,
    estimate_request_tokens,
    is_context_length_error,
)


class _Model(BaseModel):
    foo: str


# ---------------------------------------------------------------------------
# Sizing / estimation
# ---------------------------------------------------------------------------


def test_estimate_is_conservative_ceiling_division():
    # 400 chars / 3 -> 133.34, ceiling-divided to 134.
    assert estimate_request_tokens(400) == 134
    assert estimate_request_tokens(0) == 1
    assert estimate_request_tokens(1) == 1
    assert estimate_request_tokens(3) == 1
    assert estimate_request_tokens(4) == 2


def test_char_factor_is_documented_value():
    assert CHARS_PER_TOKEN == 3


def test_describe_request_size_sorts_largest_first():
    messages = [
        {'role': 'system', 'content': 'abc'},
        {'role': 'user', 'content': 'x' * 10},
        {'role': 'user', 'content': 'yy'},
    ]

    total, per_message = describe_request_size(messages)

    assert total == 15
    assert [(m.index, m.role, m.chars) for m in per_message] == [
        (1, 'user', 10),
        (0, 'system', 3),
        (2, 'user', 2),
    ]


def test_describe_tolerates_non_dict_messages():
    total, per_message = describe_request_size([{'role': 'user', 'content': 'abc'}, 'junk', None])

    assert total == 3
    assert len(per_message) == 3
    assert all(isinstance(m.chars, int) for m in per_message)


def test_build_request_context_carries_structure_not_content():
    context = build_request_context(
        model='m',
        endpoint='http://localhost:8080/v1',
        messages=[{'role': 'user', 'content': 'secret-prompt-body ' * 10}],
        group_id='episode-1',
        prompt_name='extract_nodes',
    )

    assert context.model == 'm'
    assert context.endpoint == 'http://localhost:8080/v1'
    assert context.message_count == 1
    assert context.total_chars == len('secret-prompt-body ' * 10)
    assert context.estimated_tokens == estimate_request_tokens(context.total_chars)
    assert context.group_id == 'episode-1'
    assert context.prompt_name == 'extract_nodes'
    assert 'secret-prompt-body' not in repr(context)
    assert 'secret-prompt-body' not in context.describe_largest_messages()


# ---------------------------------------------------------------------------
# Ceiling semantics
# ---------------------------------------------------------------------------


def _context(chars: int) -> LLMRequestContext:
    return build_request_context(
        model='m', endpoint=None, messages=[{'role': 'user', 'content': 'x' * chars}]
    )


def test_none_ceiling_disables_the_check():
    enforce_request_ceiling(_context(90_000), None)  # must not raise


def test_zero_ceiling_disables_the_check():
    enforce_request_ceiling(_context(90_000), 0)  # must not raise


def test_within_ceiling_does_not_raise():
    enforce_request_ceiling(_context(300), 1000)  # 100 est. tokens <= 1000


def test_oversize_raises_before_send_with_structural_diagnostics():
    with pytest.raises(GraphitiRequestTooLargeError) as exc_info:
        enforce_request_ceiling(_context(30_000), 5_000)

    message = str(exc_info.value)
    assert '10,000' in message  # 30_000 chars / 3
    assert '5,000' in message
    assert 'Not sent' in message
    assert '[0] user=30000c' in message


def test_rejection_error_is_the_public_d5_exception():
    assert issubclass(GraphitiRequestTooLargeError, GraphitiError)
    with pytest.raises(GraphitiRequestTooLargeError):
        enforce_request_ceiling(_context(30_000), 1)


# ---------------------------------------------------------------------------
# Context-length classification
# ---------------------------------------------------------------------------


class _ProviderError(Exception):
    def __init__(self, message: str, code: object = '', body: object = None):
        super().__init__(message)
        self.code = code
        self.body = body


@pytest.mark.parametrize(
    'error',
    [
        RuntimeError('context_length_exceeded'),
        RuntimeError("This model's maximum context length is 128000 tokens"),
        _ProviderError('failed', code='context_length_exceeded'),
        _ProviderError('failed', body={'error': {'code': 'context_length_exceeded'}}),
    ],
)
def test_context_length_errors_are_classified(error: Exception):
    assert is_context_length_error(error) is True


@pytest.mark.parametrize(
    'error',
    [
        RuntimeError('invalid response schema'),
        ValueError('Your response was not valid JSON'),
        _ProviderError('failed', code='invalid_request_error'),
        _ProviderError('failed', body={'error': {'code': 'authentication_error'}}),
        _ProviderError('failed', body='a string body'),
        KeyError('missing'),
    ],
)
def test_unrelated_errors_are_not_context_length(error: Exception):
    assert is_context_length_error(error) is False


def test_nested_body_code_classifies_even_with_generic_top_level_code():
    # The nested code is inspected independently: a generic top-level code must not
    # mask a nested context_length_exceeded.
    error = _ProviderError(
        'failed',
        code='invalid_request_error',
        body={'error': {'code': 'context_length_exceeded'}},
    )
    assert is_context_length_error(error) is True


def test_nested_non_matching_body_code_does_not_classify():
    error = _ProviderError(
        'failed', code='invalid_request_error', body={'error': {'code': 'bad gateway'}}
    )
    assert is_context_length_error(error) is False


def test_structured_code_wins_over_textual_heuristic_miss():
    # code present but different, body absent -> not classified even though the
    # message mentions other provider jargon.
    assert is_context_length_error(_ProviderError('over the limit', code='bad_request')) is False


# ---------------------------------------------------------------------------
# Guard container
# ---------------------------------------------------------------------------


def test_default_guard_has_no_hooks():
    guard = RequestGuard()

    assert guard.ceiling_resolver is None
    assert guard.lifecycle_hook is None
    assert guard.failure_listener is None
