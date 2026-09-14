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

Neutral typed extension hook for node-dedup candidate filtering.

This module defines the fork-native replacement for external monkey-patching of
``node_operations`` candidate collection: callers who need to remove candidates
from the dedupe candidate pool install a :class:`CandidateFilterHook` on the
``Graphiti`` instance instead of rebinding module symbols or wrapping
``_collect_candidate_nodes``.

The hook is deliberately policy-free. It carries no structural predicate, no
view/role labels, no source policy, no logging text, and no telemetry
semantics. It only lets a caller observe one merged candidate for one extracted
node and return an explicit include/exclude decision. All evidence travels
through ordinary function arguments for the duration of a single resolution
call — there is no module-global state, no ``ContextVar``, and no cache, so
there is no cross-request leakage or reset ordering to reason about.

Invocation timing: the hook runs after semantic search results and
``existing_nodes_override`` have been merged and deduplicated, exactly once per
unique candidate per extracted node, before deterministic exact/fuzzy
resolution or LLM candidate indexing. Candidate order is preserved; excluded
candidates are removed from the pool for that extracted node. If all
candidates for an extracted node are excluded, Graphiti's ordinary
no-candidate behavior applies (the extracted node is kept as a new node) and
neither the dedupe LLM nor any installed identity-gate hook is consulted for
it.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from graphiti_core.nodes import EntityNode


class CandidateFilterDecision(Enum):
    """Explicit decision a hook returns for one merged candidate.

    - ``INCLUDE``: keep the candidate in the dedupe candidate pool for the
      extracted node (Graphiti's ordinary resolution proceeds).
    - ``EXCLUDE``: remove the candidate from the dedupe candidate pool for the
      extracted node; it can never be resolved to (deterministically or via
      the LLM) for that node.
    """

    INCLUDE = 'include'
    EXCLUDE = 'exclude'


@dataclass(frozen=True)
class CandidateFilterContext:
    """Borrowed request-local evidence for one candidate-filter decision.

    The dataclass is frozen (its fields cannot be reassigned), but the
    contained models are shared with the in-flight resolution call and are NOT
    copied — no runtime immutability is enforced. Hooks must treat these
    inputs as read-only and must not mutate them; Graphiti owns them for the
    duration of the call.

    Field availability:

    - ``extracted_node``: the newly extracted node currently being resolved.
    - ``candidate_node``: the unique merged candidate node (already
      deduplicated across semantic search results and
      ``existing_nodes_override``) under evaluation.
    """

    extracted_node: EntityNode
    candidate_node: EntityNode


@runtime_checkable
class CandidateFilterHook(Protocol):
    """Protocol for callers that want to filter dedupe candidates.

    The hook is invoked exactly once per unique merged candidate per extracted
    node during node resolution, after search results and
    ``existing_nodes_override`` have been merged and deduplicated and before
    deterministic exact/fuzzy handling or LLM candidate indexing. It must
    return a :class:`CandidateFilterDecision`; any other return value raises
    ``TypeError``. Exceptions raised by the hook propagate unchanged and abort
    the resolution call; because all evidence lives in per-call arguments,
    there is nothing to reset.
    """

    async def filter_candidate(
        self, context: CandidateFilterContext
    ) -> CandidateFilterDecision: ...


def evaluate_candidate_filter_decision(decision: object) -> CandidateFilterDecision:
    """Validate a hook return value and return it as a ``CandidateFilterDecision``.

    Raises ``TypeError`` for anything other than a ``CandidateFilterDecision``.
    """
    if not isinstance(decision, CandidateFilterDecision):
        raise TypeError(
            f'candidate_filter_hook must return a CandidateFilterDecision, '
            f'got {type(decision).__name__}'
        )
    return decision
