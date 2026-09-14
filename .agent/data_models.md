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

- `CombinedExtraction` (`graphiti_core/prompts/extract_nodes_and_edges.py`, generic half of Menhir
  installer #2, `_patch_graphiti_combined_extraction_models`): a before model-validator sanitizes
  malformed provider rows. A non-dict top-level payload is returned unchanged (normal Pydantic
  validation applies; dict payloads are copied, never mutated). Missing/non-list/null arrays become
  `[]` in the validated model, while `extracted_entities` and `edges` stay declared required with
  their descriptions, so `model_json_schema()` still marks both arrays required. Entity rows:
  non-dicts dropped; name is the valid nonblank canonical `name`, else the first valid nonblank
  alias in `entity_name` then `entity` (trimmed); rows with no recoverable name are dropped;
  `entity_type_id` is `int()`-coerced, falling back to `-1` on `TypeError`/`ValueError` (bool
  `True` intentionally coerces to `1`); only `name` and `entity_type_id` are retained. Edge rows:
  non-dicts and rows whose `source_entity_name`, `target_entity_name`, `relation_type`, or `fact`
  is not a nonblank string are dropped; retained strings are preserved exactly (not stripped);
  a list `episode_indices` keeps only non-bool ints (defaulting to `[0]` when none survive);
  non-list/missing `episode_indices` becomes `[0]`; only the five fields are retained. Row order
  is preserved. The Menhir-specific half of installer #2 remains for Phase F.

The Menhir-side runtime patches themselves are NOT removed yet (Phase F owns that); only the fork source now
provides the same normalization natively.

## Entity Record Tolerance Contract (native, fork, `provider compatibility`)

`get_entity_node_from_record` (`graphiti_core/nodes.py`) now implements the fork half of Menhir installer #6
(`_patch_graphiti_entity_record_group_id`) natively — this entry is labeled `provider compatibility`. The
Menhir-side runtime patch remains installed until Phase F; the Menhir namespace/group-id policy itself is NOT in
the fork and stays with Phase F.

- Defensive copying: the outer record is shallow-copied before any mutation; dict `attributes` are copied before
  the upstream key-pops, and non-None `labels` are copied to a fresh list (including the `Entity_<group>` prefix
  strip), so caller-owned records, nested attributes, and label containers are never mutated. KUZU JSON-string
  `attributes` behavior is unchanged.
- Null group-id hook: when the record's `group_id` is None, `graphiti_core.nodes` consults the process-level
  resolver registered via `set_entity_record_group_id_resolver(resolver | None)` (typed
  `EntityRecordGroupIdResolver = Callable[[Mapping[str, Any], GraphProvider], str | None]`). Registration is startup
  configuration, not per-record monkeypatching. The resolver receives a read-only mapping over an isolated shallow
  snapshot of the record (the attributes dict and labels list are separately copied again), plus the provider; its
  authority is return-value-only — top-level assignment fails on the read-only mapping and nested mutation (if
  attempted) affects only the isolated snapshot, never caller input or the returned node. A `str` return
  (including empty string) is authoritative, `None` falls
  back to `helpers.get_default_group_id(provider)` (preserving FalkorDB's `'_'` and `''` elsewhere), a
  non-string/non-None return raises `TypeError`, and resolver exceptions propagate. Non-None stored group ids
  bypass the hook unchanged. No namespace interpretation, canonical-self behavior, or receipts live here.
- Legacy timestamp repair: a `created_at` string ending exactly in `Z[UTC]` has only that terminal suffix
  normalized to `+00:00` before `parse_db_date`; all other values pass through unchanged.
- Bounded diagnostics: null group-id repairs and `Z[UTC]` repairs each log once per record key (uuid/name) using
  sets capped at 512 keys per category; when a new key arrives at capacity, one existing key is evicted before the
  new key is added and logged, so first-seen keys always log exactly once per retention window and retained repeat
  keys never re-log; logs carry record identity and the chosen group/original timestamp, never attributes.
- `search/search_utils.py` already imports this function object, so all search call sites get the native behavior
  with no symbol rebinding.

## Single-Episode Extraction Routing Contract (native, fork)

`Graphiti.add_episode` (`graphiti_core/graphiti.py`) routes single-episode extraction through the combined
extractor by default — a deliberate fork divergence from upstream v0.29.3's separate path — with a typed neutral
hook defined in `graphiti_core/extraction_routing.py`:

- `ExtractionRoute` (`str` enum): `COMBINED` (one LLM call via
  `combined_extraction.extract_nodes_and_edges`) or `SEPARATE` (upstream two-call `extract_nodes` then
  `extract_edges` path).
- `default_extraction_route(edge_types)`: `SEPARATE` when `edge_types` is non-empty — a deliberate compatibility
  boundary that keeps custom-schema episodes on the exact pre-C1g upstream/installer path (NOT because attribute
  handling is missing: combined-route edges also pass through `resolve_extracted_edges`, which performs the
  downstream typed-attribute work); `COMBINED` otherwise (`None` and `{}` both route combined).
- `SingleEpisodeExtractionContext`: frozen dataclass of borrowed request-local extraction inputs (clients, episode,
  previous_episodes, entity_types, excluded_entity_types, edge_type_map, edge_types,
  custom_extraction_instructions). The container is frozen (fields cannot be reassigned) but its contents are
  shared with, and owned by, the in-flight `add_episode` call — they are NOT copied and no runtime immutability is
  enforced; hooks must treat them as read-only and must not mutate them.
- `SingleEpisodeExtractionResult`: frozen dataclass (nodes, edges, node_episode_index_map) a hook may return to
  replace extraction entirely; the index map keys node UUIDs to 0-indexed episode positions.
- `SingleEpisodeExtractionHook`: runtime-checkable protocol with one method,
  `async extract_single_episode(context) -> ExtractionRoute | SingleEpisodeExtractionResult | None`. Invoked at
  most once per `add_episode` call, before extraction. `None` defers to default routing; an `ExtractionRoute`
  forces a built-in route; a `SingleEpisodeExtractionResult` makes Graphiti skip its own extraction calls and
  resolve/persist the supplied nodes and edges. Any other return value raises `TypeError`. Hook exceptions,
  including `asyncio.CancelledError`, propagate unchanged.

State semantics: route choice and edges travel as per-call function arguments only (`_extract_single_episode`
returns the edges; `_extract_and_resolve_edges(precomputed_edges=...)` skips `extract_edges` when they are
present). There is no module-global, class-level, or `ContextVar` state, so concurrent `add_episode` calls are
isolated, exceptions leave nothing to reset, and cancellation is simply task cancellation. The default `add_episode`
span gains an `extraction.route` attribute (`combined`, `separate`, or `hook`).
