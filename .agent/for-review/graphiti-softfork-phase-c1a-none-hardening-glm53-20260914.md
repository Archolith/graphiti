# Wrapup — Graphiti Soft-fork Phase C: Native None-Hardening (Installers #5/#7/#8)

- Agent: OpenCode
- Model: zai-coding-plan/glm-5.3-flash
- Status: READY FOR REVIEW
- Plan/ticket: Phase C (durable plan; Menhir Graphiti soft-fork migration), harness task
  `TASK-graphiti-softfork-phase-c1a-none-hardening-glm53-20260914`
- Worktree/branch: `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
- Commits: `f850cd76a01b308035b371298406fe8c17655e30` (label: `Fork-Label: provider compatibility`)

## Summary

Implemented fork-native, source-level replacements for three Menhir runtime Graphiti installers, with no
monkeypatching, wrapper sentinels, or symbol rebinding:

- **#5 `_patch_graphiti_none_replace`** → direct method hardening: `EntityEdge.generate_embedding` coerces
  `fact is None` to `''` before the embedder call; `EntityNode.generate_name_embedding` and
  `CommunityNode.generate_name_embedding` do the same for `name`. Observable behavior matches the legacy wrapper
  (field ends as `''`; embedder receives the empty string; newline replacement cannot crash).
- **#7 `_patch_graphiti_node_summary_none`** → native Pydantic before-validator `coerce_none_summary` on
  `EntityNode.summary`: explicit `summary=None` becomes `''`; omission keeps the `str` default factory; real strings
  unchanged.
- **#8 `_patch_graphiti_edge_none_fields`** → native Pydantic before model-validator `coerce_none_fields` on
  `EntityEdge`: explicit `None` for `uuid`/`episodes` drops the key so default factories run; explicit `None` for
  `group_id`, `name`, `fact`, `source_node_uuid`, `target_node_uuid` coerces to `''`; missing/non-None values keep
  upstream behavior. `episodes` default changed from `default=[]` to `default_factory=list` (no shared mutable
  default).

## Files Changed

- `graphiti_core/nodes.py` — `coerce_none_summary` validator (EntityNode); None-name hardening in
  `EntityNode.generate_name_embedding` and `CommunityNode.generate_name_embedding`
- `graphiti_core/edges.py` — `coerce_none_fields` model validator and None-fact hardening in
  `EntityEdge.generate_embedding`; `episodes` → `default_factory=list`; added `model_validator` import
- `tests/test_model_none_hardening.py` — NEW: 13 focused tests (type-correct; degenerate payloads via `model_validate`)
- `.agent/data_models.md` — new "None-Hardening Contract (native, fork)" section naming the three migrated contracts
- `.agent/CHANGELOG.md` — dated Phase C entry naming installers #5/#7/#8 as the only migrated ones; explicitly notes
  Menhir-side patches are NOT removed yet
- `.agent/for-review/graphiti-softfork-phase-c1a-none-hardening-glm53-20260914.md` — this wrapup (NEW)

## Verification

Commands/results (all NOT RUN by worker — task contract prohibits worker test/lint/typecheck runs; orchestrator owns
verification):

- `uv run pytest tests/test_model_none_hardening.py` — NOT RUN
- `make lint` / `make test` — NOT RUN

Design checks performed by inspection only:

- Pydantic v2 `mode='before'` validators run at construction; `Edge` has no `validate_assignment`, so the
  `EntityEdge` before model-validator does not fire on attribute assignment (upstream behavior preserved).
- `Node` has `validate_assignment=True`; assigning `self.name = ''` in the embedding methods re-validates `name`
  (plain `str`, no-op) and then runs `labels`/summary validators only as applicable — no behavioral change.
- For `uuid`/`episodes`, the validator pops only present-but-None keys; absent keys are untouched, so required-field
  `ValidationError` behavior for missing fields is preserved (`field in data` guard on the str-coercion loop).
- Other `Edge` subclasses (`EpisodicEdge`, `CommunityEdge`, `HasEpisodeEdge`, `NextEpisodeEdge`) are intentionally
  NOT hardened — the legacy installer #8 targeted `EntityEdge` only.

## Review Corrections (Codex independent C1a verification)

- P2 (Pyright gate failed on the test file): fixed in `tests/test_model_none_hardening.py` only. Degenerate
  explicit-None payloads now go through `EntityEdge.model_validate` / `EntityNode.model_validate` with
  `dict[str, Any]` inputs instead of typed-constructor `None` kwargs; `_StubEmbedder` is now a real `EmbedderClient`
  subclass implementing the exact abstract `create` signature. No file-level ignores; no production annotation
  changes. Pyright rerun NOT performed by the worker — Codex owns the rerun.
- P3 (missing negative compatibility proof): added `test_edge_missing_required_field_still_raises` proving an
  actually omitted required `EntityEdge` str field still raises `pydantic.ValidationError` (None tolerated, absence
  required).
- P3 (test-count mismatch): the wrapup previously claimed 14 tests; actual count before this correction was 12, and
  the final count after adding the missing-required regression is 13. All counts in this wrapup now say 13.
- Production implementation (`graphiti_core/nodes.py`, `graphiti_core/edges.py`) and docs were NOT altered in the
  first correction pass; only the test file and this wrapup changed.
- P3 (EOF whitespace, final review pass): `git diff --check` reported an extra blank line at EOF in
  `.agent/data_models.md`; removed by the worker and confirmed clean in the final Codex verification pass.
- P2 follow-up (final stub capture-type mismatch, corrected after the first review pass): Pyright flagged
  `_StubEmbedder.received: list[list[str]]` because the abstract `create` input union cannot be appended to it. The
  capture type is now the honest `list[object]` (each appended value is still checked to be `list[str]` at runtime
  before appending); value assertions and test count (13) are unchanged. No blanket ignores; no production changes.
  Pyright rerun NOT performed by the worker — confirmed clean by Codex.

## Independent Verification Results (Codex-run; NOT run by worker)

The following were executed by Codex during independent C1a verification; the worker itself did not run them:

- Focused suite (`uv run pytest tests/test_model_none_hardening.py`): 13 passed, with 1 pre-existing Pydantic
  deprecation warning.
- Ruff on `graphiti_core/nodes.py`, `graphiti_core/edges.py`, and `tests/test_model_none_hardening.py`: all checks
  passed.
- Pyright on those three files: 0 errors, 0 warnings.
- Documented no-database unit gate (CI disable flags `DISABLE_NEO4J=1 DISABLE_FALKORDB=1 DISABLE_KUZU=1
  DISABLE_NEPTUNE=1` plus the CI `--ignore` list): 379 passed, 11 skipped, 3 environment/deprecation warnings.
- `uv sync --extra dev --frozen` could not complete on this machine: optional dependency `kuzu==0.11.3` fails to
  build on Windows/Python 3.14. Codex installed only the declared test/provider clients needed to run the no-DB
  suite. This is an environment limitation, not a product failure.

## Independent Acceptance

C1a was independently accepted by Codex with no open findings. All of the following passed as already recorded above
(see Verification and Independent Verification Results): final `git diff --check`, the 13 focused tests, Ruff,
Pyright (0 errors / 0 warnings), and the no-database unit gate (379 passed, 11 skipped), plus wrapup artifact
validation (2 checked, 0 findings). The only remaining action is this wrapup metadata adjustment itself, which is
left for a separate orchestrator closeout commit.

## Claim Cross-Check

- Summary matches actual diff: yes
- Files-changed list matches actual diff: yes (six named files only; confirmed via `git status`/`git diff` inspection)
- Commit list accurate: yes (`f850cd76a01b308035b371298406fe8c17655e30`, `Fork-Label: provider compatibility`)
- Verification entries honest: yes (worker runs marked NOT RUN; Codex-run results recorded separately)
- No overclaim of Phase C/Menhir completion: yes (only installers #5/#7/#8 claimed migrated)

## Completion Checklist

- [x] #5 embedding safety implemented natively (method hardening, no wrappers)
- [x] #7 EntityNode summary=None → '' natively; omission/default/real-string semantics preserved
- [x] #8 EntityEdge uuid/episodes None-drop and five str-field None→'' natively
- [x] Tests cover: all required fields, default factories (non-empty generated UUID, fresh list), embedder receiving
      empty-string equivalent, normal values unchanged, no shared mutable default
- [x] Docs updated (`data_models.md`, `CHANGELOG.md`) naming exactly #5/#7/#8 as migrated
- [x] Diff scope confirmed: only the six named files
- [x] Harness task file deleted
- [x] No Menhir edits, no worker git actions, no worker test/lint runs (orchestrator committed C1a)

## Assumptions

- The authoritative behavior contract in the harness task (restated after an authorized absolute-path read to the
  Menhir evidence files was denied) matches the legacy installer semantics; the previously retrieved Menhir test
  files (node-summary and edge-none regression tests, CF-128 idempotency test) were used as the behavioral spec.
- `EntityEdge.episodes` `default=[]` → `default_factory=list` is a safe internal cleanup consistent with the
  "no shared mutable default" test requirement; Pydantic deep-copies defaults so upstream observable behavior is
  unchanged either way.

## Risks / Gaps

- `coerce_none_fields` fires for any dict-validation path into `EntityEdge` (e.g. `model_validate`); this matches the
  legacy patch's construction-site behavior but is broader than strict constructor-only semantics.
- CommunityNode/EpisodicNode `summary=None` (non-EntityNode) is not hardened — out of the legacy installer scope.
- Full `uv sync --extra dev --frozen` is not reproducible on Windows/Python 3.14 (`kuzu==0.11.3` build failure) —
  an environment limitation, not a product failure.

## Follow-Up Tasks

- Later Phase C steps: migrate remaining Menhir installers; finally remove the Menhir-side patch applications (this
  change does NOT claim the Menhir patches are removed).
- Separate orchestrator closeout commit for this wrapup metadata adjustment.
