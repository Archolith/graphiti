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
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from time import time
from typing import TYPE_CHECKING, Any

import httpx
import openai
from pydantic import BaseModel
from typing_extensions import LiteralString

from graphiti_core.driver.driver import GraphDriver, GraphProvider
from graphiti_core.edge_expiry import (
    EdgeExpiryContext,
    EdgeExpiryDecision,
    EdgeExpiryHook,
    edge_expiry_kwargs,
    evaluate_edge_expiry_decision,
)
from graphiti_core.edge_invalidation import (
    EdgeInvalidationContext,
    EdgeInvalidationDecision,
    EdgeInvalidationHook,
    EdgeInvalidationReason,
    edge_invalidation_kwargs,
    evaluate_edge_invalidation_decision,
)
from graphiti_core.edges import (
    CommunityEdge,
    EntityEdge,
    EpisodicEdge,
    create_entity_edge_embeddings,
)
from graphiti_core.graphiti_types import (
    GraphitiClients,
    generate_prompt_response,
    uses_prompt_routing,
)
from graphiti_core.helpers import semaphore_gather
from graphiti_core.llm_client import LLMClient
from graphiti_core.llm_client.client import RateLimitError
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodicNode
from graphiti_core.prompts import prompt_library
from graphiti_core.prompts.dedupe_edges import EdgeDuplicate, EdgeDuplicateBatch
from graphiti_core.prompts.extract_edges import Edge as ExtractedEdge
from graphiti_core.prompts.extract_edges import (
    EdgeTimestamps,
    ExtractedEdges,
)
from graphiti_core.prompts.prompt_helpers import to_prompt_json
from graphiti_core.search.search import search
from graphiti_core.search.search_config import SearchResults
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
from graphiti_core.search.search_filters import SearchFilters
from graphiti_core.utils.datetime_utils import ensure_utc, utc_now
from graphiti_core.utils.maintenance.attribute_utils import apply_capped_attributes
from graphiti_core.utils.maintenance.dangling_endpoints import (
    materialize_dangling_endpoint_node,
    prune_unreferenced_materialized_nodes,
)
from graphiti_core.utils.maintenance.dedup_helpers import _normalize_string_exact
from graphiti_core.utils.maintenance.temporal_edge_utils import (
    apply_extracted_timestamps,
    normalize_future_temporal_bounds,
)
from graphiti_core.utils.text_utils import concatenate_episodes

if TYPE_CHECKING:
    from graphiti_core.ingestion.change_set import ResolutionTrace

logger = logging.getLogger(__name__)

DEFAULT_EDGE_NAME = 'RELATES_TO'

# Bounded batching limits for multi-edge semantic deduplication (Menhir #91 Path B).
EDGE_DEDUP_BATCH_MAX_ITEMS = 12
EDGE_DEDUP_BATCH_MAX_CHARS = 24_000
_DEFAULT_RESOLVE_EDGE_PROMPT = prompt_library.dedupe_edges.resolve_edge


def build_episodic_edges(
    entity_nodes: list[EntityNode],
    episode_uuid: str | list[str],
    created_at: datetime,
    node_episode_index_map: dict[str, list[int]] | None = None,
) -> list[EpisodicEdge]:
    """Build episodic (MENTIONED_IN) edges between entity nodes and episodes.

    Parameters
    ----------
    entity_nodes : list[EntityNode]
        Nodes to connect to episodes.
    episode_uuid : str | list[str]
        A single episode UUID or a list of episode UUIDs.
    created_at : datetime
        Timestamp for the edges.
    node_episode_index_map : dict[str, list[int]] | None
        Optional mapping from node UUID to 0-indexed episode positions.
        When provided with a list of episode_uuids, each node is connected
        only to its attributed episodes. When None, every node is connected
        to all episodes.
    """
    episode_uuids = [episode_uuid] if isinstance(episode_uuid, str) else episode_uuid

    episodic_edges: list[EpisodicEdge] = []
    for node in entity_nodes:
        if node_episode_index_map and node.uuid in node_episode_index_map:
            indices = node_episode_index_map[node.uuid]
        else:
            indices = list(range(len(episode_uuids)))

        for idx in indices:
            if 0 <= idx < len(episode_uuids):
                episodic_edges.append(
                    EpisodicEdge(
                        source_node_uuid=episode_uuids[idx],
                        target_node_uuid=node.uuid,
                        created_at=created_at,
                        group_id=node.group_id,
                    )
                )

    logger.debug(f'Built {len(episodic_edges)} episodic edges')

    return episodic_edges


def build_community_edges(
    entity_nodes: list[EntityNode],
    community_node: CommunityNode,
    created_at: datetime,
) -> list[CommunityEdge]:
    edges: list[CommunityEdge] = [
        CommunityEdge(
            source_node_uuid=community_node.uuid,
            target_node_uuid=node.uuid,
            created_at=created_at,
            group_id=community_node.group_id,
        )
        for node in entity_nodes
    ]

    return edges


async def extract_edges(
    clients: GraphitiClients,
    episode: EpisodicNode | list[EpisodicNode],
    nodes: list[EntityNode],
    previous_episodes: list[EpisodicNode],
    edge_type_map: dict[tuple[str, str], list[str]],
    group_id: str = '',
    edge_types: dict[str, type[BaseModel]] | None = None,
    strict_edge_types: bool = False,
    custom_extraction_instructions: str | None = None,
) -> tuple[list[EntityEdge], list[EntityNode]]:
    """Extract edges from one or more episodes.

    Parameters
    ----------
    episode : EpisodicNode | list[EpisodicNode]
        A single episode or a list of episodes to extract edges from.
        When a list is provided, their contents are concatenated for extraction
        and edges are linked to all episode UUIDs.

    Returns
    -------
    tuple[list[EntityEdge], list[EntityNode]]
        Extracted edges and any endpoint nodes materialized onto ``nodes`` for
        dangling source/target names that survived edge filters.
    """
    episodes = episode if isinstance(episode, list) else [episode]
    primary_episode = episodes[0]

    start = time()
    initial_node_uuids = {node.uuid for node in nodes}

    extract_edges_max_tokens = 16384
    llm_client = clients.llm_client

    # Build mapping from edge type name to list of valid signatures
    edge_type_signatures_map: dict[str, list[tuple[str, str]]] = {}
    for signature, edge_type_names in edge_type_map.items():
        for edge_type in edge_type_names:
            if edge_type not in edge_type_signatures_map:
                edge_type_signatures_map[edge_type] = []
            edge_type_signatures_map[edge_type].append(signature)

    edge_types_context = (
        [
            {
                'fact_type_name': type_name,
                'fact_type_signatures': edge_type_signatures_map.get(
                    type_name, [('Entity', 'Entity')]
                ),
                'fact_type_description': type_model.__doc__,
            }
            for type_name, type_model in edge_types.items()
        ]
        if edge_types is not None
        else []
    )
    if strict_edge_types and not edge_types_context:
        logger.debug('No edge types available for strict edge extraction')
        return [], []

    allowed_edge_type_names = set(edge_types or {}) if strict_edge_types else None

    # Build normalized name-to-node mapping for validation / materialization
    name_to_node: dict[str, EntityNode] = {
        _normalize_string_exact(node.name): node for node in nodes
    }

    # Build episode attribution instructions for multi-episode extraction
    episode_attribution = ''
    if len(episodes) > 1:
        episode_attribution = (
            '\n8. **Episode Attribution**: The CURRENT_MESSAGE contains multiple episodes labeled '
            '[Episode 0], [Episode 1], etc. Each episode header includes a timestamp indicating '
            'when that episode occurred. Use the per-episode timestamp to resolve relative time '
            'mentions within each episode rather than relying solely on REFERENCE_TIME. '
            'For each extracted fact, set `episode_indices` '
            'to the 0-based list of episode numbers that the fact was derived from. '
            'A fact sourced from Episodes 0 and 1 should have `episode_indices: [0, 1]`.'
        )

    # Prepare context for LLM
    # Use the latest episode's timestamp as the primary reference time
    latest_episode = max(episodes, key=lambda ep: ep.valid_at)
    context = {
        'episode_content': concatenate_episodes(episodes),
        'nodes': [{'name': node.name, 'entity_types': node.labels} for node in nodes],
        'previous_episodes': [
            {
                'content': ep.content,
                'timestamp': ep.valid_at.isoformat() if ep.valid_at else None,
            }
            for ep in previous_episodes
        ],
        'reference_time': latest_episode.valid_at,
        'edge_types': edge_types_context,
        'strict_edge_types': strict_edge_types,
        'custom_extraction_instructions': (custom_extraction_instructions or '')
        + episode_attribution,
    }

    llm_response = await generate_prompt_response(
        llm_client,
        'extract_edges.edge',
        prompt_library.extract_edges.edge,
        context,
        clients=clients,
        response_model=ExtractedEdges,
        max_tokens=extract_edges_max_tokens,
        group_id=group_id or primary_episode.group_id,
    )
    all_edges_data = ExtractedEdges(**llm_response).edges

    edge_group_id = group_id or primary_episode.group_id

    # Validate entity names; materialize dangling endpoints only for edges we keep
    edges_data: list[ExtractedEdge] = []
    for edge_data in all_edges_data:
        source_name = edge_data.source_entity_name.strip()
        target_name = edge_data.target_entity_name.strip()
        if not source_name or not target_name:
            logger.warning('Skipping edge with empty source or target entity name')
            continue

        if not edge_data.fact.strip():
            continue

        if (
            allowed_edge_type_names is not None
            and edge_data.relation_type not in allowed_edge_type_names
        ):
            logger.debug(
                'Skipping edge with relation type "%s" not in strict edge ontology',
                edge_data.relation_type,
            )
            continue

        source_node = name_to_node.get(_normalize_string_exact(source_name))
        if source_node is None:
            source_node = materialize_dangling_endpoint_node(
                source_name,
                group_id=edge_group_id,
                name_to_node=name_to_node,
                nodes=nodes,
            )

        target_node = name_to_node.get(_normalize_string_exact(target_name))
        if target_node is None:
            target_node = materialize_dangling_endpoint_node(
                target_name,
                group_id=edge_group_id,
                name_to_node=name_to_node,
                nodes=nodes,
            )

        # Keep canonical names on the edge payload for the conversion loop below
        edge_data.source_entity_name = source_node.name
        edge_data.target_entity_name = target_node.name
        edges_data.append(edge_data)

    end = time()
    logger.debug(f'Extracted {len(edges_data)} new edges in {(end - start) * 1000:.0f} ms')

    if len(edges_data) == 0:
        materialized_nodes = prune_unreferenced_materialized_nodes(
            nodes,
            initial_uuids=initial_node_uuids,
            referenced_uuids=set(),
        )
        return [], materialized_nodes

    # Convert the extracted data into EntityEdge objects
    edges = []
    for edge_data in edges_data:
        # Validate Edge Date information
        valid_at = edge_data.valid_at
        invalid_at = edge_data.invalid_at
        valid_at_datetime = None
        invalid_at_datetime = None

        # Names already validated / rewritten to canonical node names above
        source_node = name_to_node.get(_normalize_string_exact(edge_data.source_entity_name))
        target_node = name_to_node.get(_normalize_string_exact(edge_data.target_entity_name))

        if source_node is None or target_node is None:
            logger.warning('Could not find source or target node for extracted edge')
            continue

        source_node_uuid = source_node.uuid
        target_node_uuid = target_node.uuid

        if valid_at:
            try:
                valid_at_datetime = ensure_utc(
                    datetime.fromisoformat(valid_at.replace('Z', '+00:00'))
                )
            except ValueError:
                logger.warning('Error parsing valid_at date, skipping')

        if invalid_at:
            try:
                invalid_at_datetime = ensure_utc(
                    datetime.fromisoformat(invalid_at.replace('Z', '+00:00'))
                )
            except ValueError as e:
                logger.warning(f'WARNING: Error parsing invalid_at date: {e}. Input: {invalid_at}')

        # Map episode_indices (0-indexed) to episode UUIDs.
        # Clamp indices to valid range and fall back to all episodes if empty.
        edge_episode_uuids = []
        for idx in edge_data.episode_indices:
            if 0 <= idx < len(episodes):
                edge_episode_uuids.append(episodes[idx].uuid)
        if not edge_episode_uuids:
            edge_episode_uuids = [ep.uuid for ep in episodes]

        edge_reference_time = (
            episodes[edge_data.episode_indices[0]].valid_at
            if edge_data.episode_indices and 0 <= edge_data.episode_indices[0] < len(episodes)
            else primary_episode.valid_at
        )
        valid_at_datetime, invalid_at_datetime = normalize_future_temporal_bounds(
            edge_data.fact,
            edge_reference_time,
            valid_at_datetime,
            invalid_at_datetime,
        )

        edge = EntityEdge(
            source_node_uuid=source_node_uuid,
            target_node_uuid=target_node_uuid,
            name=edge_data.relation_type,
            group_id=group_id or primary_episode.group_id,
            fact=edge_data.fact,
            episodes=edge_episode_uuids,
            created_at=utc_now(),
            valid_at=valid_at_datetime,
            invalid_at=invalid_at_datetime,
            reference_time=edge_reference_time,
        )
        edges.append(edge)
        logger.debug(
            f'Created new edge {edge.uuid} from {edge.source_node_uuid} to {edge.target_node_uuid}'
        )

    logger.debug(f'Extracted edges: {[e.uuid for e in edges]}')

    referenced_uuids = {edge.source_node_uuid for edge in edges} | {
        edge.target_node_uuid for edge in edges
    }
    materialized_nodes = prune_unreferenced_materialized_nodes(
        nodes,
        initial_uuids=initial_node_uuids,
        referenced_uuids=referenced_uuids,
    )
    if materialized_nodes:
        logger.warning(
            'Materialized %d dangling edge endpoint node(s) for group_id=%s',
            len(materialized_nodes),
            edge_group_id,
        )

    return edges, materialized_nodes


def _pair_search_filter(edge: EntityEdge) -> SearchFilters:
    """Filter restricting an edge search to the extracted edge's endpoints.

    src IN {a, b} AND tgt IN {a, b} covers both orientations in one query.
    The filter can reduce the candidate set before ranking. It also
    admits self-loops on either endpoint when a != b -- callers drop those
    with _same_pair_edges.
    """
    endpoints = [edge.source_node_uuid, edge.target_node_uuid]
    return SearchFilters(
        edge_source_node_uuids=endpoints,
        edge_target_node_uuids=endpoints,
    )


def _same_pair_edges(extracted_edge: EntityEdge, candidates: list[EntityEdge]) -> list[EntityEdge]:
    """Keep only edges between the extracted edge's endpoints, either
    orientation (drops the self-loops a {a,b}x{a,b} endpoint filter admits)."""
    pair = {
        (extracted_edge.source_node_uuid, extracted_edge.target_node_uuid),
        (extracted_edge.target_node_uuid, extracted_edge.source_node_uuid),
    }
    return [edge for edge in candidates if (edge.source_node_uuid, edge.target_node_uuid) in pair]


def preserved_attributes_kwargs(preserved: frozenset[str]) -> dict[str, Any]:
    """Return resolve kwargs for ``preserved``; empty when unset to keep the legacy call shape."""
    return {'preserved_attributes': preserved} if preserved else {}


def _precomputed_decision_kwargs(decision: EdgeDuplicate | None) -> dict[str, Any]:
    """Return resolve kwargs for a precomputed batch decision; empty when unset for legacy mocks."""
    return {'precomputed_decision': decision} if decision is not None else {}


def _find_verbatim_related_edge(
    extracted_edge: EntityEdge, related_edges: list[EntityEdge]
) -> EntityEdge | None:
    """Return the first same-direction exact normalized-fact match in ``related_edges``."""
    normalized_fact = _normalize_string_exact(extracted_edge.fact)
    for edge in related_edges:
        if (
            edge.source_node_uuid == extracted_edge.source_node_uuid
            and edge.target_node_uuid == extracted_edge.target_node_uuid
            and _normalize_string_exact(edge.fact) == normalized_fact
        ):
            return edge
    return None


def _candidate_cannot_interact_temporally(
    candidate: EntityEdge,
    possible_resolved_edges: list[EntityEdge],
) -> bool:
    """True when ``candidate`` provably ended before every possible resolved edge started.

    Deterministic temporal prefilter (Menhir #91 item 1.6): if ``candidate.invalid_at <= r.valid_at``
    (and ``candidate.valid_at`` is absent or ``<= r.valid_at``) for every ``r`` in
    ``[extracted_edge, *related_edges]``, then ``_contradiction_invalidates(candidate, r)`` is
    guaranteed False and ``candidate`` cannot expire ``r`` via the newer-candidate check either.
    Never applied to duplicate candidates (``related_edges``).
    """
    candidate_invalid_at = ensure_utc(candidate.invalid_at)
    if candidate_invalid_at is None or not possible_resolved_edges:
        return False
    candidate_valid_at = ensure_utc(candidate.valid_at)
    for resolved_candidate in possible_resolved_edges:
        resolved_valid_at = ensure_utc(resolved_candidate.valid_at)
        if resolved_valid_at is None:
            return False
        if candidate_invalid_at > resolved_valid_at:
            return False
        if candidate_valid_at is not None and candidate_valid_at > resolved_valid_at:
            return False
    return True


def _prefilter_invalidation_candidates(
    extracted_edge: EntityEdge,
    related_edges: list[EntityEdge],
    existing_edges: list[EntityEdge],
) -> list[EntityEdge]:
    """Drop invalidation candidates whose validity window provably ended before the new fact."""
    if not existing_edges:
        return existing_edges
    possible_resolved = [extracted_edge, *related_edges]
    if any(ensure_utc(edge.valid_at) is None for edge in possible_resolved):
        return existing_edges
    return [
        candidate
        for candidate in existing_edges
        if not _candidate_cannot_interact_temporally(candidate, possible_resolved)
    ]


def _can_batch_edge_dedupe(clients: GraphitiClients | None) -> bool:
    """True when batched edge deduplication will not bypass a custom single-edge route or override."""
    if uses_prompt_routing(clients):
        return False
    if prompt_library.dedupe_edges.resolve_edge is not _DEFAULT_RESOLVE_EDGE_PROMPT:
        return False
    return (
        _dedupe_extracted_edge is _ORIGINAL_DEDUPE_EXTRACTED_EDGE
        and decide_edge_against_candidates is _ORIGINAL_DECIDE_EDGE_AGAINST_CANDIDATES
    )


def _build_edge_dedupe_request_payload(
    request_id: int,
    extracted_edge: EntityEdge,
    related_edges: list[EntityEdge],
    existing_edges: list[EntityEdge],
) -> dict[str, Any]:
    invalidation_idx_offset = len(related_edges)
    return {
        'request_id': request_id,
        'existing_edges': [{'idx': i, 'fact': edge.fact} for i, edge in enumerate(related_edges)],
        'edge_invalidation_candidates': [
            {'idx': invalidation_idx_offset + i, 'fact': existing_edge.fact}
            for i, existing_edge in enumerate(existing_edges)
        ],
        'new_edge': extracted_edge.fact,
    }


def _chunk_edge_dedupe_indices(
    candidate_indices: list[int],
    extracted_edges: list[EntityEdge],
    related_edges_lists: list[list[EntityEdge]],
    edge_invalidation_candidates: list[list[EntityEdge]],
    *,
    max_items: int,
    max_chars: int,
) -> tuple[list[list[int]], list[int]]:
    """Pack semantic edge indices into bounded chunks of >=2 items; return (chunks, singles)."""
    chunks: list[list[int]] = []
    singles: list[int] = []
    current: list[int] = []
    current_chars = 0

    for edge_idx in candidate_indices:
        payload = _build_edge_dedupe_request_payload(
            len(current),
            extracted_edges[edge_idx],
            related_edges_lists[edge_idx],
            edge_invalidation_candidates[edge_idx],
        )
        item_chars = len(to_prompt_json(payload))
        if item_chars > max_chars:
            singles.append(edge_idx)
            continue
        if current and (len(current) >= max_items or current_chars + item_chars > max_chars):
            if len(current) >= 2:
                chunks.append(current)
            else:
                singles.extend(current)
            current = [edge_idx]
            current_chars = item_chars
        else:
            current.append(edge_idx)
            current_chars += item_chars

    if current:
        if len(current) >= 2:
            chunks.append(current)
        else:
            singles.extend(current)

    return chunks, singles


async def _resolve_edge_dedupe_batch_chunk(
    llm_client: LLMClient,
    chunk_indices: list[int],
    extracted_edges: list[EntityEdge],
    related_edges_lists: list[list[EntityEdge]],
    edge_invalidation_candidates: list[list[EntityEdge]],
    *,
    clients: GraphitiClients | None = None,
) -> dict[int, EdgeDuplicate] | None:
    """Resolve one bounded chunk of edge dedupe requests in a single LLM call.

    Returns ``{edge_index: EdgeDuplicate}`` when every request_id 0..len-1 is accounted for
    exactly once; returns ``None`` on malformed/incomplete output so the caller can fall back
    to per-edge resolution for only this chunk.
    """
    requests = [
        _build_edge_dedupe_request_payload(
            req_id,
            extracted_edges[edge_idx],
            related_edges_lists[edge_idx],
            edge_invalidation_candidates[edge_idx],
        )
        for req_id, edge_idx in enumerate(chunk_indices)
    ]
    try:
        llm_response = await generate_prompt_response(
            llm_client,
            'dedupe_edges.resolve_edges_batch',
            prompt_library.dedupe_edges.resolve_edges_batch,
            {'edge_requests': requests},
            clients=clients,
            response_model=EdgeDuplicateBatch,
            model_size=ModelSize.small,
        )
    except (RateLimitError, openai.APIError, httpx.HTTPError):
        # Provider, quota and transport failures would fail every per-edge call the same way;
        # falling back would multiply the calls against a provider that is already refusing.
        raise
    except Exception:
        logger.warning(
            'Batched edge deduplication failed for chunk of %d edges; falling back to per-edge resolution',
            len(chunk_indices),
            exc_info=True,
        )
        return None

    if not isinstance(llm_response, dict) or not isinstance(
        llm_response.get('edge_resolutions'), list
    ):
        logger.warning(
            'Batched edge deduplication returned unexpected payload without edge_resolutions; '
            'falling back to per-edge resolution'
        )
        return None

    try:
        batch = EdgeDuplicateBatch(**llm_response)
    except Exception:
        logger.warning(
            'Batched edge deduplication response failed validation; falling back to per-edge resolution',
            exc_info=True,
        )
        return None

    expected_ids = set(range(len(chunk_indices)))
    by_req_id: dict[int, EdgeDuplicate] = {}
    for item in batch.edge_resolutions:
        if item.request_id not in expected_ids or item.request_id in by_req_id:
            logger.warning(
                'Batched edge deduplication returned invalid or duplicate request_id=%s '
                '(expected 0..%d); falling back to per-edge resolution',
                item.request_id,
                len(chunk_indices) - 1,
            )
            return None
        edge_idx = chunk_indices[item.request_id]
        duplicate_count = len(related_edges_lists[edge_idx])
        candidate_count = duplicate_count + len(edge_invalidation_candidates[edge_idx])
        # Every request numbers its facts from 0, so an out-of-range idx is the only visible sign
        # of an answer attached to the wrong request. Reject the chunk instead of dropping the idx.
        if any(not 0 <= idx < duplicate_count for idx in item.duplicate_facts) or any(
            not 0 <= idx < candidate_count for idx in item.contradicted_facts
        ):
            logger.warning(
                'Batched edge deduplication returned an out-of-range idx for request_id=%s; '
                'falling back to per-edge resolution',
                item.request_id,
            )
            return None
        by_req_id[item.request_id] = EdgeDuplicate(
            duplicate_facts=item.duplicate_facts,
            contradicted_facts=item.contradicted_facts,
        )

    if len(by_req_id) != len(chunk_indices):
        logger.warning(
            'Batched edge deduplication returned %d resolutions for %d requests; '
            'falling back to per-edge resolution',
            len(by_req_id),
            len(chunk_indices),
        )
        return None

    return {edge_idx: by_req_id[req_id] for req_id, edge_idx in enumerate(chunk_indices)}


async def resolve_extracted_edges(
    clients: GraphitiClients,
    extracted_edges: list[EntityEdge],
    episode: EpisodicNode,
    entities: list[EntityNode],
    edge_types: dict[str, type[BaseModel]],
    edge_type_map: dict[tuple[str, str], list[str]],
    existing_edges_override: list[EntityEdge] | None = None,
    strict_edge_types: bool = False,
    invalidated_by: dict[str, list[str]] | None = None,
    edge_expiry_hook: EdgeExpiryHook | None = None,
    edge_invalidation_hook: EdgeInvalidationHook | None = None,
    preserved_attributes: frozenset[str] = frozenset(),
    trace: 'ResolutionTrace | None' = None,
    batch_edge_dedupe: bool = True,
) -> tuple[list[EntityEdge], list[EntityEdge], list[EntityEdge]]:
    """Resolve extracted edges against existing graph context.

    An edge whose name is a custom edge type must join the entity types that the
    type's signature declares in ``edge_type_map``. A mismatched edge is dropped
    when ``strict_edge_types`` is set and renamed to ``DEFAULT_EDGE_NAME`` otherwise.

    When `invalidated_by` is given, it is filled with the uuid of every
    invalidated edge mapped to the uuids of the resolved edges that invalidated it.

    ``preserved_attributes`` names caller-owned edge properties that keep their stored
    values on a resolved duplicate; it is passed to every ``_dedupe_extracted_edge`` call.
    ``edge_expiry_hook`` (see :mod:`graphiti_core.edge_expiry`) and
    ``edge_invalidation_hook`` (see :mod:`graphiti_core.edge_invalidation`) are passed to
    every ``_apply_temporal_invalidation`` call. Copies of one stored edge are reconciled
    after temporal invalidation (:func:`reconcile_edge_copies`).

    When `trace` is given, it records the candidates that each extracted edge saw and
    one operation for each change that the resolution makes to a stored edge.

    Returns
    -------
    tuple[list[EntityEdge], list[EntityEdge], list[EntityEdge]]
        A tuple of (resolved_edges, invalidated_edges, new_edges) where:
        - resolved_edges: All edges after resolution (may include existing edges if duplicates found)
        - invalidated_edges: Edges that were invalidated/contradicted by new information
        - new_edges: Only edges that are new to the graph (not duplicates of existing edges)
    """
    driver = clients.driver
    llm_client = clients.llm_client
    embedder = clients.embedder

    # Build entity hash table
    uuid_entity_map: dict[str, EntityNode] = {entity.uuid: entity for entity in entities}

    # Collect all node UUIDs referenced by edges that are not in the entities list
    referenced_node_uuids = set()
    for extracted_edge in extracted_edges:
        if extracted_edge.source_node_uuid not in uuid_entity_map:
            referenced_node_uuids.add(extracted_edge.source_node_uuid)
        if extracted_edge.target_node_uuid not in uuid_entity_map:
            referenced_node_uuids.add(extracted_edge.target_node_uuid)

    # Fetch missing nodes from the database
    if referenced_node_uuids:
        # Limit the lookup to the edge group.
        edge_group_id = extracted_edges[0].group_id
        missing_nodes = await EntityNode.get_by_uuids(
            driver, list(referenced_node_uuids), group_id=edge_group_id
        )
        for node in missing_nodes:
            uuid_entity_map[node.uuid] = node

    # Determine which edge types are relevant for each edge based on node signatures.
    # `edge_types_lst` stores the subset of custom edge definitions whose
    # node signature matches each extracted edge.
    edge_types_lst: list[dict[str, type[BaseModel]]] = []
    for extracted_edge in extracted_edges:
        edge_types_lst.append(
            _edge_types_for_endpoints(extracted_edge, uuid_entity_map, edge_types, edge_type_map)
        )

    extracted_edges, edge_types_lst = enforce_edge_type_signatures(
        extracted_edges, edge_types_lst, edge_type_map, strict_edge_types
    )

    # Fast path: deduplicate same-direction and same-type reverse-direction matches
    seen: dict[tuple[str, str, str], EntityEdge] = {}
    deduplicated_edges: list[EntityEdge] = []
    deduplicated_edge_types: list[dict[str, type[BaseModel]]] = []

    for edge, matching_edge_types in zip(extracted_edges, edge_types_lst, strict=True):
        if find_exact_duplicate_edge(edge, seen) is not None:
            continue

        deduplicated_edges.append(edge)
        deduplicated_edge_types.append(matching_edge_types)

    extracted_edges = deduplicated_edges
    edge_types_lst = deduplicated_edge_types

    # Fast path (item 1.7): if an override edge already matches (src, tgt, normalized_fact, group),
    # skip both hybrid search fan-outs for that edge.
    override_by_pair: dict[tuple[str, str], list[EntityEdge]] = {}
    if existing_edges_override:
        for edge in existing_edges_override:
            pair = (edge.source_node_uuid, edge.target_node_uuid)
            override_by_pair.setdefault(pair, []).append(edge)

    override_exact_matches: dict[int, EntityEdge] = {}
    if override_by_pair:
        for i, extracted_edge in enumerate(extracted_edges):
            pair = (extracted_edge.source_node_uuid, extracted_edge.target_node_uuid)
            same_group_overrides = [
                edge
                for edge in override_by_pair.get(pair, [])
                if edge.group_id == extracted_edge.group_id
            ]
            exact_hit = _find_verbatim_related_edge(extracted_edge, same_group_overrides)
            if exact_hit is not None:
                override_exact_matches[i] = exact_hit

    edges_needing_pair_search = [
        i for i in range(len(extracted_edges)) if i not in override_exact_matches
    ]
    if edges_needing_pair_search:
        await create_entity_edge_embeddings(
            embedder, [extracted_edges[i] for i in edges_needing_pair_search]
        )

    # Duplicate candidates have the same endpoints in either direction.
    # Prefiltering the search by endpoints limits the search work. The post-filter
    # removes self-loops and protects against indexes that return unrelated edges.
    # Reuse each computed fact embedding to avoid another embedder request.
    pair_search_results: list[SearchResults] = (
        list(
            await semaphore_gather(
                *[
                    search(
                        clients,
                        extracted_edges[i].fact,
                        group_ids=[extracted_edges[i].group_id],
                        config=EDGE_HYBRID_SEARCH_RRF,
                        search_filter=_pair_search_filter(extracted_edges[i]),
                        query_vector=extracted_edges[i].fact_embedding,
                    )
                    for i in edges_needing_pair_search
                ],
                max_coroutines=getattr(clients, 'max_coroutines', None),
            )
        )
        if edges_needing_pair_search
        else []
    )
    pair_results_by_idx = dict(zip(edges_needing_pair_search, pair_search_results, strict=True))

    related_edges_lists: list[list[EntityEdge]] = []
    for i, extracted_edge in enumerate(extracted_edges):
        if i in override_exact_matches:
            related_edges_lists.append([override_exact_matches[i]])
        else:
            related_edges_lists.append(
                [
                    candidate
                    for candidate in _same_pair_edges(
                        extracted_edge, pair_results_by_idx[i].edges
                    )
                ]
            )

    if override_by_pair:
        for i, (extracted_edge, related_edges) in enumerate(
            zip(extracted_edges, related_edges_lists, strict=True)
        ):
            if i in override_exact_matches:
                continue
            pair = (extracted_edge.source_node_uuid, extracted_edge.target_node_uuid)
            existing_uuids = {edge.uuid for edge in related_edges}
            for edge in override_by_pair.get(pair, []):
                if edge.group_id == extracted_edge.group_id and edge.uuid not in existing_uuids:
                    related_edges.append(edge)
                    existing_uuids.add(edge.uuid)

    # Item 1.7: edges that already match a stored edge verbatim in related_edges take the
    # exact-fact fast path in _dedupe_extracted_edge and never consume invalidation candidates,
    # so skip the unfiltered global hybrid search fan-out for them.
    verbatim_match_indices = {
        i
        for i, (extracted_edge, related_edges) in enumerate(
            zip(extracted_edges, related_edges_lists, strict=True)
        )
        if _find_verbatim_related_edge(extracted_edge, related_edges) is not None
    }
    edges_needing_invalidation_search = [
        i for i in range(len(extracted_edges)) if i not in verbatim_match_indices
    ]

    invalidation_search_results: list[SearchResults] = (
        list(
            await semaphore_gather(
                *[
                    search(
                        clients,
                        extracted_edges[i].fact,
                        group_ids=[extracted_edges[i].group_id],
                        config=EDGE_HYBRID_SEARCH_RRF,
                        search_filter=SearchFilters(),
                        query_vector=extracted_edges[i].fact_embedding,
                    )
                    for i in edges_needing_invalidation_search
                ],
                max_coroutines=getattr(clients, 'max_coroutines', None),
            )
        )
        if edges_needing_invalidation_search
        else []
    )
    invalidation_results_by_idx = dict(
        zip(edges_needing_invalidation_search, invalidation_search_results, strict=True)
    )

    resolver_kwargs = {'clients': clients} if uses_prompt_routing(clients) else {}
    edge_invalidation_candidates: list[list[EntityEdge]] = []
    # Before the 1.6 prefilter. The trace records these: a dropped candidate was considered, so
    # commit repair must not decide it again.
    searched_invalidation_candidates: list[list[EntityEdge]] = []
    prefilter_emptied: set[int] = set()
    for i, (extracted_edge, related_edges) in enumerate(
        zip(extracted_edges, related_edges_lists, strict=True)
    ):
        invalidation_result = invalidation_results_by_idx.get(i)
        if invalidation_result is None:
            edge_invalidation_candidates.append([])
            searched_invalidation_candidates.append([])
            continue
        related_uuids = {edge.uuid for edge in related_edges}
        deduplicated = [
            edge for edge in invalidation_result.edges if edge.uuid not in related_uuids
        ]
        # Item 1.6: drop invalidation candidates provably ended before the new edge starts.
        filtered = _prefilter_invalidation_candidates(
            extracted_edge, related_edges, deduplicated
        )
        if deduplicated and not filtered and not related_edges:
            prefilter_emptied.add(i)
        edge_invalidation_candidates.append(filtered)
        searched_invalidation_candidates.append(deduplicated)

    logger.debug(
        f'Related edges: {[e.uuid for edges_lst in related_edges_lists for e in edges_lst]}'
    )

    # A resolved-attributes mark must come from this resolution, not from an earlier one that
    # handed back the same object. This covers reuse across successive calls only: concurrent
    # calls (add_episode_bulk episodes) that share one candidate object can clear each other's
    # marks, so the bulk path assumes searches return fresh objects, as the drivers here do.
    # Resetting the extracted edges is defensive; they are new objects in every caller here.
    for edge in extracted_edges:
        edge._attributes_resolved = False
    for candidates in (*related_edges_lists, *edge_invalidation_candidates):
        for edge in candidates:
            edge._attributes_resolved = False

    watched_edges: list[EntityEdge] = []
    if trace is not None:
        extracted_uuids = {edge.uuid for edge in extracted_edges}
        for extracted_edge, related_edges, existing_edges in zip(
            extracted_edges, related_edges_lists, searched_invalidation_candidates, strict=True
        ):
            trace.record_edge_candidates(extracted_edge.uuid, related_edges, existing_edges)
            # An edge is a stored record when the call did not extract it.
            watched_edges.extend(
                edge
                for edge in [*related_edges, *existing_edges]
                if edge.uuid not in extracted_uuids
            )
        trace.watch_stored_edges(watched_edges)

    # Phase 3 (Menhir #91 Path B): batch semantic edge dedupe judgments into bounded multi-edge
    # calls, then feed validated decisions through the existing deterministic per-edge application.
    precomputed_decisions: dict[int, EdgeDuplicate] = {}
    if batch_edge_dedupe and _can_batch_edge_dedupe(clients):
        semantic_indices = [
            i
            for i, (related_edges, existing_edges) in enumerate(
                zip(related_edges_lists, edge_invalidation_candidates, strict=True)
            )
            if i not in verbatim_match_indices and (related_edges or existing_edges)
        ]
        if len(semantic_indices) >= 2:
            # A chunk over the request guard's ceiling raises GraphitiRequestTooLargeError before
            # sending and falls back per-edge.
            char_limit = EDGE_DEDUP_BATCH_MAX_CHARS
            chunks, _singles = _chunk_edge_dedupe_indices(
                semantic_indices,
                extracted_edges,
                related_edges_lists,
                edge_invalidation_candidates,
                max_items=EDGE_DEDUP_BATCH_MAX_ITEMS,
                max_chars=char_limit,
            )
            if chunks:
                chunk_results = await semaphore_gather(
                    *[
                        _resolve_edge_dedupe_batch_chunk(
                            llm_client,
                            chunk,
                            extracted_edges,
                            related_edges_lists,
                            edge_invalidation_candidates,
                            **resolver_kwargs,
                        )
                        for chunk in chunks
                    ],
                    max_coroutines=getattr(clients, 'max_coroutines', None),
                )
                for result_map in chunk_results:
                    if result_map is not None:
                        precomputed_decisions.update(result_map)

    # The prefilter emptied these edges' candidates and no candidate can be a duplicate or be
    # contradicted, so "no duplicate, no contradiction" is the decision. It keeps the edge on the
    # decision path so own-end expiry and edge_expiry_hook still run (see _dedupe_extracted_edge).
    for i in prefilter_emptied:
        precomputed_decisions.setdefault(i, EdgeDuplicate(duplicate_facts=[], contradicted_facts=[]))

    dedupe_results: list[tuple[EntityEdge, list[EntityEdge], list[EntityEdge] | None]] = list(
        await semaphore_gather(
            *[
                _dedupe_extracted_edge(
                    llm_client,
                    extracted_edge,
                    related_edges,
                    existing_edges,
                    episode,
                    extracted_edge_types,
                    **resolver_kwargs,
                    **preserved_attributes_kwargs(preserved_attributes),
                    **_precomputed_decision_kwargs(precomputed_decisions.get(i)),
                )
                for i, (extracted_edge, related_edges, existing_edges, extracted_edge_types) in enumerate(
                    zip(
                        extracted_edges,
                        related_edges_lists,
                        edge_invalidation_candidates,
                        edge_types_lst,
                        strict=True,
                    )
                )
            ],
            max_coroutines=getattr(clients, 'max_coroutines', None),
        )
    )

    resolved_edges: list[EntityEdge] = []
    invalidated_edges: list[EntityEdge] = []
    new_edges: list[EntityEdge] = []
    for extracted_edge, (resolved_edge, _duplicates, _candidates) in zip(
        extracted_edges, dedupe_results, strict=True
    ):
        resolved_edges.append(resolved_edge)

        # Track edges that are new (not duplicates of existing edges)
        # An edge is new if the resolved edge UUID matches the extracted edge UUID
        if resolved_edge.uuid == extracted_edge.uuid:
            new_edges.append(resolved_edge)

    logger.debug(f'Resolved edges: {[e.uuid for e in resolved_edges]}')
    logger.debug(f'New edges (non-duplicates): {[e.uuid for e in new_edges]}')

    for extracted_edge, (resolved_edge, _duplicates, candidates) in zip(
        extracted_edges, dedupe_results, strict=True
    ):
        if candidates is None:
            continue
        had_end_date = resolved_edge.invalid_at is not None
        invalidated = await _apply_temporal_invalidation(
            resolved_edge,
            candidates,
            extracted_edge,
            episode,
            **edge_expiry_kwargs(edge_expiry_hook),
            **edge_invalidation_kwargs(edge_invalidation_hook),
        )
        invalidated_edges.extend(invalidated)
        if trace is not None:
            trace.mark_ended_as_candidate(invalidated)
            if not had_end_date and resolved_edge.invalid_at is not None:
                trace.ended_by_candidate.add(resolved_edge.uuid)
        if invalidated_by is not None:
            for edge in invalidated:
                invalidated_by.setdefault(edge.uuid, []).append(resolved_edge.uuid)

    # Archolith fork: copies of one stored edge take one merged state, after every mention's
    # temporal invalidation, so a supersession found by any mention survives every save order.
    reconcile_edge_copies(resolved_edges, invalidated_edges)

    await semaphore_gather(
        create_entity_edge_embeddings(embedder, resolved_edges),
        create_entity_edge_embeddings(embedder, invalidated_edges),
        max_coroutines=getattr(clients, 'max_coroutines', None),
    )

    if trace is not None:
        trace.collect_edge_operations(watched_edges)

    return resolved_edges, invalidated_edges, new_edges


def find_exact_duplicate_edge(
    edge: EntityEdge, seen: dict[tuple[str, str, str], EntityEdge]
) -> EntityEdge | None:
    """Return the earlier edge that `edge` duplicates, or add `edge` to `seen`.

    An edge is a duplicate of an earlier edge when the two have the same source, the
    same target, and the same normalized fact. It is also a duplicate when the two
    have opposite directions, the same normalized fact, and the same name.
    """
    key = (
        edge.source_node_uuid,
        edge.target_node_uuid,
        _normalize_string_exact(edge.fact),
    )
    if key in seen:
        return seen[key]

    reverse_key = (
        edge.target_node_uuid,
        edge.source_node_uuid,
        _normalize_string_exact(edge.fact),
    )
    reverse_edge = seen.get(reverse_key)
    if reverse_edge is not None and reverse_edge.name == edge.name:
        return reverse_edge

    seen[key] = edge
    return None


def _edge_types_for_endpoints(
    extracted_edge: EntityEdge,
    uuid_entity_map: dict[str, EntityNode],
    edge_types: dict[str, type[BaseModel]],
    edge_type_map: dict[tuple[str, str], list[str]],
) -> dict[str, type[BaseModel]]:
    """Return the custom edge types whose signature matches the edge's endpoint labels."""
    source_node = uuid_entity_map.get(extracted_edge.source_node_uuid)
    target_node = uuid_entity_map.get(extracted_edge.target_node_uuid)
    source_node_labels = source_node.labels + ['Entity'] if source_node is not None else ['Entity']
    target_node_labels = target_node.labels + ['Entity'] if target_node is not None else ['Entity']

    matching_edge_types: dict[str, type[BaseModel]] = {}
    for source_label in source_node_labels:
        for target_label in target_node_labels:
            for type_name in edge_type_map.get((source_label, target_label), []):
                type_model = edge_types.get(type_name)
                if type_model is not None:
                    matching_edge_types[type_name] = type_model
    return matching_edge_types


def enforce_edge_type_signatures(
    extracted_edges: list[EntityEdge],
    edge_types_lst: list[dict[str, type[BaseModel]]],
    edge_type_map: dict[tuple[str, str], list[str]],
    strict_edge_types: bool,
) -> tuple[list[EntityEdge], list[dict[str, type[BaseModel]]]]:
    """Apply the custom edge type signatures to the extracted edges.

    ``edge_types_lst[i]`` holds the custom edge types whose signature matches the
    endpoint labels of ``extracted_edges[i]``. An edge named after an edge type
    that declares at least one signature in ``edge_type_map``, but is not in its
    matching set, violates the signature. Such an edge is dropped when
    ``strict_edge_types`` is set, and renamed to ``DEFAULT_EDGE_NAME`` otherwise.
    Edges named after a type with no declared signature pass through unchanged,
    because such a type applies between any entity types.
    """
    signed_edge_types = {name for names in edge_type_map.values() for name in names}
    kept_edges: list[EntityEdge] = []
    kept_edge_types: list[dict[str, type[BaseModel]]] = []
    for extracted_edge, matching_edge_types in zip(extracted_edges, edge_types_lst, strict=True):
        if (
            extracted_edge.name in signed_edge_types
            and extracted_edge.name not in matching_edge_types
        ):
            if strict_edge_types:
                logger.info(
                    'Dropping edge %s: name %s does not match the endpoint signature',
                    extracted_edge.uuid,
                    extracted_edge.name,
                )
                continue
            logger.info(
                'Renaming edge %s from %s to %s: name does not match the endpoint signature',
                extracted_edge.uuid,
                extracted_edge.name,
                DEFAULT_EDGE_NAME,
            )
            extracted_edge.name = DEFAULT_EDGE_NAME
            extracted_edge.attributes = {}
        kept_edges.append(extracted_edge)
        kept_edge_types.append(matching_edge_types)
    return kept_edges, kept_edge_types


def _earliest(values: list[datetime | None]) -> datetime | None:
    earliest: datetime | None = None
    earliest_utc: datetime | None = None
    for value in values:
        value_utc = ensure_utc(value)
        if value_utc is not None and (earliest_utc is None or value_utc < earliest_utc):
            earliest, earliest_utc = value, value_utc
    return earliest


def _copy_identity(edge: EntityEdge) -> tuple:
    return edge.group_id, edge.source_node_uuid, edge.target_node_uuid, edge.fact


def reconcile_edge_copies(
    resolved_edges: list[EntityEdge], invalidated_edges: list[EntityEdge]
) -> None:
    """Give every copy of the same stored edge one merged state, in place.

    Mentions resolve in parallel, each against its own fetched copy of a stored edge, and the
    save is a full replace per row, so the last copy saved would win. One copy can be expired
    by a contradiction while another stays live. Every copy instead takes the earliest
    ``expired_at``, the earliest ``invalid_at`` (resolution only ever moves it earlier, so two
    copies that differ always differ by a truncation) and the union of ``episodes``, so any
    supersession found by one mention survives in any save order. ``attributes`` come from the
    last copy whose attributes resolution recomputed or cleared (list order; across a bulk
    batch, the episode later in the caller's list, not by date). A copy returned by the
    exact-fact fast path, or passed only as an invalidation candidate, is never the source,
    so its fetched attributes never override a recomputed copy; it takes the recomputed ones
    like every other copy. When no copy was recomputed, attributes are left as they are.

    Copies always agree on group, endpoints and fact. A uuid group that does not is not copies
    of one edge; it is left untouched and logged.
    """
    copies: dict[str, list[EntityEdge]] = {}
    for edge in (*resolved_edges, *invalidated_edges):
        copies.setdefault(edge.uuid, []).append(edge)
    for uuid, edges in copies.items():
        if len(edges) < 2:
            continue
        if len({_copy_identity(edge) for edge in edges}) > 1:
            logger.warning('Edges sharing uuid %s are not copies of one edge; not reconciled', uuid)
            continue
        expired_at = _earliest([edge.expired_at for edge in edges])
        invalid_at = _earliest([edge.invalid_at for edge in edges])
        episodes: list[str] = []
        for edge in edges:
            for episode_uuid in edge.episodes:
                if episode_uuid not in episodes:
                    episodes.append(episode_uuid)
        recomputed = [edge for edge in edges if edge._attributes_resolved]
        attributes = recomputed[-1].attributes if recomputed else None
        for edge in edges:
            edge.expired_at = expired_at
            edge.invalid_at = invalid_at
            edge.episodes = list(episodes)
            if attributes is not None:
                edge.attributes = dict(attributes)


def _apply_update_to_own_copy(resolved_edge: EntityEdge, update: EntityEdge) -> None:
    """Archolith fork: an update by uuid wins over its own stored copy chosen as its duplicate.

    add_triplet with an existing uuid: dedupe may resolve the update to that edge's stored copy,
    which kept the stored fact and dropped the update. The update's fact, name and attributes win.
    The validity window is one value: an update that sets either end replaces both (as on the
    non-duplicate path), else the stored window stays; mixing them could end it before it starts.
    The stored copy keeps created_at, expiry and the merged episodes.
    """
    if resolved_edge is update or resolved_edge.uuid != update.uuid:
        return
    resolved_edge.fact = update.fact
    resolved_edge.name = update.name
    resolved_edge.fact_embedding = update.fact_embedding
    if update.valid_at is not None or update.invalid_at is not None:
        resolved_edge.valid_at = update.valid_at
        resolved_edge.invalid_at = update.invalid_at
    if update.reference_time is not None:
        resolved_edge.reference_time = update.reference_time
    resolved_edge.attributes = {**(resolved_edge.attributes or {}), **(update.attributes or {})}


def _contradiction_invalidates(edge: EntityEdge, resolved_edge: EntityEdge) -> bool:
    """True when ``resolved_edge`` invalidates ``edge`` (the mutating branch below)."""
    # (Edge invalid before new edge becomes valid) or (new edge invalid before edge becomes valid)
    edge_invalid_at_utc = ensure_utc(edge.invalid_at)
    resolved_edge_valid_at_utc = ensure_utc(resolved_edge.valid_at)
    edge_valid_at_utc = ensure_utc(edge.valid_at)
    resolved_edge_invalid_at_utc = ensure_utc(resolved_edge.invalid_at)

    if (
        edge_invalid_at_utc is not None
        and resolved_edge_valid_at_utc is not None
        and edge_invalid_at_utc <= resolved_edge_valid_at_utc
    ) or (
        edge_valid_at_utc is not None
        and resolved_edge_invalid_at_utc is not None
        and resolved_edge_invalid_at_utc <= edge_valid_at_utc
    ):
        return False
    return (
        edge_valid_at_utc is not None
        and resolved_edge_valid_at_utc is not None
        and edge_valid_at_utc < resolved_edge_valid_at_utc
    )


async def _apply_temporal_invalidation(
    resolved_edge: EntityEdge,
    invalidation_candidates: list[EntityEdge],
    extracted_edge: EntityEdge,
    episode: EpisodicNode | None,
    edge_expiry_hook: EdgeExpiryHook | None = None,
    edge_invalidation_hook: EdgeInvalidationHook | None = None,
) -> list[EntityEdge]:
    """Expire the resolved edge and any candidate it contradicts.

    Every check here compares dates, so the caller must have dated the edge first.
    Returns the candidate edges this edge invalidated.

    Archolith fork: ``extracted_edge`` is the mention ``resolved_edge`` was resolved from
    (the same object when the edge is new). ``edge_expiry_hook`` may declare the edge's own
    ``invalid_at`` a world-time end (:mod:`graphiti_core.edge_expiry`) and
    ``edge_invalidation_hook`` may veto each expiry or invalidation
    (:mod:`graphiti_core.edge_invalidation`). With neither hook this is upstream behavior.
    """
    now = utc_now()

    # One answer per (reason, target, cause) pair in this call; the LLM may repeat an index.
    invalidation_answers: dict[tuple[EdgeInvalidationReason, int, int], bool] = {}

    async def invalidation_allowed(
        target: EntityEdge, cause: EntityEdge, reason: EdgeInvalidationReason
    ) -> bool:
        if edge_invalidation_hook is None:
            return True
        key = (reason, id(target), id(cause))
        if key not in invalidation_answers:
            decision = evaluate_edge_invalidation_decision(
                await edge_invalidation_hook.decide_edge_invalidation(
                    EdgeInvalidationContext(
                        target_edge=target,
                        cause_edge=cause,
                        reason=reason,
                        target_is_new=target is extracted_edge,
                        episode=episode,
                    )
                )
            )
            invalidation_answers[key] = decision is EdgeInvalidationDecision.INVALIDATE
        return invalidation_answers[key]

    # The edge's own end: upstream expires it; a hook may say it is a world-time end.
    world_end_utc: datetime | None = None
    if resolved_edge.invalid_at and not resolved_edge.expired_at:
        decision = EdgeExpiryDecision.EXPIRE
        if edge_expiry_hook is not None:
            decision = evaluate_edge_expiry_decision(
                await edge_expiry_hook.decide_edge_expiry(
                    EdgeExpiryContext(
                        extracted_edge=extracted_edge,
                        resolved_edge=resolved_edge,
                        is_duplicate=resolved_edge is not extracted_edge,
                        episode=episode,
                    )
                )
            )
        own_end_utc = ensure_utc(resolved_edge.invalid_at)
        own_start_utc = ensure_utc(resolved_edge.valid_at)
        # An inverted window (end <= start) is malformed; expire it as upstream does.
        has_window = own_start_utc is None or (
            own_end_utc is not None and own_end_utc > own_start_utc
        )
        if decision is EdgeExpiryDecision.WORLD_END and has_window:
            world_end_utc = own_end_utc
        elif (
            has_window
            and resolved_edge is not extracted_edge
            and resolved_edge.expired_at is None
            and not await invalidation_allowed(
                resolved_edge, extracted_edge, EdgeInvalidationReason.OWN_END
            )
        ):
            # Vetoed: the stored edge stays live and its own end bounds it like a world-time
            # end, so path 2 below can only move invalid_at earlier, never later.
            world_end_utc = own_end_utc
        elif edge_invalidation_hook is None or resolved_edge.expired_at is None:
            # With a hook, re-checked: its awaits can yield to a concurrent resolution of this
            # shared edge (bulk pass 1), whose expiry is kept. No hook: upstream, unchanged.
            resolved_edge.expired_at = now

    # Determine if the new_edge needs to be expired
    if resolved_edge.expired_at is None:
        invalidation_candidates.sort(key=lambda c: (c.valid_at is None, ensure_utc(c.valid_at)))
        resolved_edge_valid_at_utc = ensure_utc(resolved_edge.valid_at)
        for candidate in invalidation_candidates:
            candidate_valid_at_utc = ensure_utc(candidate.valid_at)
            if candidate_valid_at_utc is None:
                continue
            # A world-ended fact is superseded only from inside its window.
            if world_end_utc is not None and candidate_valid_at_utc >= world_end_utc:
                continue
            if resolved_edge_valid_at_utc is None:
                if world_end_utc is not None:
                    # Undated start: the window is open, so an in-window contradiction
                    # expires it as upstream would, without inventing a truncation date.
                    if not await invalidation_allowed(
                        resolved_edge, candidate, EdgeInvalidationReason.NEWER_CANDIDATE
                    ):
                        continue
                    # The await may have let a concurrent resolution end this shared edge.
                    if resolved_edge.expired_at is not None:
                        break
                    resolved_edge.expired_at = now
                    break
                continue
            if candidate_valid_at_utc > resolved_edge_valid_at_utc:
                if not await invalidation_allowed(
                    resolved_edge, candidate, EdgeInvalidationReason.NEWER_CANDIDATE
                ):
                    continue
                # The await may have let a concurrent resolution end this shared edge. Keep the
                # earlier end: never later than either sequential (no-hook) order would leave it.
                if resolved_edge.expired_at is not None:
                    current_end_utc = ensure_utc(resolved_edge.invalid_at)
                    if current_end_utc is None or candidate_valid_at_utc < current_end_utc:
                        resolved_edge.invalid_at = candidate.valid_at
                    break
                # Expire new edge since we have information about more recent events
                resolved_edge.invalid_at = candidate.valid_at
                resolved_edge.expired_at = now
                break

    # Determine which contradictory edges need to be expired
    contradiction_candidates = invalidation_candidates
    if edge_invalidation_hook is not None:
        # Only pairs the hook allowed are passed on; resolve_edge_contradictions re-checks the
        # predicate, so a candidate whose dates changed during an await is skipped, not ended.
        # The cause is the new mention (extracted_edge), never a stored duplicate it matched.
        contradiction_candidates = []
        for candidate in invalidation_candidates:
            if _contradiction_invalidates(candidate, resolved_edge) and await invalidation_allowed(
                candidate, extracted_edge, EdgeInvalidationReason.CONTRADICTION
            ):
                contradiction_candidates.append(candidate)
    invalidated_edges: list[EntityEdge] = resolve_edge_contradictions(
        resolved_edge, contradiction_candidates
    )

    return invalidated_edges


def resolve_edge_contradictions(
    resolved_edge: EntityEdge, invalidation_candidates: list[EntityEdge]
) -> list[EntityEdge]:
    if len(invalidation_candidates) == 0:
        return []

    # Determine which contradictory edges need to be expired
    invalidated_edges: list[EntityEdge] = []
    for edge in invalidation_candidates:
        # New edge invalidates edge
        if _contradiction_invalidates(edge, resolved_edge):
            edge.invalid_at = resolved_edge.valid_at
            edge.expired_at = edge.expired_at if edge.expired_at is not None else utc_now()
            invalidated_edges.append(edge)

    return invalidated_edges


async def _extract_edge_timestamps(
    llm_client: LLMClient,
    edge: EntityEdge,
    episode: EpisodicNode | None,
    *,
    clients: GraphitiClients | None = None,
) -> None:
    """Extract valid_at / invalid_at timestamps for an edge via a lightweight LLM call.

    Modifies the edge in place. Skips if the edge already has timestamps set
    (e.g., from the extraction prompt in the separate-extraction path) or if
    no reference time is available.
    clients: optional bundle that selects prompt overrides and model routes.
    """
    if edge.valid_at is not None or edge.invalid_at is not None:
        return

    reference_time = ensure_utc(
        edge.reference_time or (episode.valid_at if episode is not None else None)
    )
    if reference_time is None:
        return

    context = {
        'fact': edge.fact,
        'reference_time': reference_time.isoformat(),
    }
    try:
        llm_response = await generate_prompt_response(
            llm_client,
            'extract_edges.extract_timestamps',
            prompt_library.extract_edges.extract_timestamps,
            context,
            clients=clients,
            response_model=EdgeTimestamps,
            model_size=ModelSize.small,
        )
        timestamps = EdgeTimestamps(**llm_response)
        apply_extracted_timestamps(
            edge,
            timestamps.valid_at,
            timestamps.invalid_at,
            reference_time,
        )
    except Exception:
        logger.warning('Failed to extract timestamps for edge %s', edge.uuid, exc_info=True)


async def resolve_extracted_edge(
    llm_client: LLMClient,
    extracted_edge: EntityEdge,
    related_edges: list[EntityEdge],
    existing_edges: list[EntityEdge],
    episode: EpisodicNode,
    edge_type_candidates: dict[str, type[BaseModel]] | None = None,
    edge_expiry_hook: EdgeExpiryHook | None = None,
    edge_invalidation_hook: EdgeInvalidationHook | None = None,
    preserved_attributes: frozenset[str] = frozenset(),
    *,
    clients: GraphitiClients | None = None,
) -> tuple[EntityEdge, list[EntityEdge], list[EntityEdge]]:
    """Fully resolve one edge on its own: dedupe it, date it, then apply contradictions.

    This wrapper resolves one edge and applies its temporal contradictions.
    ``preserved_attributes`` is passed to :func:`_dedupe_extracted_edge`;
    ``edge_expiry_hook`` and ``edge_invalidation_hook`` to
    :func:`_apply_temporal_invalidation`. When absent, upstream behavior.
    """
    resolved_edge, duplicate_edges, candidates = await _dedupe_extracted_edge(
        llm_client,
        extracted_edge,
        related_edges,
        existing_edges,
        episode,
        edge_type_candidates,
        **preserved_attributes_kwargs(preserved_attributes),
        clients=clients,
    )
    if candidates is None:
        return resolved_edge, [], duplicate_edges
    invalidated_edges = await _apply_temporal_invalidation(
        resolved_edge,
        candidates,
        extracted_edge,
        episode,
        **edge_expiry_kwargs(edge_expiry_hook),
        **edge_invalidation_kwargs(edge_invalidation_hook),
    )
    return resolved_edge, invalidated_edges, duplicate_edges


@dataclass
class EdgeDecision:
    """The decision for one edge against its candidates.

    `exact_match` is a duplicate candidate with the same endpoints and the same
    normalized fact. When it has a value, no model call was made and the other fields
    are empty. `duplicates` are the duplicate candidates that the model selected, and
    the first one is the edge that the new edge resolves to. `contradicted` are the
    candidates that the new edge contradicts.
    """

    exact_match: EntityEdge | None = None
    duplicates: list[EntityEdge] = field(default_factory=list)
    contradicted: list[EntityEdge] = field(default_factory=list)
    started_at: float = field(default_factory=time)


async def decide_edge_against_candidates(
    llm_client: LLMClient,
    extracted_edge: EntityEdge,
    related_edges: list[EntityEdge],
    existing_edges: list[EntityEdge],
    *,
    clients: GraphitiClients | None = None,
    precomputed_decision: EdgeDuplicate | None = None,
) -> EdgeDecision:
    """Decide if an edge is a duplicate of a candidate and which candidates it contradicts.

    `related_edges` are the duplicate candidates. `existing_edges` are the invalidation
    candidates. The function extracts no attributes and no timestamps, and it changes
    no edge.

    A `precomputed_decision` (Archolith fork, menhir#91 batch edge dedupe) replaces the
    model call; the exact-match fast path and index validation still apply.
    """
    # Fast path: if the fact text and endpoints already exist verbatim, reuse the matching edge.
    normalized_fact = _normalize_string_exact(extracted_edge.fact)
    for edge in related_edges:
        if (
            edge.source_node_uuid == extracted_edge.source_node_uuid
            and edge.target_node_uuid == extracted_edge.target_node_uuid
            and _normalize_string_exact(edge.fact) == normalized_fact
        ):
            return EdgeDecision(exact_match=edge)

    start = time()

    invalidation_idx_offset = len(related_edges)

    if precomputed_decision is not None:
        response_object = precomputed_decision
    else:
        # Prepare context for LLM with continuous indexing
        related_edges_context = [{'idx': i, 'fact': edge.fact} for i, edge in enumerate(related_edges)]

        # Invalidation candidates start where duplicate candidates end
        invalidation_edge_candidates_context = [
            {'idx': invalidation_idx_offset + i, 'fact': existing_edge.fact}
            for i, existing_edge in enumerate(existing_edges)
        ]

        context = {
            'existing_edges': related_edges_context,
            'new_edge': extracted_edge.fact,
            'edge_invalidation_candidates': invalidation_edge_candidates_context,
        }

        if related_edges or existing_edges:
            logger.debug(
                'Resolving edge: sent %d EXISTING FACTS%s and %d INVALIDATION CANDIDATES%s',
                len(related_edges),
                f' (idx 0-{len(related_edges) - 1})' if related_edges else '',
                len(existing_edges),
                f' (idx {invalidation_idx_offset}-{invalidation_idx_offset + len(existing_edges) - 1})'
                if existing_edges
                else '',
            )

        llm_response = await generate_prompt_response(
            llm_client,
            'dedupe_edges.resolve_edge',
            prompt_library.dedupe_edges.resolve_edge,
            context,
            clients=clients,
            response_model=EdgeDuplicate,
            model_size=ModelSize.small,
        )
        response_object = EdgeDuplicate(**llm_response)
    duplicate_facts = response_object.duplicate_facts

    # Validate duplicate_facts are in valid range for EXISTING FACTS
    invalid_duplicates = [i for i in duplicate_facts if i < 0 or i >= len(related_edges)]
    if invalid_duplicates:
        logger.warning(
            'LLM returned invalid duplicate_facts idx values %s (valid range: 0-%d for EXISTING FACTS)',
            invalid_duplicates,
            len(related_edges) - 1,
        )

    duplicate_fact_ids: list[int] = [i for i in duplicate_facts if 0 <= i < len(related_edges)]

    # Process contradicted facts (continuous indexing across both lists)
    contradicted_facts: list[int] = response_object.contradicted_facts
    invalidation_candidates: list[EntityEdge] = []

    # Only process contradictions if there are edges to check against
    if related_edges or existing_edges:
        max_valid_idx = len(related_edges) + len(existing_edges) - 1
        invalid_contradictions = [i for i in contradicted_facts if i < 0 or i > max_valid_idx]
        if invalid_contradictions:
            logger.warning(
                'LLM returned invalid contradicted_facts idx values %s (valid range: 0-%d)',
                invalid_contradictions,
                max_valid_idx,
            )

        # Split contradicted facts into those from related_edges vs existing_edges based on offset
        for idx in contradicted_facts:
            if 0 <= idx < len(related_edges):
                # From EXISTING FACTS (duplicate candidates)
                invalidation_candidates.append(related_edges[idx])
            elif invalidation_idx_offset <= idx <= max_valid_idx:
                # From FACT INVALIDATION CANDIDATES (adjust index by offset)
                invalidation_candidates.append(existing_edges[idx - invalidation_idx_offset])

    return EdgeDecision(
        duplicates=[related_edges[idx] for idx in duplicate_fact_ids],
        contradicted=invalidation_candidates,
        started_at=start,
    )


async def _dedupe_extracted_edge(
    llm_client: LLMClient,
    extracted_edge: EntityEdge,
    related_edges: list[EntityEdge],
    existing_edges: list[EntityEdge],
    episode: EpisodicNode,
    edge_type_candidates: dict[str, type[BaseModel]] | None = None,
    preserved_attributes: frozenset[str] = frozenset(),
    *,
    clients: GraphitiClients | None = None,
    precomputed_decision: EdgeDuplicate | None = None,
) -> tuple[EntityEdge, list[EntityEdge], list[EntityEdge] | None]:
    """Deduplicate an extracted edge and gather the edges it might contradict.

    Stops before contradiction so the caller can apply temporal invalidation after
    the edge is dated.
    Returns (resolved_edge, duplicate_edges, invalidation_candidates). Candidates is
    None when the edge took an early return and must skip contradiction entirely;
    an empty list would instead mean "no candidates, but still run the checks".

    Parameters
    ----------
    llm_client : LLMClient
        Client used to invoke the LLM for deduplication and attribute extraction.
    extracted_edge : EntityEdge
        Newly extracted edge whose canonical representation is being resolved.
    related_edges : list[EntityEdge]
        Candidate edges with identical endpoints used for duplicate detection.
    existing_edges : list[EntityEdge]
        Broader set of edges evaluated for contradiction / invalidation.
    episode : EpisodicNode
        Episode providing content context when extracting edge attributes.
    edge_type_candidates : dict[str, type[BaseModel]] | None
        Custom edge types permitted for the current source/target signature.
    preserved_attributes : frozenset[str]
        Names of edge properties stored by the caller outside Graphiti. On the LLM
        duplicate path they keep their stored values when attributes are recomputed
        or cleared, so the edge save does not drop them; only a schema field the
        recomputation produced overrides one. A new (non-duplicate) edge is unaffected.
    clients: optional bundle that selects prompt overrides and model routes.

    Returns
    -------
    tuple[EntityEdge, list[EntityEdge], list[EntityEdge]]
        The resolved edge, any duplicates, and edges to invalidate.
    """
    resolver_kwargs = {'clients': clients} if clients is not None else {}

    def append_episode_provenance(resolved_edge: EntityEdge) -> None:
        for episode_uuid in [*extracted_edge.episodes, episode.uuid]:
            if episode_uuid not in resolved_edge.episodes:
                resolved_edge.episodes.append(episode_uuid)
        _apply_update_to_own_copy(resolved_edge, extracted_edge)

    if precomputed_decision is None:
        filtered = _prefilter_invalidation_candidates(extracted_edge, related_edges, existing_edges)
        if existing_edges and not filtered and not related_edges:
            # The prefilter emptied the candidates. Skip the model call but stay on the decision
            # path: the zero-candidate return below skips own-end expiry and edge_expiry_hook.
            precomputed_decision = EdgeDuplicate(duplicate_facts=[], contradicted_facts=[])
        existing_edges = filtered

    # Nothing to compare against, so there is no duplicate and nothing to contradict.
    if precomputed_decision is None and len(related_edges) == 0 and len(existing_edges) == 0:
        # Still extract custom attributes and timestamps even when no dedup needed
        edge_model = edge_type_candidates.get(extracted_edge.name) if edge_type_candidates else None
        if edge_model is not None and len(edge_model.model_fields) != 0:
            edge_attributes_context = {
                'fact': extracted_edge.fact,
                'reference_time': episode.valid_at if episode is not None else None,
                'existing_attributes': extracted_edge.attributes,
            }
            edge_attributes_response = await generate_prompt_response(
                llm_client,
                'extract_edges.extract_attributes',
                prompt_library.extract_edges.extract_attributes,
                edge_attributes_context,
                clients=clients,
                response_model=edge_model,  # type: ignore
                model_size=ModelSize.small,
                attribute_extraction=True,
            )
            merged, _ = apply_capped_attributes(
                edge_attributes_response,
                edge_model,
                extracted_edge.attributes,
                merge_mode='replace',
                prompt_name='extract_edges.extract_attributes',
                entity_uuid=extracted_edge.uuid,
                group_id=extracted_edge.group_id,
            )
            extracted_edge.attributes = merged
            extracted_edge._attributes_resolved = True

        await _extract_edge_timestamps(llm_client, extracted_edge, episode, **resolver_kwargs)

        return extracted_edge, [], None

    decision = await decide_edge_against_candidates(
        llm_client,
        extracted_edge,
        related_edges,
        existing_edges,
        clients=clients,
        **_precomputed_decision_kwargs(precomputed_decision),
    )

    # Fast path: the fact text and endpoints already exist verbatim, so reuse the matching edge.
    if decision.exact_match is not None:
        resolved = decision.exact_match
        append_episode_provenance(resolved)
        return resolved, [], None

    resolved_edge = extracted_edge
    for duplicate_edge in decision.duplicates:
        resolved_edge = duplicate_edge
        break

    if decision.duplicates:
        append_episode_provenance(resolved_edge)

    invalidation_candidates: list[EntityEdge] = decision.contradicted

    # Archolith fork (menhir#251): the edge being saved never contradicts its own stored
    # copy. On an update by uuid (add_triplet) the same-pair search returns that copy;
    # invalidating it would send two copies with different facts to the save, which keeps
    # the old fact. When the update resolves to a different edge, its old stored copy is
    # not saved alongside, so it may still be invalidated.
    self_copies = [c for c in invalidation_candidates if c.uuid == resolved_edge.uuid]
    if self_copies:
        logger.info(
            'Dropped the stored copy of edge %s from its own invalidation candidates',
            resolved_edge.uuid,
        )
        invalidation_candidates = [
            c for c in invalidation_candidates if c.uuid != resolved_edge.uuid
        ]

    # Only extract structured attributes if the edge's relation_type matches an allowed custom type
    # AND the edge model exists for this node pair signature
    # Stored values of caller-owned keys on a duplicate; a new edge clears as upstream.
    kept = (
        {
            key: value
            for key, value in (resolved_edge.attributes or {}).items()
            if key in preserved_attributes
        }
        if preserved_attributes and resolved_edge is not extracted_edge
        else {}
    )
    edge_model = edge_type_candidates.get(resolved_edge.name) if edge_type_candidates else None
    if edge_model is not None and len(edge_model.model_fields) != 0:
        edge_attributes_context = {
            'fact': resolved_edge.fact,
            'reference_time': episode.valid_at if episode is not None else None,
            'existing_attributes': resolved_edge.attributes,
        }

        edge_attributes_response = await generate_prompt_response(
            llm_client,
            'extract_edges.extract_attributes',
            prompt_library.extract_edges.extract_attributes,
            edge_attributes_context,
            clients=clients,
            response_model=edge_model,  # type: ignore
            model_size=ModelSize.small,
            attribute_extraction=True,
        )
        merged, _ = apply_capped_attributes(
            edge_attributes_response,
            edge_model,
            resolved_edge.attributes,
            merge_mode='replace',
            prompt_name='extract_edges.extract_attributes',
            entity_uuid=resolved_edge.uuid,
            group_id=resolved_edge.group_id,
        )
        resolved_edge.attributes = merged
    elif edge_type_candidates is not None:
        # Schema map was supplied but this relation has no model. Clear
        # attributes that belong to a prior schema. Do not wipe when the
        # caller omitted the map: add_triplet puts caller attributes on the
        # edge and never passes edge_type_candidates.
        resolved_edge.attributes = {}
    if kept:
        # Caller-owned properties survive the recompute/clear above and the full-replace save.
        # Only a schema field the recomputation produced beats the stored value: the attribute
        # LLM may echo (or null) any existing key it was shown.
        schema_fields = set(edge_model.model_fields) if edge_model is not None else set()
        recomputed = resolved_edge.attributes
        resolved_edge.attributes = {
            **recomputed,
            **{
                key: value
                for key, value in kept.items()
                if key not in schema_fields or key not in recomputed
            },
        }
    resolved_edge._attributes_resolved = True

    # Extract timestamps for new edges (duplicated edges retain their existing timestamps)
    if resolved_edge.uuid == extracted_edge.uuid:
        await _extract_edge_timestamps(llm_client, resolved_edge, episode, **resolver_kwargs)

    end = time()
    logger.debug(
        f'Resolved Edge: {extracted_edge.uuid} -> {resolved_edge.uuid}, '
        f'in {(end - decision.started_at) * 1000} ms'
    )

    return resolved_edge, decision.duplicates, invalidation_candidates


_ORIGINAL_DEDUPE_EXTRACTED_EDGE = _dedupe_extracted_edge
_ORIGINAL_DECIDE_EDGE_AGAINST_CANDIDATES = decide_edge_against_candidates


async def filter_existing_duplicate_of_edges(
    driver: GraphDriver, duplicates_node_tuples: list[tuple[EntityNode, EntityNode]]
) -> list[tuple[EntityNode, EntityNode]]:
    if not duplicates_node_tuples:
        return []

    duplicate_nodes_map = {
        (source.uuid, target.uuid): (source, target) for source, target in duplicates_node_tuples
    }

    if driver.provider == GraphProvider.NEPTUNE:
        query: LiteralString = """
            UNWIND $duplicate_node_uuids AS duplicate_tuple
            MATCH (n:Entity {uuid: duplicate_tuple.source})-[r:RELATES_TO {name: 'IS_DUPLICATE_OF'}]->(m:Entity {uuid: duplicate_tuple.target})
            RETURN DISTINCT
                n.uuid AS source_uuid,
                m.uuid AS target_uuid
        """

        duplicate_nodes = [
            {'source': source.uuid, 'target': target.uuid}
            for source, target in duplicates_node_tuples
        ]

        records, _, _ = await driver.execute_query(
            query,
            duplicate_node_uuids=duplicate_nodes,
            routing_='r',
        )
    else:
        query: LiteralString = """
            UNWIND $duplicate_node_uuids AS duplicate_tuple
            MATCH (n:Entity {uuid: duplicate_tuple[0]})-[r:RELATES_TO {name: 'IS_DUPLICATE_OF'}]->(m:Entity {uuid: duplicate_tuple[1]})
            RETURN DISTINCT
                n.uuid AS source_uuid,
                m.uuid AS target_uuid
        """
        duplicate_node_uuids = list(duplicate_nodes_map.keys())

        records, _, _ = await driver.execute_query(
            query,
            duplicate_node_uuids=duplicate_node_uuids,
            routing_='r',
        )

    # Remove duplicates that already have the IS_DUPLICATE_OF edge
    for record in records:
        duplicate_tuple = (record.get('source_uuid'), record.get('target_uuid'))
        if duplicate_nodes_map.get(duplicate_tuple):
            duplicate_nodes_map.pop(duplicate_tuple)

    return list(duplicate_nodes_map.values())
