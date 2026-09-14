# Changelog — graphiti

## 2026-09-14 — Phase C: native prompt JSON serialization (migrates Menhir installer #3)

- `graphiti_core/prompts/prompt_helpers.py`: `to_prompt_json` now implements the prompt-serialization
  boundary natively (Menhir installer #3, `_patch_graphiti_prompt_json`). Before `json.dumps`, the payload
  is recursively copied/normalized: dict entries whose string key ends with `_embedding` are dropped, as
  are entries whose value is a list/tuple of length strictly greater than 64 whose first eight items are
  int/float (bool excluded) — the intentional sampled-head compatibility rule (length 64 is preserved).
  Non-JSON-native values convert via callable `isoformat`, then `iso_format`, then `to_native` (each called
  without arguments), recursing into non-primitive conversion results, with `str` fallback when a
  conversion returns the same object or no conversion applies; exceptions from malformed conversion methods
  propagate. Input is never mutated; `ensure_ascii`/`indent` pass through unchanged. No monkeypatching or
  symbol rebinding — all prompt modules and `search/search_helpers.py` inherit the behavior through the
  existing shared-helper import.
- `tests/test_prompt_json.py`: NEW focused regression tests (baseline JSON, ensure_ascii default/True,
  indent, `_embedding` key removal for non-vector values, structural removal at 65 vs preservation at 64,
  sampled-head boundary, bool/string long-list preservation, nested dict/list/tuple recursion, input
  non-mutation, isoformat/iso_format/to_native fallbacks including recursive conversion,
  self-returning conversion → str, plain unsupported object → str).
- `.agent/architecture.md`: documents the native prompt-serialization boundary and the exact
  >64/first-eight rule; bootstrap-state wording updated to reflect Phase C migrations in progress.
- Scope: only Menhir installer #3 (`_patch_graphiti_prompt_json`) is migrated. Follows C1a (installers
  #5/#7/#8) and C1b (installers #9/#10). The Menhir-side runtime patches are NOT removed yet (Phase F);
  other installers remain un-migrated. Tests/lint/typecheck NOT RUN by the worker (orchestrator owns
  verification).

## 2026-09-14 — Phase C: native response normalization for prompts (migrates Menhir installers #9/#10)

- `graphiti_core/prompts/extract_nodes.py`: `ExtractedEntity` gains a `_normalize_provider_fields` before
  model-validator (Menhir installer #9) — alias recovery only when the canonical `name` key is absent
  (`entity_name`, string `entity`, key typos like `name-`/`name_`/`Name `), degenerate
  `{<entity name>: <int type id>}` payloads, and `entity_type_id` fallbacks in installer precedence
  (`type_id`; integer `type`; present `type_name` → 0, winning over `entity_type`/`entity`; integer
  `entity_type` else 0; remaining `entity` coerced to int else 0; default 0). Missing name still raises.
  `episode_indices` deliberately KEEPS the upstream v0.29.3 default `[0]`, correcting the stale Menhir
  replacement-model default `[]` whose comment wrongly claimed it mirrored upstream.
- `graphiti_core/prompts/dedupe_nodes.py`: `NodeResolutions` gains an `_normalize_entity_resolutions` before
  model-validator (Menhir installer #10) — fresh-list default; missing/null/non-sequence top-level values
  fail safe to `[]`; non-dict and non-integer-id entries dropped (bools never accepted as ids); `name`
  null→`''`; `duplicate_candidate_id` null/non-integer (bools included)→`-1`, integer-coercible→`int`.
- `tests/test_response_model_normalization.py`: NEW focused regression tests for both validators (canonical
  passthrough, all aliases/typos, singleton mapping, type defaults, missing-name failure, upstream
  `episode_indices=[0]` correction, mixed/degenerate rows, fail-safe empties, no shared mutable defaults).
- `.agent/data_models.md`: documents the native response-normalization contract and the deliberate
  `episode_indices=[0]` correction versus the stale Menhir patch.
- Scope: only Menhir installers #9 (`_patch_graphiti_entity_extraction`) and #10
  (`_patch_graphiti_dedupe_resolutions`) are migrated. The Menhir-side runtime patches are NOT removed yet;
  other installers remain un-migrated. Tests/lint/typecheck NOT RUN by the worker (orchestrator owns
  verification).

## 2026-09-14 — Phase C: native None-hardening for models (migrates Menhir installers #5/#7/#8)

- `graphiti_core/nodes.py`: `EntityNode` gains a `coerce_none_summary` before-validator (explicit `summary=None`
  becomes `''`); `EntityNode.generate_name_embedding` and `CommunityNode.generate_name_embedding` harden a `None`
  name to `''` before embedding (Menhir installer #5 semantics, and #7 for the summary coercion).
- `graphiti_core/edges.py`: `EntityEdge` gains a `coerce_none_fields` before model-validator — explicit `None` for
  `uuid`/`episodes` drops the key so default factories run; explicit `None` for `group_id`, `name`, `fact`,
  `source_node_uuid`, `target_node_uuid` coerces to `''` (Menhir installer #8). `generate_embedding` hardens a `None`
  fact to `''` before embedding (Menhir installer #5). `episodes` default changed from `default=[]` to
  `default_factory=list`.
- `tests/test_model_none_hardening.py`: focused regression tests covering all of the above, distinct default
  instances, unchanged non-None values, and embedder inputs.
- `.agent/data_models.md`: documents the native None-hardening contract.
- Scope: only Menhir installers #5 (`_patch_graphiti_none_replace`), #7 (`_patch_graphiti_node_summary_none`), and #8
  (`_patch_graphiti_edge_none_fields`) are migrated. The Menhir-side runtime patches themselves are NOT removed yet;
  other installers remain un-migrated. Tests/lint/typecheck NOT RUN by the worker (orchestrator owns verification).

## 2026-09-14 — Phase 1A review corrections (Codex independent review)

- `.agent/data_models.md`: corrected model inventory against `graphiti_core/nodes.py`/`edges.py` at v0.29.3 — added
  `fact_triple` to `EpisodeType`; fixed `Node`/`Edge` base fields; listed exact fields for `EpisodicNode`, `EntityNode`,
  `CommunityNode`, `EpisodicEdge`, `EntityEdge` (`name` is the relation name, not a `relation` field), `CommunityEdge`.
- `.agent/architecture.md`: replaced the no-monkeypatching implication with current facts — unmodified v0.29.3 baseline
  on this branch, Menhir still applies 17 runtime Graphiti patches, runtime symbol rebinding removal deferred to later
  migration phases. Clarified there is no repository-wide Neo4j password default (server example `password`, MCP/Docker
  commonly `demodemo`).
- `.agent/for-review/graphiti-softfork-phase1a-bootstrap-glm53-20260914.md`: wrapup updated to list review corrections
  and drop the unverified no-invented-fields claim.

## 2026-09-14 — Archolith soft-fork Phase 1A bootstrap documentation

- `README.md`: added "Archolith soft-fork maintenance" section documenting purpose, baseline (upstream `v0.29.3`,
  exact SHA `021d3a57d511f21b10adaf7fa923bd5c1fce5e9d`), branch `menhir/0.29.3`, origin/upstream roles, exact-SHA
  consumer pinning, maintenance procedure, and the four mandatory fork-commit labels. Upstream README content preserved.
- `.agent/README.md`: replaced scaffold with fork-specific read order, role, fork boundary, branch/worktree rule, and
  the structural-ingest deferral note. Upstream `AGENTS.md`/`CLAUDE.md` remain authoritative for source conventions.
- `.agent/architecture.md`: documented Graphiti core modules, data flow, provider seams, fork/upstream topology,
  exact-SHA consumption, no-runtime-monkeypatch goal, and bootstrap-vs-future-migration state.
- `.agent/data_models.md`: documented core `EpisodicNode`/`EntityNode`/`CommunityNode`, `EpisodicEdge`/`EntityEdge`/
  `CommunityEdge`, `EpisodeType`, and canonical definition locations from the v0.29.3 tree.
- `.agent/workflows/code_conventions.md`: documented Python 3.10+, Ruff/Pyright/Pytest conventions, unit vs integration
  commands, telemetry-disabled testing, and fork branch/worktree/no-blind-upstream rules.
- Public fork: https://github.com/Archolith/graphiti. Docs scaffold created by Agent Smith was completed here; remote
  Menhir structural ingest remains deferred (known unavailable capability). No source, tests, CI, or packaging changes;
  no test runs were performed in this session.
