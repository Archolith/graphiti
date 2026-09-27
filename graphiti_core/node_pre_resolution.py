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

Neutral typed extension hook for node pre-resolution ahead of dedupe search.

This module defines the fork-native seam for callers who already know how a
newly extracted node resolves (for example, because it is bound to a declared
canonical identity) and want to bypass semantic candidate search, candidate
filtering, deterministic similarity, and the dedupe LLM for that node
entirely. Callers install a :class:`NodePreResolutionHook` on the ``Graphiti``
instance instead of rebinding ``node_operations`` symbols or wrapping search
utilities.

The hook is deliberately policy-free. It carries no canonical-self heuristic,
no UUID/namespace derivation, no receipt semantics, no logging text, and no
telemetry measurement. It only lets a caller inspect request-local evidence
before candidate search begins and return a strict ``PreResolutionResult``
(either an explicit deferral or a fully resolved
:class:`~graphiti_core.nodes.EntityNode`). All evidence travels
through ordinary function arguments for the duration of a single resolution
call — there is no module-global state, no ``ContextVar``, and no cache, so
there is no cross-request leakage or reset ordering to reason about.

Invocation timing: the hook runs exactly once per extracted node, before any
semantic candidate search is issued for that node. Returning a
``PreResolutionResult`` with ``PreResolutionDecision.DEFER`` leaves
Graphiti's ordinary resolution path untouched. A ``RESOLVE`` result carries
the resolved ``EntityNode``: it is excluded from candidate search, the
candidate filter, deterministic similarity, and the dedupe LLM, and it is
committed to the final resolved state, uuid map, and duplicate bookkeeping
exactly once (a duplicate pair is recorded when the resolved UUID differs
from the extracted UUID).

Edge evidence: the context's ``edges`` field is populated exclusively from
the separately named ``node_pre_resolution_edges`` argument supplied to
``resolve_extracted_nodes`` (never from ``identity_gate_edges``), and only
when a pre-resolution hook is configured.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodicNode

if TYPE_CHECKING:
    from graphiti_core.graphiti_types import GraphitiClients


class PreResolutionDecision(Enum):
    """Explicit decision a hook returns for one extracted node.

    - ``DEFER``: leave the node to Graphiti's ordinary resolution path
      (semantic candidate search, candidate filtering, deterministic
      handling, then the dedupe LLM); ``resolved_node`` must be ``None``.
    - ``RESOLVE``: the supplied ``resolved_node`` becomes the node's final
      resolution; ``resolved_node`` must be an ``EntityNode``.
    """

    DEFER = 'defer'
    RESOLVE = 'resolve'


@dataclass(frozen=True)
class PreResolutionResult:
    """Strict result object a pre-resolution hook must return.

    - ``decision=DEFER`` requires ``resolved_node is None``.
    - ``decision=RESOLVE`` requires ``resolved_node`` to be an
      :class:`~graphiti_core.nodes.EntityNode`.

    Any other decision, payload type, or combination is rejected with
    ``TypeError`` — a bare ``EntityNode`` is NOT an implicit ``RESOLVE``.
    """

    decision: PreResolutionDecision
    resolved_node: EntityNode | None = None


@dataclass(frozen=True)
class NodePreResolutionContext:
    """Borrowed request-local evidence for one pre-resolution decision.

    The dataclass is frozen (its fields cannot be reassigned), but the
    contained models, clients object, and lists are shared with the in-flight
    resolution call and are NOT copied — no runtime immutability is enforced.
    Hooks must treat these inputs as read-only and must not mutate them;
    Graphiti owns them for the duration of the call.

    Field availability:

    - ``extracted_node``: the newly extracted node about to be resolved.
    - ``clients``: the :class:`~graphiti_core.graphiti_types.GraphitiClients`
      bundle (driver, LLM client, embedder, cross-encoder, tracer) for the
      in-flight call, giving the hook the same access Graphiti itself uses.
    - ``episode``: the episode being ingested, when node resolution is
      episode-bound (``None`` for flows like ``add_triplet`` that resolve
      nodes without an episode).
    - ``previous_episodes``: prior episodes supplied to resolution for
      context; may be empty.
    - ``entity_types``: the custom entity-type map supplied to resolution;
      ``None`` when no custom types were supplied.
    - ``edges``: request-local edge evidence available at the resolution
      boundary (e.g. combined-extraction edges for this episode, or the
      episode's extracted edges in the bulk flow). May be empty when the
      caller legitimately has no edges yet at node-resolution time.
    """

    extracted_node: EntityNode
    clients: 'GraphitiClients'
    episode: EpisodicNode | None
    previous_episodes: list[EpisodicNode]
    entity_types: dict[str, type[BaseModel]] | None
    edges: list[EntityEdge]


@runtime_checkable
class NodePreResolutionHook(Protocol):
    """Protocol for callers that want to pre-resolve extracted nodes.

    The hook is invoked exactly once per extracted node before semantic
    candidate search. It must return a :class:`PreResolutionResult`;
    anything else (including a bare ``EntityNode``) raises ``TypeError``.
    Exceptions raised by the hook propagate unchanged and abort the
    resolution call; because all evidence lives in per-call arguments, there
    is nothing to reset.
    """

    async def pre_resolve_node(self, context: NodePreResolutionContext) -> PreResolutionResult: ...


def evaluate_pre_resolution_result(result: object) -> EntityNode | None:
    """Validate a hook return value under the strict pre-resolution contract.

    Returns ``None`` for a valid ``DEFER`` result and the supplied
    ``EntityNode`` for a valid ``RESOLVE`` result. Raises ``TypeError`` for
    anything that is not a ``PreResolutionResult``, for an invalid
    decision/payload combination (``DEFER`` with a node, ``RESOLVE`` without
    an ``EntityNode``), and for a bare ``EntityNode`` or any other type.
    """
    if not isinstance(result, PreResolutionResult):
        raise TypeError(
            f'node_pre_resolution_hook must return a PreResolutionResult, '
            f'got {type(result).__name__}'
        )
    if result.decision is PreResolutionDecision.DEFER:
        if result.resolved_node is not None:
            raise TypeError(
                'node_pre_resolution_hook DEFER result must have resolved_node=None, '
                f'got {type(result.resolved_node).__name__}'
            )
        return None
    if result.decision is PreResolutionDecision.RESOLVE:
        if not isinstance(result.resolved_node, EntityNode):
            raise TypeError(
                'node_pre_resolution_hook RESOLVE result must carry an EntityNode in '
                f'resolved_node, got {type(result.resolved_node).__name__}'
            )
        return result.resolved_node
    raise TypeError(
        f'node_pre_resolution_hook returned an unknown PreResolutionDecision: {result.decision!r}'
    )
