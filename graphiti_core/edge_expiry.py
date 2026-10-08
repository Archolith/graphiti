"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Neutral typed extension hook for an edge's own-end expiry.

Upstream ``resolve_extracted_edge`` expires every resolved edge that carries its own
``invalid_at``, even when nothing contradicted it. A caller that knows the edge's
``invalid_at`` is the end of the fact in world time (a finished trip, a past state) installs
an :class:`EdgeExpiryHook` on the ``Graphiti`` instance to answer ``WORLD_END`` instead.

The hook is deliberately policy-free: it observes one resolved edge and returns an explicit
decision. All evidence travels through ordinary function arguments for a single resolution
call; there is no module-global state, no ``ContextVar`` and no cache.

Invocation timing: once per ``resolve_extracted_edge`` call that reaches the expiry step,
only when the resolved edge (new or an existing duplicate) has ``invalid_at`` set and
``expired_at`` unset. The early-return (no candidates) and exact-fact fast paths never reach
it, as upstream never expires there either.

Effect of ``WORLD_END``:

- the edge is not expired for having its own ``invalid_at``;
- a contradicted candidate newer than the edge still supersedes it only when the candidate
  starts strictly before the edge's ``invalid_at`` (inside the fact's window). The edge's
  ``invalid_at`` then becomes the candidate's ``valid_at`` and it is expired. A candidate that
  starts at or after the end does not overlap the fact and is ignored;
- when the edge's ``valid_at`` is unknown, its window is open: any contradicted candidate that
  starts before the end expires it with ``invalid_at`` unchanged (the upstream result);
- an inverted window (``invalid_at <= valid_at``) is malformed and is expired as upstream;
- older contradicted edges are handled exactly as upstream.

``EXPIRE`` (and no hook) is upstream behavior.

Known limitations:

- the decision applies to ``resolved_edge``. In ``dedupe_edges_bulk`` (bulk pass 1) the
  resolved edge may be another unsaved edge of the same batch, and an ``EXPIRE`` written
  there is final, because later passes skip already-expired edges. A hook that keys its
  evidence by edge uuid should look up ``resolved_edge``, not only ``extracted_edge``;

When two extracted edges of one batch resolve to the same stored edge, each call works on its
own copy. ``reconcile_edge_copies`` (in ``edge_operations``) gives all copies the earliest
``expired_at`` and ``invalid_at`` before anything is saved, so a supersession found by one
copy is never overwritten by a live copy.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EpisodicNode


class EdgeExpiryDecision(Enum):
    """Explicit decision a hook returns for one resolved edge.

    - ``EXPIRE``: upstream behavior; the edge's own ``invalid_at`` expires it.
    - ``WORLD_END``: ``invalid_at`` is the fact's world-time end, not a supersession.
    """

    EXPIRE = 'expire'
    WORLD_END = 'world_end'


@dataclass(frozen=True)
class EdgeExpiryContext:
    """Borrowed request-local evidence for one edge-expiry decision.

    The dataclass is frozen, but the contained models are shared with the in-flight
    resolution call and are NOT copied. Hooks must treat them as read-only.

    - ``extracted_edge``: the newly extracted edge being resolved.
    - ``resolved_edge``: the edge resolution produced; the extracted edge itself, or the
      edge it was resolved to as a duplicate (a stored edge, or during bulk dedupe another
      edge of the same batch).
    - ``is_duplicate``: ``resolved_edge`` is not the extracted edge object.
    - ``episode``: the episode being ingested. On ``add_triplet`` it is a synthetic, empty
      episode node.
    """

    extracted_edge: EntityEdge
    resolved_edge: EntityEdge
    is_duplicate: bool
    episode: EpisodicNode | None


@runtime_checkable
class EdgeExpiryHook(Protocol):
    """Protocol for callers that decide whether an edge's own end expires it.

    It must return an :class:`EdgeExpiryDecision`; any other return value raises
    ``TypeError``. Exceptions raised by the hook propagate unchanged and abort the
    resolution call; because all evidence lives in per-call arguments, there is nothing to
    reset.
    """

    async def decide_edge_expiry(self, context: EdgeExpiryContext) -> EdgeExpiryDecision: ...


def evaluate_edge_expiry_decision(decision: object) -> EdgeExpiryDecision:
    """Validate a hook return value and return it as an ``EdgeExpiryDecision``.

    Raises ``TypeError`` for anything other than an ``EdgeExpiryDecision``.
    """
    if not isinstance(decision, EdgeExpiryDecision):
        raise TypeError(
            f'edge_expiry_hook must return an EdgeExpiryDecision, got {type(decision).__name__}'
        )
    return decision


def edge_expiry_kwargs(hook: EdgeExpiryHook | None) -> dict[str, Any]:
    """Keyword arguments that thread ``hook`` to a resolver call.

    Empty when there is no hook, so the no-hook call keeps the legacy signature.
    """
    return {} if hook is None else {'edge_expiry_hook': hook}
