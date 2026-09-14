# Wrapup — Graphiti Soft-fork Phase C: Native Response Normalization (Installers #9/#10)

- Agent: OpenCode
- Model: zai-coding-plan/glm-5.3-flash
- Status: READY FOR REVIEW (pending separate closeout commit for this wrapup, to be recorded by Codex)
- Plan/ticket: Phase C (durable plan; Menhir Graphiti soft-fork migration), harness task
  `TASK-graphiti-softfork-phase-c1b-response-normalization-glm53-20260914`
- Worktree/branch: `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
- Commits: `aea78ab` (product commit; message `fix: normalize malformed provider responses natively`, body
  label `Fork-Label: provider compatibility`)

## Summary

Implemented fork-native, source-level replacements for two Menhir runtime Graphiti installers, with no
monkeypatching, wrapper sentinels, or symbol rebinding:

- **#9 `_patch_graphiti_entity_extraction`** → native Pydantic before model-validator
  `_normalize_provider_fields` on the existing `ExtractedEntity` class (`graphiti_core/prompts/extract_nodes.py`).
  Alias recovery runs only when the canonical `name` key is absent (a present-but-invalid canonical `name`
  proceeds to normal Pydantic validation). Handles: name aliases (`entity_name`; string `entity`; first
  string-valued key normalizing to `name` under trim/lower plus trailing `-`/`_`/space tolerance, e.g.
  `name-`, `name_`, `Name `); degenerate single-pair `{<entity name>: <integer type id>}` payloads;
  `entity_type_id` fallbacks in the reviewed installer precedence (`type_id`; integer `type`; present
  `type_name` discards the value and defaults 0, winning over coexisting `entity_type`/`entity`; integer
  `entity_type` else 0; remaining `entity` coerced to int else 0; final default 0). Missing/unrecoverable
  `name` still raises `ValidationError`; canonical valid fields pass through unchanged.
  `episode_indices` deliberately keeps the upstream v0.29.3 default `[0]` — a cross-check correction of the
  stale Menhir replacement model, whose `default_factory=list` (`[]`) carried a comment incorrectly claiming
  it mirrored upstream (accidental patch drift, not copied).
- **#10 `_patch_graphiti_dedupe_resolutions`** → native Pydantic before model-validator
  `_normalize_entity_resolutions` on the existing `NodeResolutions` class (`graphiti_core/prompts/dedupe_nodes.py`).
  `entity_resolutions` gets a fresh-list default; missing or null top-level values yield `[]`; a malformed
  non-sequence top-level value fails safely to `[]` (no iteration TypeError). Non-dict entries and entries
  without an integer-coercible `id` are dropped (bools are never accepted as ids). Retained entries:
  missing/null `name` → `''`; missing/null/non-integer `duplicate_candidate_id` → `-1` (bools included);
  integer-coercible duplicate ids cast to `int`. Conversion goes through the typed `_coerce_int` helper
  (no ignores). Valid entries and order are unchanged; the existing `NodeDuplicate` model still validates
  retained fields.

Phase C is NOT complete overall — only installers #9/#10 are claimed here (C1a already migrated #5/#7/#8).
Menhir-side patches are NOT removed yet.

## Files Changed

- `graphiti_core/prompts/extract_nodes.py` — `_normalize_provider_fields` before model-validator on
  `ExtractedEntity` (+ `_normalized_key` helper, `model_validator` import)
- `graphiti_core/prompts/dedupe_nodes.py` — `_normalize_entity_resolutions` before model-validator on
  `NodeResolutions` (+ `model_validator` import); `entity_resolutions` default `...` → `default_factory=list`
- `tests/test_response_model_normalization.py` — NEW: 25 focused test functions (28 collected cases:
  24 non-parametrized plus the four-way parametrized name-typo test)
- `.agent/data_models.md` — new "Response Normalization Contract (native, fork)" section naming #9/#10 and the
  deliberate `episode_indices=[0]` correction versus the stale Menhir patch
- `.agent/CHANGELOG.md` — dated Phase C entry naming installers #9/#10 as the only migrated ones; explicitly
  notes Menhir-side patches are NOT removed yet
- `.agent/for-review/graphiti-softfork-phase-c1b-response-normalization-glm53-20260914.md` — this wrapup (NEW)

## Verification

Commands/results (all NOT RUN by worker — task contract prohibits worker test/lint/typecheck runs;
orchestrator owns verification):

- `uv run pytest tests/test_response_model_normalization.py` — NOT RUN
- `make lint` / `make test` — NOT RUN

Design checks performed by inspection only:

- Both validators are `mode='before'` model-validators on the existing classes; no symbols replaced.
- Degenerate single-pair check runs first (before `entity_type_id` synthesis) so the singleton shape is not
  mutated into a two-key dict before recognition; singleton keys equal to `name`/`entity_type_id` and non-int
  values are excluded.
- `episode_indices` field definition is untouched — upstream `[0]` default preserved.
- `NodeResolutions` non-dict top-level input is returned as-is so Pydantic's own validation still raises for
  genuinely non-dict payloads; only dict payloads get fail-safe `[]` normalization of the nested field.
- Bool values are rejected outright by the `_coerce_int` helper: bool `id` rows are dropped and bool
  `duplicate_candidate_id` normalizes to `-1`, avoiding the `int` subclass loophole.
- Test payloads for malformed inputs use `model_validate` with `dict[str, Any]`, so Pyright needs no widened
  production annotations or ignores.

## Review Corrections (Codex independent C1b review, applied by worker)

- P1 (bool ids accepted): `_normalize_entity_resolutions` previously entered the conversion branch for bools,
  so `int(True)` produced id `1` and bool duplicate ids became `1`. Fixed via the `_coerce_int` helper that
  returns `None` for bools: bool-id rows are dropped, bool duplicate ids become `-1`. Direct assertions for
  both retained/added in `test_node_resolutions_mixed_rows_normalized`.
- P1 (alias recovery on invalid canonical name): the gate was `not isinstance(d.get('name'), str)`, so
  `{'name': None, 'entity_name': ...}` recovered the alias; the contract requires recovery only when the
  canonical key is absent. Gate changed to `'name' not in d`; added
  `test_extracted_entity_invalid_canonical_name_skips_alias_recovery` proving `{'name': None,
  'entity_name': 'rescuer', 'entity_type_id': 1}` raises `ValidationError`.
- P2 (legacy precedence): `type_name` was ignored entirely, letting `entity_type`/`entity` win when
  coexisting. Precedence now matches the reviewed installer: `type_id`; integer `type`; present
  `type_name` → 0; integer `entity_type` else 0; remaining `entity` int-coercible else 0; default 0. Added
  `test_extracted_entity_type_name_wins_over_entity_type` proving the coexistence case yields 0.
- P1 (Pyright reportArgumentType ×4 at the old dedupe lines 60/68): conversion refactored through the typed
  `_coerce_int(value: Any) -> int | None` helper; no ignores, no production annotation widening.
- P2 (Ruff I001): `ExtractedEntities`/`ExtractedEntity` import order sorted in the test file.
- P3 (wrapup accuracy): corrected the collected-case count (previously claimed 27; orchestrator measured 26
  before corrections; final after corrections is 28 — 25 functions, 24 non-parametrized + 4 parametrized
  cases). Earlier claims not exactly true after correction were removed or reworded in this update.
- Correction reruns of pytest/pyright/ruff were NOT performed by the worker — orchestrator owns verification.

## Independent Codex Acceptance (Codex-run; NOT run by worker)

The C1b product was independently reviewed, corrected, and accepted by Codex. All six findings above were
corrected; no open findings remain. Exact evidence from Codex verification:

- Focused pytest (`tests/test_response_model_normalization.py`): 28 passed, 1 pre-existing Pydantic warning,
  0 failed.
- Ruff `--no-cache` on the two source files plus the focused test file: all checks passed.
- Pyright on the same files: 0 errors, 0 warnings.
- Repository CI-shaped no-external-dependencies pytest: 407 passed, 11 skipped, 3 warnings, 0 failed.
- `git diff --check`: clean.
- Scope: the five product files are contained in product commit `aea78ab` (`fix: normalize malformed provider
  responses natively`, body label `Fork-Label: provider compatibility`); this wrapup remains the sixth phase
  file, to be recorded in a separate closeout commit.

## Claim Cross-Check

- Summary matches actual diff: yes
- Files-changed list matches actual diff: yes (six named files only; five product files in `aea78ab`, this wrapup pending closeout)
- Commit list accurate: yes (`aea78ab`, recorded above)
- Verification entries honest: yes (all worker runs marked NOT RUN; Codex-run results recorded separately)
- No overclaim of Phase C/Menhir completion: yes (only installers #9/#10 claimed migrated; patches not removed;
  no push claimed)

## Completion Checklist

- [x] #9 entity-extraction normalization implemented natively on `ExtractedEntity` (before-validator, no wrappers)
- [x] #10 dedupe-resolutions normalization implemented natively on `NodeResolutions`
- [x] Upstream v0.29.3 `episode_indices=[0]` default retained; stale Menhir `[]` default deliberately not copied
- [x] Tests cover: canonical passthrough, all name aliases/typos, singleton mapping, each type alias/default,
      missing-name failure, default/explicit episode indices, no shared defaults; #10 mixed/degenerate rows,
      dropped rows, field normalization, integer-coercible ids, fail-safe empties, order preservation, fresh defaults
- [x] Docs updated (`data_models.md`, `CHANGELOG.md`) naming exactly #9/#10 as migrated plus the stale-default correction
- [x] Diff scope confirmed: only the six named files
- [x] Harness task file deleted
- [x] No Menhir edits, no worker git actions, no worker test/lint/typecheck runs (orchestrator owns verification)

## Assumptions

- The embedded harness contract is the authoritative behavioral spec for both installers (external Menhir paths
  were not read, per instructions).
- "Integer-coercible" means `int(value)` succeeds (ints, numeric strings); bools are excluded deliberately.
- `ExtractedEntities` and `NodeDuplicate` need no changes; they inherit normalized inner models.

## Risks / Gaps

- The before-validators fire on any dict-validation path into these models (`model_validate`, nested
  construction), which matches the legacy installer's construction-site behavior but is broader than
  strict constructor-only semantics.
- A legit payload consisting of exactly one key like `{'entity_type_id': <non-int>}` falls through to normal
  Pydantic validation (fails as upstream would) — the singleton shortcut intentionally excludes that key.
- Full `uv sync --extra dev --frozen` remains non-reproducible on Windows/Python 3.14 (`kuzu==0.11.3` build
  failure, known from C1a) — environment limitation, not a product failure.

## Follow-Up Tasks

- Later Phase C steps: migrate remaining Menhir installers; finally remove the Menhir-side patch applications
  (Phase F). This change does NOT claim the Menhir patches are removed.
- Orchestrator: run focused tests, lint, and typecheck; then commit with an appropriate fork label.
