# Changelog — graphiti

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
