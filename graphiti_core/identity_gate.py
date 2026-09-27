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

Neutral typed extension hook for LLM-proposed node-merge identity gating.

This module defines the fork-native replacement for external monkey-patching of
``node_operations._resolve_with_llm``: callers who need to veto or allow
LLM-proposed duplicate merges install an :class:`IdentityGateHook` on the
``Graphiti`` instance instead of rebinding module symbols or wrapping
``llm_client.generate_response``.

The hook is deliberately policy-free. It carries no identity heuristic
(exact/substring/acronym/Jaccard or otherwise), no edge-fact mention policy, no
logging text, and no telemetry semantics. It only lets a caller observe the
request-local evidence available at the moment the dedupe LLM proposes a merge
and return an explicit allow/veto decision. All evidence travels through
ordinary function arguments for the duration of a single resolution call —
there is no module-global state, no ``ContextVar``, and no cache, so there is
no cross-request leakage or reset ordering to reason about.

Invocation timing: the hook runs exactly once per *valid* LLM-proposed merge,
after normalized ``NodeResolutions`` are available and before the candidate is
promoted or any resolved-state, uuid-map, or duplicate-pair mutation happens.
It is NOT invoked for negative (``duplicate_candidate_id < 0``) decisions,
invalid candidate ids, invalid or duplicate relative ids, deterministic
exact/similarity resolution, or paths with no LLM-proposed merge.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodicNode


class IdentityGateDecision(Enum):
    """Explicit decision a hook returns for one LLM-proposed merge.

    - ``ALLOW``: proceed with Graphiti's ordinary promotion of the candidate
      (the extracted node merges into the candidate).
    - ``VETO``: reject the merge; the extracted node keeps Graphiti's ordinary
      no-duplicate behavior (it is kept as a new node).
    """

    ALLOW = 'allow'
    VETO = 'veto'


@dataclass(frozen=True)
class IdentityGateContext:
    """Borrowed request-local evidence for one LLM-proposed merge.

    The dataclass is frozen (its fields cannot be reassigned), but the
    contained models and lists are shared with the in-flight resolution call
    and are NOT copied — no runtime immutability is enforced. Hooks must treat
    these inputs as read-only and must not mutate them; Graphiti owns them for
    the duration of the call.

    Field availability:

    - ``extracted_node``: the newly extracted node the LLM proposed to merge.
    - ``candidate_node``: the existing candidate node the LLM selected.
    - ``candidate_id``: the LLM's candidate id (index into the dedupe
      candidate list sent to the LLM for this call).
    - ``episode``: the episode being ingested, when node resolution is
      episode-bound (``None`` for flows like ``add_triplet`` that resolve
      nodes without an episode).
    - ``previous_episodes``: prior episodes supplied to resolution for
      context; may be empty.
    - ``edges``: request-local extracted/precomputed ``EntityEdge`` evidence
      available at the resolution boundary (e.g. combined-extraction edges for
      this episode, or the episode's extracted edges in the bulk flow). May be
      empty when the caller legitimately has no edges yet at node-resolution
      time (separate-route extraction, ``add_triplet``).
    """

    extracted_node: EntityNode
    candidate_node: EntityNode
    candidate_id: int
    episode: EpisodicNode | None
    previous_episodes: list[EpisodicNode]
    edges: list[EntityEdge]


@runtime_checkable
class IdentityGateHook(Protocol):
    """Protocol for callers that want to gate LLM-proposed node merges.

    The hook is invoked exactly once per valid LLM-proposed merge during node
    deduplication. It must return an :class:`IdentityGateDecision`; any other
    return value raises ``TypeError``. Exceptions raised by the hook propagate
    unchanged and abort the resolution call; because all evidence lives in
    per-call arguments, there is nothing to reset.
    """

    async def evaluate_identity_gate(
        self, context: IdentityGateContext
    ) -> IdentityGateDecision: ...


def evaluate_identity_decision(decision: object) -> IdentityGateDecision:
    """Validate a hook return value and return it as an ``IdentityGateDecision``.

    Raises ``TypeError`` for anything other than an ``IdentityGateDecision``.
    """
    if not isinstance(decision, IdentityGateDecision):
        raise TypeError(
            f'identity_gate_hook must return an IdentityGateDecision, got {type(decision).__name__}'
        )
    return decision
