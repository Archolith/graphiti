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

## None-Hardening Contract (native, fork)

These models enforce None-tolerance natively via Pydantic validators and direct method hardening — no runtime
patching. This migrates three Menhir installer contracts into the fork source:

- `EntityNode` (Menhir installer #7): an explicit `summary=None` at construction coerces to `''`; omission keeps the
  `str` default factory (`''`); non-None values pass through unchanged (`coerce_none_summary` before-validator).
- `EntityEdge` (Menhir installer #8): an explicit `None` for `uuid` and `episodes` is dropped so their default
  factories run (fresh uuid4 string / fresh empty list); an explicit `None` for the required str fields `group_id`,
  `name`, `fact`, `source_node_uuid`, `target_node_uuid` coerces to `''`; missing and non-None values keep upstream
  behavior (`coerce_none_fields` before model-validator). `episodes` uses `default_factory=list` (no shared default).
- Embedding safety (Menhir installer #5): `EntityEdge.generate_embedding` hardens `fact is None` and
  `EntityNode`/`CommunityNode.generate_name_embedding` harden `name is None` to `''` before calling the embedder, so
  newline replacement cannot crash; observable behavior matches the legacy wrapper (field ends as `''`, embedder
  receives the empty string).

## Response Normalization Contract (native, fork)

These response models tolerate malformed LLM provider output natively via Pydantic before model-validators —
no runtime patching. This migrates two Menhir installer contracts into the fork source:

- `ExtractedEntity` (`graphiti_core/prompts/extract_nodes.py`, Menhir installer #9): a before model-validator
  normalizes degenerate provider payloads. Alias recovery runs only when the canonical `name` key is absent
  (a present-but-invalid canonical `name` proceeds to normal Pydantic validation). It accepts `entity_name`
  or a string `entity` as the name, plus key-typo tolerance (trim/lower and trailing `-`/`_`/space, e.g.
  `name-`, `Name `); accepts the degenerate single-pair `{<entity name>: <integer type id>}` shape; and
  resolves a missing `entity_type_id` in the reviewed installer precedence: `type_id`; integer `type`;
  present `type_name` discards the value and defaults to 0 (winning over coexisting `entity_type`/`entity`);
  integer `entity_type` else 0; remaining `entity` integer-coercible else 0; final default 0.
  Missing/unrecoverable `name` still raises `ValidationError`. `episode_indices` deliberately keeps the
  upstream v0.29.3 default `[0]` — this is a correction of the stale Menhir replacement model, whose
  `default_factory=list` (`[]`) was copied with a comment incorrectly claiming it mirrored upstream
  (accidental patch drift, now fixed).
- `NodeResolutions` (`graphiti_core/prompts/dedupe_nodes.py`, Menhir installer #10): a before model-validator
  normalizes `entity_resolutions` (fresh-list default; missing/null yields `[]`; a malformed non-sequence
  top-level value fails safely to `[]` instead of raising). Non-dict entries and entries without an
  integer-coercible `id` are dropped (bools are never accepted as integer ids); retained entries get
  `name=''` when missing/null and `duplicate_candidate_id=-1` when missing/null/non-integer-coercible
  (bools included; integer-coercible values are cast to `int`). Valid entries and their order are unchanged;
  the existing `NodeDuplicate` model still validates retained canonical fields.

The Menhir-side runtime patches themselves are NOT removed yet (Phase F owns that); only the fork source now
provides the same normalization natively.
