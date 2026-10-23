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

Neutral typed extension hook to veto the invalidation of an edge.

Upstream ``resolve_extracted_edge`` is blind to who caused an edge to be invalidated or
expired. A caller installs an :class:`EdgeInvalidationHook` on the ``Graphiti`` instance to
answer ``KEEP`` when the cause is not allowed to end the target edge (for example a
lower-authority source). The hook is policy-free: it observes one (target, cause) pair and
returns an explicit decision. No module-global state is involved.

Invocation timing: before the mutation it guards, at most once per (reason, target, cause)
within one resolution call (keyed by object, so one stored edge loaded as two objects may be
asked twice), and only for pairs that would be written as of the ask (a concurrent resolution
or the final predicate re-check may still make the write moot). ``KEEP`` means "this cause
does not end the target": the pair is skipped. The result never has an ``expired_at`` or
invalidated edge that no-hook resolution would not write, nor a later ``invalid_at``; it can
have an earlier one, when ``KEEP`` vetoes one cause and another allowed cause truncates.

Per reason:

- ``OWN_END``: a stored duplicate carries an ``invalid_at`` and would be expired. Asked only
  when the edge-expiry hook did not already answer ``WORLD_END`` and the window is well
  formed (an inverted window is expired as upstream, unasked). On ``KEEP`` the edge stays
  live and its own ``invalid_at`` bounds it like a world-time end, so a later contradiction
  can only move ``invalid_at`` earlier.
- ``NEWER_CANDIDATE``: a newer candidate would set the target's ``invalid_at`` / expire it.
- ``CONTRADICTION``: the new mention would invalidate an existing candidate. The truncation
  date written is the resolved edge's ``valid_at``, which on a duplicate match is the stored
  duplicate's, not ``cause_edge.valid_at``.

Not covered: an extracted edge whose own ``invalid_at`` expires it on a non-duplicate
resolution (no other cause exists), including ``add_triplet`` reusing a caller-supplied
uuid.

Hook awaits may yield to a concurrent resolution of the same shared edge objects (bulk
ingest); with a hook installed, every write after an await re-checks the edge and keeps an
existing expiry and the earlier ``invalid_at``. With no hook, resolution is unchanged.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EpisodicNode


class EdgeInvalidationDecision(Enum):
    """Explicit decision a hook returns for one invalidation.

    - ``INVALIDATE``: upstream behavior; the target is invalidated or expired.
    - ``KEEP``: this cause does not end the target; the pair is skipped.
    """

    INVALIDATE = 'invalidate'
    KEEP = 'keep'


class EdgeInvalidationReason(Enum):
    """Why the target edge would be invalidated or expired."""

    OWN_END = 'own_end'
    NEWER_CANDIDATE = 'newer_candidate'
    CONTRADICTION = 'contradiction'


@dataclass(frozen=True)
class EdgeInvalidationContext:
    """Borrowed request-local evidence for one edge-invalidation decision.

    The dataclass is frozen, but the contained models are shared with the in-flight
    resolution call and are NOT copied. Hooks must treat them as read-only.

    - ``target_edge``: the edge that would get ``invalid_at`` / ``expired_at``.
    - ``cause_edge``: the edge whose information causes it. For ``OWN_END`` and
      ``CONTRADICTION`` it is this call's extracted edge (the new mention, from ``episode``),
      never the stored duplicate it matched; for ``NEWER_CANDIDATE`` it is the candidate.
    - ``reason``: which resolution step asks.
    - ``target_is_new``: ``target_edge`` is this call's extracted edge. ``False`` means the
      target is some other edge object; it does not prove the target is stored.
    - ``episode``: the episode being resolved.
    """

    target_edge: EntityEdge
    cause_edge: EntityEdge
    reason: EdgeInvalidationReason
    target_is_new: bool
    episode: EpisodicNode | None


@runtime_checkable
class EdgeInvalidationHook(Protocol):
    """Protocol for callers that can veto the invalidation of an edge.

    Called before any mutation. It must return an :class:`EdgeInvalidationDecision`; any
    other return value raises ``TypeError``. Exceptions raised by the hook propagate
    unchanged and abort the resolution call (as for the edge-expiry hook). ``KEEP`` skips
    this (target, cause) pair; see the module docstring for the per-reason effect.
    """

    async def decide_edge_invalidation(
        self, context: EdgeInvalidationContext
    ) -> EdgeInvalidationDecision: ...


def evaluate_edge_invalidation_decision(decision: object) -> EdgeInvalidationDecision:
    """Validate a hook return value and return it as an ``EdgeInvalidationDecision``.

    Raises ``TypeError`` for anything other than an ``EdgeInvalidationDecision``.
    """
    if not isinstance(decision, EdgeInvalidationDecision):
        raise TypeError(
            'edge_invalidation_hook must return an EdgeInvalidationDecision, '
            f'got {type(decision).__name__}'
        )
    return decision


def edge_invalidation_kwargs(hook: EdgeInvalidationHook | None) -> dict[str, Any]:
    """Keyword arguments that thread ``hook`` to a resolver call.

    Empty when there is no hook, so the no-hook call keeps the legacy signature.
    """
    return {} if hook is None else {'edge_invalidation_hook': hook}
