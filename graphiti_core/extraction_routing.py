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

Neutral typed extension hook for single-episode extraction routing.

This module defines the fork-native replacement for external monkey-patching of
``extract_nodes`` / ``extract_edges``: callers who need to influence or replace
single-episode extraction install a :class:`SingleEpisodeExtractionHook` on the
``Graphiti`` instance instead of rebinding module symbols.

The hook is deliberately policy-free. It carries no receipt, marker, repair, or
telemetry semantics; it only lets a caller observe the extraction inputs and
either pick a built-in route or supply a complete extraction result. Everything
returned by the hook flows through ordinary function arguments for the duration
of a single ``add_episode`` call — there is no module-global state, no
``ContextVar``, and therefore no cross-request leakage or reset ordering to
reason about.

Note on the default route: the fork routes ordinary single-episode extraction
through the combined extractor by default. This is a deliberate divergence from
upstream v0.29.3 (which always uses the separate two-call path); it is the
fork-native behavior this module exists to support, not an upstream-compatible
default.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from graphiti_core.edges import EntityEdge
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.nodes import EntityNode, EpisodicNode


class ExtractionRoute(str, Enum):
    """Built-in extraction strategies for a single episode.

    - ``COMBINED``: one LLM call extracts entities and facts together via
      ``graphiti_core.utils.maintenance.combined_extraction.extract_nodes_and_edges``.
    - ``SEPARATE``: the legacy two-call path (``extract_nodes`` then
      ``extract_edges``).
    """

    COMBINED = 'combined'
    SEPARATE = 'separate'


@dataclass(frozen=True)
class SingleEpisodeExtractionContext:
    """Borrowed request-local extraction inputs.

    The dataclass is frozen (its fields cannot be reassigned), but the contained
    lists, dicts, and models are shared with the in-flight ``add_episode`` call
    and are NOT copied — no runtime immutability is enforced. Hooks must treat
    these inputs as read-only and must not mutate them; Graphiti owns them for
    the duration of the call.
    """

    clients: GraphitiClients
    episode: EpisodicNode
    previous_episodes: list[EpisodicNode]
    entity_types: dict[str, type[BaseModel]] | None
    excluded_entity_types: list[str] | None
    edge_type_map: dict[tuple[str, str], list[str]]
    edge_types: dict[str, type[BaseModel]] | None
    custom_extraction_instructions: str | None


@dataclass(frozen=True)
class SingleEpisodeExtractionResult:
    """A complete extraction produced by a hook.

    When a hook returns this object, Graphiti skips its own extraction LLM calls
    entirely and feeds these nodes and edges into the normal resolution,
    attribute-extraction, deduplication, and persistence pipeline. The episode
    index map must map node UUID (as they appear in ``nodes``) to 0-indexed
    positions in the episode list; for single-episode extraction this is
    typically ``{node_uuid: [0]}`` for every connected node.
    """

    nodes: list[EntityNode]
    edges: list[EntityEdge]
    node_episode_index_map: dict[str, list[int]]


@runtime_checkable
class SingleEpisodeExtractionHook(Protocol):
    """Protocol for callers that want to influence single-episode extraction.

    The hook is invoked once per ``add_episode`` call, before extraction.
    Return values are interpreted as follows:

    - ``None``: use Graphiti's default route selection.
    - :class:`ExtractionRoute`: force that built-in route; Graphiti performs
      the extraction itself.
    - :class:`SingleEpisodeExtractionResult`: the hook performed the extraction;
      Graphiti skips its own extraction and resolves/persists the supplied
      nodes and edges.

    Any other return value raises ``TypeError``. Exceptions raised by the hook
    propagate unchanged and abort the ``add_episode`` call; because routing
    state lives only in per-call arguments, there is nothing to reset.
    """

    async def extract_single_episode(
        self, context: SingleEpisodeExtractionContext
    ) -> ExtractionRoute | SingleEpisodeExtractionResult | None: ...


def default_extraction_route(
    edge_types: dict[str, type[BaseModel]] | None,
) -> ExtractionRoute:
    """Select the built-in route for a single episode.

    Combined extraction is used for ordinary schemas. Episodes with custom edge
    schemas (``edge_types`` non-empty) take the SEPARATE route as a deliberate
    compatibility boundary: this phase keeps custom-schema behavior identical to
    the pre-C1g upstream/Menhir-installer path (``extract_nodes`` ->
    ``extract_edges`` -> resolution). It is NOT because attribute handling is
    missing on the combined route — combined edges also flow through
    ``resolve_extracted_edges``, which performs the downstream typed-attribute
    work; the fallback simply avoids changing custom-schema behavior in this
    phase.
    """
    if edge_types:
        return ExtractionRoute.SEPARATE
    return ExtractionRoute.COMBINED
