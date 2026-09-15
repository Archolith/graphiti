"""Request sizing, ceiling enforcement, and context-length classification.

Fork-native mechanism introduced by Phase E of the soft-fork migration (installer #17,
``_patch_graphiti_openai_generic_client``). This module is generic and policy-free: it
owns the *mechanism* — a documented conservative request-size estimate, an optional
pre-send ceiling, narrow provider context-length classification, and typed
request-scoped hook seams — while the *policy* stays outside the library:

- where an effective ceiling comes from (e.g. probing a local inference server's
  context window) is a :class:`RequestCeilingResolver` supplied by the consumer;
- failure diagnostics and lifecycle telemetry are consumer callbacks
  (:class:`RequestFailureListener`, :class:`RequestLifecycleHook`) and receive an
  explicitly bounded :class:`LLMResponseMetadata` (status, response id, raw length,
  bounded raw preview, duration) so consumers can reproduce their own diagnostic
  records without the fork logging or retaining anything;
- ``group_id`` is a partition/namespace and ``prompt_name`` an operation name — they
  are NOT intrinsically a unique per-episode key. A consumer needing per-request
  correlation supplies an opaque correlation id through a
  :class:`RequestCorrelationProvider` — a narrow seam that receives the immutable
  measured context and returns ``str | None``; the fork itself creates the final
  context, so consumer code cannot alter fork-measured structural sizing facts.

Everything is optional: with no :class:`RequestGuard` configured the LLM client behaves
exactly as before. All request state is created per call and passed as arguments — no
module-global mutable state, no ``ContextVar``, no symbol rebinding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..errors import GraphitiRequestTooLargeError

#: Conservative chars-per-token factor for the assembled-request estimate. Local-model
#: and code-heavy Graphiti prompts (JSON schemas, candidate lists) tokenize denser than
#: prose, so 3 chars/token deliberately over-estimates; a ceiling derived from this
#: estimate errs on the safe side. This is an ESTIMATE for a guard, not a tokenizer.
CHARS_PER_TOKEN = 3

#: How many of the largest messages (by char count) to name in structural diagnostics.
#: Diagnostics expose roles, indices, and sizes only — never prompt content.
LARGEST_MESSAGES_DIAGNOSTIC_COUNT = 5

#: Maximum characters of the model's RAW RESPONSE carried in
#: :attr:`LLMResponseMetadata.raw_preview`. The response is model output (not prompt
#: content), but it is still bounded so diagnostic hooks cannot silently retain
#: unbounded payloads.
RAW_PREVIEW_LIMIT = 240

#: Phase of the request pipeline a failure listener is invoked for.
RequestFailurePhase = Literal['ceiling_rejected', 'provider_error']


@dataclass(frozen=True)
class MessageSize:
    """Structural size facts about one assembled provider message (no content)."""

    index: int
    role: str
    chars: int


@dataclass(frozen=True)
class LLMRequestContext:
    """Borrowed, request-scoped facts about one assembled LLM request.

    Carries structural diagnostics only — never message content — so it is safe to
    hand to consumer hooks (telemetry, failure diagnostics, ceiling derivation). A
    fresh instance is built per request; hooks must not retain it beyond the call.

    ``group_id`` is a partition/namespace identifier and ``prompt_name`` names the
    prompt/operation; neither is intrinsically a unique per-episode key. Consumers
    needing per-request correlation supply an opaque ``correlation_id`` through a
    ``RequestCorrelationProvider``; the fork creates the final context, so the
    measured structural fields are not consumer-writable.
    """

    model: str
    endpoint: str | None
    message_count: int
    total_chars: int
    estimated_tokens: int
    largest_messages: tuple[MessageSize, ...]
    group_id: str | None = None
    prompt_name: str | None = None
    correlation_id: str | None = None

    def describe_largest_messages(self) -> str:
        """Render the largest-messages breakdown, largest first."""
        return ', '.join(
            f'[{item.index}] {item.role}={item.chars}c' for item in self.largest_messages
        )


@dataclass(frozen=True)
class LLMResponseMetadata:
    """Bounded, generic metadata about one provider response (or absence of one).

    Delivered to lifecycle/failure hooks so consumers can build their own
    diagnostics without the fork implementing logging policy. ``raw_preview`` is
    bounded to :data:`RAW_PREVIEW_LIMIT` characters; the full raw response is never
    retained by the fork. ``duration_ms`` is the provider call wall clock when the
    metadata is built on completion, and ``None`` when unavailable (e.g. metadata
    attached to a failure that occurred before or during the call).
    """

    status: int | str | None
    response_id: str | None
    raw_length: int
    raw_preview: str
    duration_ms: int | None = None


class RequestCeilingResolver(Protocol):
    """Resolves the effective estimated-token ceiling for one request.

    Semantics of the returned int:

    - ``None`` — no ceiling configured for this request (check disabled);
    - ``0`` — the check is explicitly disabled (a deliberate opt-out);
    - a positive int — reject the request *before sending* when its conservative
      estimated-token size exceeds the value.

    The derivation policy (probing endpoints, caching, TTLs) belongs entirely to the
    consumer; this protocol is the only seam the library sees.
    """

    async def resolve_request_ceiling(self, context: LLMRequestContext) -> int | None: ...


class RequestLifecycleHook(Protocol):
    """Observes the lifecycle of one assembled LLM request.

    ``on_request_started`` is invoked immediately before the provider call (after
    ceiling enforcement, so rejected requests never reach it as "started" — they are
    reported through :class:`RequestFailureListener` instead). ``on_request_completed``
    is invoked exactly once per successful request with bounded response metadata
    (status, response id, raw length/preview, and duration). Hook exceptions
    propagate and abort the request, leaving no state to reset; implementations that
    must not break ingestion should catch their own errors.
    """

    def on_request_started(self, context: LLMRequestContext) -> None: ...

    def on_request_completed(
        self, context: LLMRequestContext, response: LLMResponseMetadata
    ) -> None: ...


class RequestFailureListener(Protocol):
    """Receives structured failure diagnostics for one LLM request.

    Invoked exactly once per failed request with the request context, the error, the
    phase the failure occurred in, and the response metadata when a response exists
    (``None`` for pre-send ceiling rejections and for failures where the provider
    never returned a response, e.g. transport errors and rate limits). The listener
    is diagnostic-only: it cannot alter control flow, and exceptions it raises
    propagate to the caller. It is never invoked for successful requests.
    """

    def on_request_failed(
        self,
        context: LLMRequestContext,
        error: Exception,
        phase: RequestFailurePhase,
        response: LLMResponseMetadata | None,
    ) -> None: ...


class RequestCorrelationProvider(Protocol):
    """Supplies the opaque per-request correlation id for one request.

    Invoked once per request, immediately after the measured context is built and
    before any other hook sees it. Receives the immutable measured context and must
    return ``str | None`` — nothing else. The fork itself produces the final context
    (``dataclasses.replace(context, correlation_id=...)``), so a provider CANNOT
    alter the fork-measured structural facts (model, endpoint, message counts,
    sizes, estimated tokens, largest messages); a non-string return is ignored and
    the request proceeds with ``correlation_id=None``. Provider exceptions propagate
    and abort the request with no state to reset.
    """

    def resolve_correlation(self, context: LLMRequestContext) -> str | None: ...


@dataclass(frozen=True)
class RequestGuard:
    """Optional, typed bundle of request-scoped hooks for an LLM client.

    Passed to a client at construction (e.g.
    ``OpenAIGenericClient(..., request_guard=...)``). Every member is optional; an
    all-default guard behaves like no guard at all. The guard is immutable and owned
    by the client instance — there is no module-global request state anywhere on this
    path.
    """

    ceiling_resolver: RequestCeilingResolver | None = None
    lifecycle_hook: RequestLifecycleHook | None = None
    failure_listener: RequestFailureListener | None = None
    correlation_provider: RequestCorrelationProvider | None = None


def describe_request_size(messages: Sequence[Any]) -> tuple[int, list[MessageSize]]:
    """Return ``(total_chars, per-message sizes)`` for assembled provider messages.

    Sizes are char counts of the ``content`` of each mapping-shaped message, largest
    first in the per-message list. Non-dict messages count as zero chars rather than
    raising, so unusual provider payloads cannot break the guard itself.
    """
    per_message: list[MessageSize] = []
    for index, message in enumerate(messages):
        if isinstance(message, dict):
            role = str(message.get('role', '?'))
            content = str(message.get('content', ''))
        else:
            role = '?'
            content = ''
        per_message.append(MessageSize(index=index, role=role, chars=len(content)))
    return sum(item.chars for item in per_message), sorted(
        per_message, key=lambda item: -item.chars
    )


def estimate_request_tokens(total_chars: int) -> int:
    """Estimate the token size of an assembled request from its char count.

    Uses the documented conservative :data:`CHARS_PER_TOKEN` factor (ceiling
    division, minimum 1). Deliberately NOT a tokenizer: the guard needs a stable,
    dependency-free, slightly pessimistic estimate.
    """
    return max(1, (total_chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def build_request_context(
    *,
    model: str,
    endpoint: str | None,
    messages: Sequence[Any],
    group_id: str | None = None,
    prompt_name: str | None = None,
) -> LLMRequestContext:
    """Build the request-scoped context for one assembled provider request."""
    total_chars, per_message = describe_request_size(messages)
    return LLMRequestContext(
        model=model,
        endpoint=endpoint,
        message_count=len(messages),
        total_chars=total_chars,
        estimated_tokens=estimate_request_tokens(total_chars),
        largest_messages=tuple(per_message[:LARGEST_MESSAGES_DIAGNOSTIC_COUNT]),
        group_id=group_id,
        prompt_name=prompt_name,
    )


def build_response_metadata(
    response: Any, *, raw: str = '', duration_ms: int | None = None
) -> LLMResponseMetadata:
    """Build bounded response metadata from a provider response object.

    ``raw`` is the model's raw textual response (pre-parsing); it is not retained by
    the fork — only its length and a :data:`RAW_PREVIEW_LIMIT`-character preview are
    kept. Tolerates unusual provider payloads: missing attributes resolve to
    ``None``, and ``duration_ms`` stays ``None`` when unavailable.
    """
    raw = str(raw or '')
    return LLMResponseMetadata(
        status=getattr(response, 'status_code', None),
        response_id=getattr(response, 'id', None),
        raw_length=len(raw),
        raw_preview=raw[:RAW_PREVIEW_LIMIT],
        duration_ms=duration_ms,
    )


def enforce_request_ceiling(context: LLMRequestContext, ceiling: int | None) -> None:
    """Reject an oversized assembled request BEFORE it is sent.

    ``ceiling`` follows the :class:`RequestCeilingResolver` semantics: ``None`` or
    ``0`` disables the check; a positive value is compared against the context's
    conservative token estimate. Raises :class:`GraphitiRequestTooLargeError` with
    structural diagnostics (sizes, roles, indices — never prompt content) so the
    caller sees the rejection's cause instead of a downstream provider 400.
    """
    if not ceiling:
        return
    if context.estimated_tokens > ceiling:
        raise GraphitiRequestTooLargeError(
            f'Assembled LLM request is ~{context.estimated_tokens:,} estimated tokens '
            f'(chars/3 conservative estimate), over the {ceiling:,} ceiling. '
            f'{context.message_count} messages, {context.total_chars:,} chars; largest: '
            f'{context.describe_largest_messages()}. Not sent — the provider would '
            f'reject it as a context-length error.'
        )


def is_context_length_error(exc: Exception) -> bool:
    """Return True when a provider rejected the request specifically for context size.

    Classification is deliberately narrow:

    1. Structured data — the exception's ``code`` attribute equal to
       ``context_length_exceeded``, OR a dict ``body`` whose ``error.code`` equals
       it; the two are inspected independently, so a nested
       ``context_length_exceeded`` classifies even when the top-level code is a
       generic one (e.g. ``invalid_request_error``).
    2. Recognized textual fallbacks — the exception's string form contains
       ``context_length_exceeded`` or ``maximum context length``.

    Unrelated bad requests (invalid schema, auth, malformed body) and local parse
    failures do not match and retain their normal behavior.
    """
    code = str(getattr(exc, 'code', '') or '').strip().lower()
    body = getattr(exc, 'body', None)
    body_code = ''
    if isinstance(body, dict):
        error = body.get('error')
        if isinstance(error, dict):
            body_code = str(error.get('code') or '').strip().lower()
    if code == 'context_length_exceeded' or body_code == 'context_length_exceeded':
        return True

    rendered = str(exc).lower()
    return 'context_length_exceeded' in rendered or 'maximum context length' in rendered
