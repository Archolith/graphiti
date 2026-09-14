# graphiti — Data Models

All models are Pydantic v2 (BaseModel) definitions in `graphiti_core`.

## Entities (Nodes)

Canonical definitions live in `graphiti_core/nodes.py`; derived/auxiliary node types live in `graphiti_core/models/nodes/`.

| Model | Location | Fields beyond base |
|-------|----------|--------------------|
| `EpisodicNode` | `graphiti_core/nodes.py` | `source` (EpisodeType), `source_description`, `content`, `valid_at`, `entity_edges`, `episode_metadata` |
| `EntityNode` | `graphiti_core/nodes.py` | `name_embedding`, `summary`, `attributes` |
| `CommunityNode` | `graphiti_core/nodes.py` | `name_embedding`, `summary` |

Base class: `Node` (same file) carries `uuid`, `name`, `group_id`, `labels`, `created_at`.

## Edges

Canonical definitions live in `graphiti_core/edges.py`; derived/auxiliary edge types live in `graphiti_core/models/edges/`.

| Model | Location | Fields beyond base |
|-------|----------|--------------------|
| `EpisodicEdge` | `graphiti_core/edges.py` | none (link between an `EpisodicNode` and an `EntityNode`) |
| `EntityEdge` | `graphiti_core/edges.py` | `name` (relation name), `fact`, `fact_embedding`, `episodes`, `expired_at`, `valid_at`, `invalid_at`, `reference_time`, `attributes` |
| `CommunityEdge` | `graphiti_core/edges.py` | none (membership link between a `CommunityNode` and another node) |

Base class: `Edge` (same file) carries `uuid`, `group_id`, `source_node_uuid`, `target_node_uuid`, `created_at`.

## Enums

| Enum | Location | Values |
|------|----------|--------|
| `EpisodeType` | `graphiti_core/nodes.py` | `message`, `json`, `text`, `fact_triple` |

## Community Model

`CommunityNode` + `CommunityEdge` represent entity clusters; community detection/summary logic lives in
`graphiti_core/utils/maintenance/` and community operations in `graphiti_core/driver/operations/community_*_ops.py`.

## DTOs / Operation Models

`graphiti_core/graphiti_types.py` and `graphiti_core/models/` hold request/response and derived model types used by
`Graphiti` methods (search results, entity distinction outputs, etc.).

## Persistence / Repository Reference

- Storage access is behind `GraphDriver` (`graphiti_core/driver/driver.py`) with per-entity operation ABCs in
  `graphiti_core/driver/operations/` (e.g. `EntityNodeOperations`, `EntityEdgeOperations`,
  `EpisodicEdgeOperations`, `CommunityNodeOperations`, `CommunityEdgeOperations`).
- Implementations: `neo4j/`, `falkordb/`, `neptune/`, and deprecated `kuzu/` under `graphiti_core/driver/`.
- Namespace-scoped bulk accessors live in `graphiti_core/namespaces/` (`EntityNodeNamespace`,
  `CommunityNodeNamespace`, `EntityEdgeNamespace`, `EpisodicEdgeNamespace`, `CommunityEdgeNamespace`).

Do not invent fields not present in the source; the models above are the authoritative surface at v0.29.3.
