# Changelog — graphiti

## 2026-11-26 — Default-group searches stay in the default group

- `search/search.py`: `group_ids=['']` is the default group, not "every group". Upstream turned it
  into `None`, so the duplicate and invalidation candidate searches for a default-group edge (Menhir's
  default namespace) returned other groups' edges, and a default-group episode could expire them. An
  empty list still means no group filter.
- `search/search_utils.py`, `driver/neo4j/operations/search_ops.py`,
  `driver/relate/operations/search_ops.py`: Lucene drops an empty `group_id:""` clause, so a group
  list holding `''` gets no Lucene group clause; the Cypher `group_id IN $group_ids` filter scopes it.
  Before, `['', 'x']` narrowed to `x` alone.
- Not changed: FalkorDB (one graph per group). Edges with a null `group_id` match no group list.
- Tests: `tests/utils/search/test_default_group_scope.py`; `tests/test_default_group_search_scope_int.py`
  (disposable Neo4j) fails before the fix. The unit test joins the compatibility workflow.

## 2026-11-25 — Review fixes for batched edge deduplication (menhir#91 Path B)

- `edge_operations.py`:
  - An edge whose invalidation candidates the 1.6 prefilter empties, with no duplicate candidates,
    gets an empty decision instead of the zero-candidate return. Own-end expiry and
    `edge_expiry_hook` run for it, as before the prefilter.
  - `ResolutionTrace` records the candidates from before the prefilter, so commit repair does not
    decide the dropped ones again.
  - A batch answer with an idx outside its own request's range rejects the chunk.
  - `RateLimitError`, `openai.APIError` and `httpx.HTTPError` propagate from a chunk instead of
    falling back per-edge.
  - The dead `configured_ceiling` cap is removed; a chunk over the request guard's ceiling falls
    back per-edge.
  - `decide_edge_against_candidates` gets `precomputed_decision` only when one exists; replacing
    that function turns batching off.
- `prompts/dedupe_edges.py`: batch items validate like `EdgeDuplicate`; the lenient pre-validator is
  removed. `resolve_edges_batch` is no longer abstract, and `validate_prompt_library` accepts a
  library without it. The batch prompt carries `resolve_edge`'s rules and all eight of its examples
  (the Sam, Priya and payments examples were missing; 1,648 `o200k_base` system tokens), and a
  parity test fails if a `resolve_edge` example fact is absent from it.
- Tests: `test_batch_edge_dedupe_review_fixes.py`; the legacy prompt test context gains
  `edge_requests`. Both batch test files and that test join the compatibility workflow.

## 2026-11-25 — Batched edge deduplication, temporal invalidation prefilter, and exact-edge pre-search bypass (menhir#91 Path B)

- `graphiti_core/prompts/dedupe_edges.py`, `prompts/names.py`, `prompts/lib.py`,
  `llm_client/prompt_config.py`: add `EdgeDuplicateBatchItem`, `EdgeDuplicateBatch`, and static-first
  cacheable `resolve_edges_batch` (`dedupe_edges.resolve_edges_batch`, > 1,024 `o200k_base` system
  tokens) to resolve bounded chunks of independent edge deduplication requests in a single LLM call.
- `graphiti_core/utils/maintenance/edge_operations.py`:
  - Phase 3 batched edge resolution in `resolve_extracted_edges` (`batch_edge_dedupe: bool = True`):
    batches >= 2 semantic edges into bounded chunks (`EDGE_DEDUP_BATCH_MAX_ITEMS = 12`,
    `EDGE_DEDUP_BATCH_MAX_CHARS = 24_000`), validates exact `request_id` bijection
    with per-chunk fallback to `dedupe_edges.resolve_edge`, preserves custom prompt routing /
    overrides untouched, and feeds decisions through `_dedupe_extracted_edge` so all provenance,
    attribute, self-copy, and temporal-invalidation logic stays single-sourced.
  - Item 1.6 deterministic temporal prefilter (`_prefilter_invalidation_candidates`): drops
    invalidation candidates whose validity window provably ended (`candidate.invalid_at <= r.valid_at`)
    for every possible resolved edge in `[extracted_edge, *related_edges]` before the LLM call (never
    filtering duplicate candidates).
  - Item 1.7 exact-edge pre-search bypass: resolves verbatim `existing_edges_override` hits before
    both hybrid searches and skips the unfiltered graph-wide invalidation search when the pair-scoped
    search already returned a verbatim same-direction match.
- Tests: `tests/utils/maintenance/test_batch_edge_dedupe.py`; updated exact-fact search-count
  assertions in `tests/utils/maintenance/test_edge_operations.py` and
  `tests/utils/maintenance/test_edge_dedup_scenarios.py`.
## 2026-11-25 — Cacheable static-first dedupe prompts and default-namespace exact-name lookup (menhir#91, menhir#93)

- `graphiti_core/prompts/dedupe_edges.py`: move all static rules, constraints, and disambiguation
  examples for `resolve_edge` into `Message(role='system')` (1,091 `o200k_base` tokens, clearing
  OpenAI's 1,024-token message-boundary cache floor on GPT-5.6+/GPT-6 Luna), leaving only per-edge
  `<EXISTING FACTS>`, `<FACT INVALIDATION CANDIDATES>`, and `<NEW FACT>` in `role='user'`.
- `graphiti_core/prompts/dedupe_nodes.py`: move all static rules, naming constraints, and
  disambiguation examples for `nodes` (1,102 tokens) and `node` (1,091 tokens) into `role='system'`,
  replacing positional `"Each of the above ENTITIES"` wording and keeping only per-call dynamic
  messages, entities, candidates, and entity-count reminders in `role='user'`.
- `graphiti_core/nodes.py`, `graphiti_core/utils/maintenance/node_operations.py`: preserve empty
  string `group_id == ""` in `EntityNode.get_by_names` and `_collect_exact_name_candidate_nodes`
  (`is not None` instead of truthiness) so exact-name lookup runs for Menhir's `default` namespace.
- Tests: `tests/prompts/test_dedupe_prompts_cacheable.py`,
  `tests/utils/maintenance/test_exact_name_empty_group_id.py`.

## 2026-11-25 — Sync upstream 0.32.1 (`0.32.1.post1`)

- Merged upstream `v0.32.1` (concurrent ingestion #2154, group-checked UUID operations #2178, identity
  merge #2153, Kuzu removal #2151, Relate driver). Every fork seam is kept; where upstream rewrote the
  same code (node resolution split into `resolve_extracted_nodes` / `resolve_nodes_with_candidates`,
  `decide_edge_against_candidates`, `_entity_node_record` / `_entity_edge_record`, `write_timeout`) its
  structure is adopted and the fork seam re-applied. `_entity_edge_record` uses `entity_edge_save_row`.
- `graphiti.py`, `ingestion/scheduler.py`: `Graphiti.add_episode` / `add_episode_bulk` and the public
  `ConcurrentIngestor.add_episode` / `add_episode_bulk` raise `ValueError` before concurrent ingestion
  when a fork seam is in use (`new_episode_uuid`, `publish_fence`, any fork hook,
  `preserved_edge_attributes`); `single_episode_extraction_hook` gets a class-level default.
- `ingestion/phases.py`: the invalidation call uses the fork's async `_apply_temporal_invalidation`.
- `driver/relate/operations/entity_edge_ops.py`: upstream's file, unchanged (no copy reconciliation in
  `save_bulk`; Menhir uses Neo4j only). `.agent/architecture.md` lists what concurrent ingestion lacks.
- `add_episode` always passes `identity_properties` to `resolve_extracted_nodes`, as upstream does.
- Kuzu removed from the fork tests and the compatibility workflow lists (Relate ops added);
  `pyproject.toml` version `0.32.1.post1`, `uv.lock` regenerated, `README.md` version lines.
- Tests: `test_concurrent_ingestion_fork_guard.py` (also in the compatibility workflow); the two resolver
  census tests exempt `graphiti_core/ingestion/`; upstream test stubs gain the fork attributes, and three
  identity-property tests pin the separate extraction route. No assertions changed.

## 2026-11-22 — Release prep for `v0.31.2.post4`

- `pyproject.toml`, `uv.lock` (project entry only), `.github/workflows/release-graphiti-core.yml` (reviewed
  wheel version check), `README.md` (install line and release source), `CHANGELOG.md`: bump the published
  version to `0.31.2.post4`, which carries #28, #29 and #30 (entity-edge save fixes, menhir#251, #256,
  #259-#262, review F8) from `menhir/main`. Tag `v0.31.2.post4` on the merged release-prep commit
  triggers the PyPI release.
- `.github/workflows/menhir-compatibility.yml`: the offline tests from #28-#30
  (`test_entity_edge_save_rows`, `test_entity_edge_saved_expiry`, `test_entity_edge_save_after_delete`,
  `test_add_triplet_self_candidate`, `test_preserved_edge_attributes_validation`) join the fork-contracts
  pytest step; their files, the live `_int` tests and the edited driver ops join ruff, and the driver ops
  and `record_parsers.py` join pyright.

## 2026-11-22 — add_triplet update wins over its own stored copy chosen as duplicate

- `utils/maintenance/edge_operations.py`: `_apply_update_to_own_copy`, called where dedupe picks
  a duplicate, gives the update's fact, name, attributes and validity window (both ends together,
  when it sets either) to the edge's own stored copy, so an update by uuid is no longer replaced by the stale stored fact (PR #28 review F8).
- Tests: `test_add_triplet_self_candidate.py` (LLM duplicate and exact-fact paths),
  `test_add_triplet_self_candidate_int.py` (live, duplicate answer).

## 2026-11-22 — add_triplet update keeps its fact when the stored copy is contradicted

- `utils/maintenance/edge_operations.py`: `_dedupe_extracted_edge` never returns the stored copy
  of the edge being saved (the resolved edge's uuid) as an invalidation candidate. The same-pair search returns
  that copy on an update by uuid; when the LLM marked it contradicted, the save kept the old
  fact and expired the edge (menhir#251, the path PR #28 left open).
- Tests: `test_add_triplet_self_candidate.py` (real resolution, LLM stubbed),
  `test_add_triplet_self_candidate_int.py` (live Neo4j search and save).

## 2026-11-22 — Entity-edge save: one row builder, save-time reconcile, reported expiry

- `edges.py`: `entity_edge_save_row` builds every full-replace save row (all core fields
  including `reference_time`; attributes never shadow a core field) (menhir#260);
  `entity_edge_save_records` and `apply_saved_expiry` give in-memory edges the expiry the Neo4j
  save kept and log each restore (menhir#259).
- `driver/{neo4j,falkordb,neptune}/operations/entity_edge_ops.py`: rows from
  `entity_edge_save_row`; `save_bulk` reconciles copies first; Neo4j ops apply the saved expiry.
  `driver/kuzu/operations/entity_edge_ops.py`: passes `reference_time`.
- `utils/bulk_utils.py`: `add_nodes_and_edges_bulk_tx` reconciles copies before building rows
  (menhir#262) and, on Neo4j, applies the saved expiry.
- `models/edges/edge_db_queries.py`: Neo4j save queries also return `expired_at`/`invalid_at`.
- `graphiti.py`: `add_triplet` never offers an edge's own stored copy as an invalidation
  candidate (menhir#251); `preserved_edge_attributes` is a validated property that rejects
  mappings and core field names (menhir#256).
- menhir#261: `EntityEdge._loaded_from_store` (set by both record builders); `stored=True`
  variants of the Neo4j save queries skip an edge another writer deleted instead of recreating
  it; `save_entity_edge_rows_neo4j` splits bulk batches; `report_deleted_edges_not_saved` logs
  each skip. The mark is bound to the uuid it was loaded under (`_loaded_uuid`), so an edge
  given a new uuid (`add_triplet`'s endpoint-mismatch branch) saves as a new edge (PR #28
  review F1).
- `preserved_edge_attributes` also rejects names with surrounding whitespace (review F10).
- `architecture.md`: copies, stale-save, deleted-edge and preserved-attribute sections updated.

## 2026-11-10 — Publish fence hook and caller-supplied episode uuid (Menhir policy divergence)

- `publish_fence.py` (new): neutral `PublishFence` Protocol (`before_write`, `after_write`) and
  `publish_fence_kwargs`.
- `utils/bulk_utils.py`: `add_nodes_and_edges_bulk(_tx)` take `publish_fence`, `fence_episode_uuid`,
  `previous_episode_uuids`; `before_write` runs after the embedding loops and before the first save,
  `after_write` after the last save, inside the same transaction. No fence, no change.
- `graphiti.py`: `add_episode(new_episode_uuid=, publish_fence=)`; captures the retrieved
  `previous_episodes` uuids and threads them through `_process_episode_data`. `ValueError` for
  `uuid` + `new_episode_uuid`, and for `publish_fence` + `saga` / `update_communities`.
- `tests/test_publish_fence.py`: unit tests with a fake tx and Neo4j `_int` tests (rollback,
  atomic hook write, retry).

## 2026-11-02 — Treat cut-off LLM replies as truncation

- `llm_client/openai_generic_client.py`: `finish_reason=length` raises `OutputTruncatedError`
  before parsing. A reply that opens as a JSON document is decoded only from its start, so a
  nested fragment can't be returned as the payload. Prose-wrapped replies still get the scan.
- `llm_client/errors.py`, `llm_client/__init__.py`: add and export `OutputTruncatedError`.
- `tests/llm_client/test_openai_generic_client_truncation.py`: cut-off and malformed reply shapes.

## 2026-10-23 — Merge upstream graphiti-core 0.31.2 into `menhir/main`

- Merge `getzep/graphiti` `main` @ `3ffc978` (graphiti-core 0.31.2; merge base `eaa41286`) on
  `chore/sync-upstream-0.31.2`; `pyproject.toml` version `0.31.2.post1`, `uv.lock` regenerated.
  Every fork hook, constructor option, prompt change and robustness fix is kept (retention table
  in the PR).
- `utils/maintenance/edge_operations.py`: upstream split edge resolution into
  `_dedupe_extracted_edge` and `_apply_temporal_invalidation`, with temporal invalidation
  sequential after all dedupes. The fork's hook-aware temporal block (edge-expiry and
  edge-invalidation hooks, re-checks after awaits) moved verbatim into an async
  `_apply_temporal_invalidation`; preserved attributes and `_attributes_resolved` live in
  `_dedupe_extracted_edge`; `reconcile_edge_copies` runs after the invalidation loop. All 11
  edge-invalidation mutants are still killed.
- `graphiti.py`: upstream prompt library / `LLMRuntime`, dangling-endpoint merge (forwards the
  node-resolution hooks when configured) and `precomputed_edges` coexist; class-level
  `max_coroutines` default. `node_operations.py`: upstream identity-property and exact-name
  candidates join the pool before the candidate filter; the untyped defensive copy is kept in
  `extract_attributes_only_from_nodes`. `openai_generic_client.py`: request guard sees the
  `model=` override.
- `prompts/extract_nodes_and_edges.py`: the cache split now carries upstream's C2 prompt text
  byte for byte (SHA pins re-taken from upstream 0.31.2).
- Tests: minimal fork-test edits for upstream signature changes (`max_tokens` on mocked LLM
  clients, `query_vector=` on fake searches, `extract_edges` tuple return, patching the split
  resolver, `clients=` on `add_triplet`, structural guards include
  `_apply_temporal_invalidation`); upstream's C2 layout test asserts the split layout.
  `menhir-compatibility.yml` adds the cache test and six upstream offline suites.

## 2026-10-23 — Edge-invalidation hook: host veto on expiring an edge

- `graphiti_core/edge_invalidation.py`: NEW. `EdgeInvalidationHook`, `EdgeInvalidationContext`,
  `EdgeInvalidationDecision` (`INVALIDATE`/`KEEP`), `EdgeInvalidationReason` (`OWN_END`,
  `NEWER_CANDIDATE`, `CONTRADICTION`), `edge_invalidation_kwargs`.
- `graphiti_core/utils/maintenance/edge_operations.py`: `resolve_extracted_edge` asks the hook
  before each of the three expiry sites, once per pair and only when a write would follow; `KEEP`
  never leaves an edge more invalidated than no hook (an own-end veto bounds later truncation to
  the edge's own `invalid_at`; an inverted window is expired unasked). With a hook, writes
  after a hook await re-check the shared edge (bulk pass 1 interleaving): an existing expiry
  is kept and the earlier `invalid_at` wins. `resolve_edge_contradictions` shares its predicate via
  `_contradiction_invalidates` with identical behavior. Threaded through
  `resolve_extracted_edges`, `dedupe_edges_bulk` and every `Graphiti` resolver call; no hook keeps
  the legacy call shape. For Menhir: agent-recorded memories must never expire a user statement.
- `tests/test_edge_invalidation.py`: NEW, in `menhir-compatibility.yml` (ruff, pyright, pytest).

## 2026-10-11 — Release prep for `v0.30.2.post7`

- `pyproject.toml`, `uv.lock` (project entry only), `.github/workflows/release-graphiti-core.yml` (reviewed
  wheel version check), `README.md`, `CHANGELOG.md`: bump the published version to `0.30.2.post7`, which carries
  #13 (a stale Neo4j save never undoes a stored expiry) from `menhir/main` @ `1e0f06a`. Tag
  `v0.30.2.post7` on the merged release-prep commit triggers the PyPI release.

## 2026-10-11 — A stale save never makes a stored expired edge live again (Neo4j)

- `graphiti_core/models/edges/edge_db_queries.py`: the Neo4j entity-edge save and bulk save
  lock the relationship, read the stored `expired_at`/`invalid_at`, replace, and, once the
  stored edge is expired, keep the earliest `expired_at` and earliest non-null `invalid_at`.
  Fixes the Menhir P3c-1 race (a writer that fetched an edge before another writer expired it
  saved it back live) and review M1 (a stale save that expired the edge itself moved the
  truncated end later). Other providers are unchanged.
- `tests/test_entity_edge_save_keeps_expiry.py` (offline, in the Menhir compatibility
  workflow) and `tests/test_entity_edge_save_keeps_expiry_int.py` (live Neo4j via
  `GRAPHITI_SAVE_TEST_NEO4J_URI`): NEW. Mutants (no restore, no lock before the read, restore
  `expired_at` only, last writer wins when the save carries an expiry) each fail live tests.
  `menhir-compatibility.yml` adds `edge_db_queries.py` to pyright.

## 2026-10-10 — Release prep for `v0.30.2.post6`

- `pyproject.toml`, `uv.lock` (project entry only), `.github/workflows/release-graphiti-core.yml` (reviewed
  wheel version check), `README.md`, `CHANGELOG.md`: bump the published version to `0.30.2.post6`, which carries
  #11 (caller-preserved edge attributes) from `menhir/main` @ `8c2f0b5`. Tag
  `v0.30.2.post6` on the merged release-prep commit triggers the PyPI release.

## 2026-10-10 — Caller-preserved edge attributes

- `graphiti_core/graphiti.py`: `Graphiti(preserved_edge_attributes=...)` (iterable of property
  names, stored as a frozenset; a string or a non-str/blank entry is a `TypeError`, checked
  before any driver is created), threaded to every resolver call with
  `preserved_attributes_kwargs`.
- `graphiti_core/utils/maintenance/edge_operations.py`: `resolve_extracted_edge` and
  `resolve_extracted_edges` take `preserved_attributes`; on the LLM duplicate path the named
  keys keep their stored values when attributes are recomputed or cleared, before the
  edge-expiry hook. Only a schema field the recomputation produced overrides one (the attribute
  LLM may echo or null other keys); a new edge is unaffected.
  `graphiti_core/utils/bulk_utils.py`: `dedupe_edges_bulk` passes it.
- Review: `.agent/reviews/graphiti-preserved-edge-attributes-pr11-review.md` in the workspace.
- `tests/test_preserved_edge_attributes.py`: NEW; added to `menhir-compatibility.yml`.
  Motivation: Menhir anchored-time P3c (a restatement must not drop the contract and stamp).

## 2026-10-10 — Release prep for `v0.30.2.post5`

- `pyproject.toml`, `uv.lock` (project entry only), `.github/workflows/release-graphiti-core.yml` (reviewed
  wheel version check), `README.md`, `CHANGELOG.md`: bump the published version to `0.30.2.post5`, which carries
  #9 (recomputed attributes win when reconciling edge copies) from `menhir/main` @ `a36e307`. Tag
  `v0.30.2.post5` on the merged release-prep commit triggers the PyPI release.

## 2026-10-08 — Reconciled copies take recomputed attributes, not fast-path ones

- `graphiti_core/edges.py`: `EntityEdge` gains the private, never-saved
  `_attributes_resolved` mark.
- `graphiti_core/utils/maintenance/edge_operations.py`: `resolve_extracted_edge` sets the mark
  where it computes or clears attributes; `resolve_extracted_edges` resets it on the extracted
  edges and every candidate before resolving; `reconcile_edge_copies` takes `attributes` from
  the last marked copy and leaves attributes alone when none is marked. Previously it took the
  first resolved copy's, and an exact-fact fast-path copy (stored attributes untouched) listed
  first overwrote a paraphrase's recomputed or cleared attributes in every save order
  (Codex review P3b-R2-1).
- `tests/test_edge_copy_attributes.py`: NEW (exact + paraphrase in both orders, with and
  without a schema; the exact mention takes the fast path; stale mark from a reused object, on
  a duplicate candidate and on an invalidation-only candidate; bulk episodes in both orders
  through real resolution; last-marked rule; mark never serialized); added to
  `menhir-compatibility.yml` (ruff and pytest; `edges.py` to ruff and pyright).
  `tests/test_edge_copy_reconcile.py`: the two hand-built "recomputed" copies now set the mark
  (setup only, assertions unchanged).
- Assumption (documented at the reset): concurrent bulk episodes do not share one candidate
  object; the drivers here build fresh objects per search.

## 2026-10-08 — Reconcile copies of one stored edge before the save

- `graphiti_core/utils/maintenance/edge_operations.py`: NEW
  `reconcile_edge_copies(resolved_edges, invalidated_edges)`; every copy of a stored edge
  resolved by different mentions takes the earliest `expired_at`, the earliest `invalid_at`,
  the union of `episodes` and the first resolved copy's `attributes`, in place, at the end
  of `resolve_extracted_edges`. Edges sharing a uuid but differing in group, endpoints or
  fact are logged and left alone. Previously the full-replace save let the last copy win, so a
  live restatement could overwrite a supersession found by another mention (reachable with
  `WORLD_END`, and upstream for an open-ended edge invalidated by one mention and restated by
  another).
- `graphiti_core/graphiti.py`: `_resolve_nodes_and_edges_bulk` reconciles the combined
  per-episode results before `add_episode_bulk` saves them.
- `tests/test_edge_copy_reconcile.py`: NEW; added to `menhir-compatibility.yml`.

## 2026-10-08 — Release prep for `v0.30.2.post4`

- `pyproject.toml`, `uv.lock` (project entry only), `.github/workflows/release-graphiti-core.yml` (reviewed
  wheel version check), `README.md`, `CHANGELOG.md`: bump the published version to `0.30.2.post4`, which carries
  #7 (reconcile copies of one stored edge) from `menhir/main` @ `ee1e74b`. Tag `v0.30.2.post4` on the merged
  release-prep commit triggers the PyPI release.
