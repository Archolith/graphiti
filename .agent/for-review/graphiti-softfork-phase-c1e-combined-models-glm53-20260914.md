# Wrapup — Graphiti Soft-fork Phase C1e: Native Combined-Extraction Model Sanitization

- Agent: opencode
- Model: zai-coding-plan/glm-5.3-flash
- Date: 2026-09-14
- Status: READY FOR REVIEW — C1e independently accepted by Codex. Product commit landed; this
  wrapup pending its closeout commit. Worker verification remains NOT RUN throughout.
- Plan/Ticket: Phase C1e — migrate the generic half of Menhir installer #2 (`_patch_graphiti_combined_extraction_models`) into fork source
- Worktree/Branch: `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
- Commits: product commit `a5c11c2d8c6f748043ebdf662380381e3b1c23b5` — "feat: harden combined
  extraction models" (contains the four product files); this wrapup is the fifth phase file,
  pending a separate closeout commit. Worker was prohibited from running git; Codex committed.
- Baseline: prior phase wrapups present in `.agent/for-review/`; no baseline SHA recorded (git prohibited)

## Summary

Implemented the generic, fork-native half of Menhir installer #2 as a Pydantic `mode="before"`
validator (`sanitize_malformed_rows`) on `CombinedExtraction` in
`graphiti_core/prompts/extract_nodes_and_edges.py`. The validator returns non-dict top-level
payloads unchanged (normal Pydantic validation applies, so malformed top-level input still raises
`ValidationError`); dict payloads are copied, never mutated. Missing, null, or non-list
`extracted_entities`/`edges` arrays become `[]` in the validated model while both fields stay
declared `Field(...)` required with their existing descriptions, so `model_json_schema()` still
marks both arrays required. Entity normalization drops non-dict rows; resolves the name from the
valid nonblank canonical `name`, else the first valid nonblank alias in `entity_name` then
`entity` (trimmed); drops rows with no recoverable name; coerces `entity_type_id` via `int()`
with `-1` fallback on `TypeError`/`ValueError` (int(True)==1 parity intentionally preserved); and
retains only `name` and `entity_type_id`. Edge normalization drops non-dict rows and rows where
`source_entity_name`, `target_entity_name`, `relation_type`, or `fact` is not a nonblank string;
retains those strings exactly (not stripped); filters list `episode_indices` to non-bool ints
using `[0]` when none survive; defaults non-list/missing `episode_indices` to `[0]`; and retains
only the five fields. Row order is preserved. No Menhir receipt, hook, endpoint-closure,
self-binding, marker, grounding, suppression, titled-list, audit-counter, or policy behavior was
implemented or imported; this phase is exclusively the generic half of installer #2, and the
Menhir-specific half remains for Phase F.

## Files Changed

- `graphiti_core/prompts/extract_nodes_and_edges.py` — added `model_validator` import and the
  `sanitize_malformed_rows` before-validator on `CombinedExtraction`; fields, descriptions, and
  everything else unchanged.
- `tests/test_combined_extraction_models.py` — NEW focused regression suite (20 tests after review
  corrections; see Review Corrections).
- `.agent/data_models.md` — new `CombinedExtraction` bullet in the "Response Normalization
  Contract (native, fork)" section.
- `.agent/CHANGELOG.md` — dated Phase C1e entry at the top, labeled exactly `provider
  compatibility`, explicitly scoping to the generic half of installer #2 with Phase F pending.
- `.agent/for-review/graphiti-softfork-phase-c1e-combined-models-glm53-20260914.md` — this wrapup.
- `.harness/TASK-graphiti-softfork-phase-c1e-combined-models-glm53-20260914.md` — deleted per
  harness instruction (untracked harness input file, not a product change).

## Verification

All worker-run commands were PROHIBITED by the task contract; all verification and git below were
performed independently by Codex and recorded here verbatim. Worker statements remain honestly
NOT RUN.

Codex final acceptance evidence (post-correction):

- Focused pytest `tests/test_combined_extraction_models.py`: 20 passed, 1 pre-existing warning
- Focused Ruff check: PASS; Ruff format --check: PASS, 2 files already formatted
- Focused Pyright on changed source + test: 0 errors, 0 warnings, 0 information
- CI-shaped no-external-database suite: 462 passed, 11 skipped, 3 warnings; exit 0
  (Neo4j/FalkorDB/Kuzu/Neptune disabled; no remote/prod access)
- Repository-wide Ruff check: PASS
- Artifact validation: 6 Graphiti records, no findings
- `git diff --check`: clean apart from informational LF->CRLF warnings on docs
- Full-repository Pyright was also attempted but is explicitly non-gating; it failed with 232
  pre-existing optional-backend/example/test errors because this Windows selective-dependency
  environment lacks optional packages. These are NOT C1e findings; the changed-file Pyright gate
  is clean.

Worker-run commands (all prohibited by the task contract, NOT RUN by the worker):

- Targeted pytest on `tests/test_combined_extraction_models.py`: NOT RUN by worker
- `make test` / CI-shaped unit gate: NOT RUN by worker
- `make lint` (ruff + pyright) / `make format`: NOT RUN by worker
- `artifact_validate(artifact_type="wrapups", ...)`: NOT RUN by worker (MCP prohibited)
- git commands (status/diff/commit): NOT RUN by worker (prohibited)
- Scope inspection (read-only; only the five named files edited plus harness task deletion): PASS

Test coverage written (worker runs NOT RUN): schema `required` list and field descriptions
unchanged; missing/null/non-list arrays sanitize to `[]`; non-dict top-level still raises
`ValidationError`; entity canonical-over-alias precedence, `entity_name`-then-`entity` fallback,
trimming, `int()` coercion with `-1` fallback including `True`→1 parity, no-name row drops,
extra-field drops; edge required-field dropping (blank/non-str/None), exact preservation of
retained strings (padded/tab-padded values verbatim), `episode_indices` filtering (bools,
strings, floats excluded; `[0]` when empty), non-list/missing default `[0]`, extra-field drops;
input dict non-mutation; mixed valid/malformed row order preservation; behavioral proof that an
edge referencing an entity absent from `extracted_entities` leaves the entity list unchanged (no
endpoint synthesis) while the edge stays model-valid, plus a pathlib source check that the
module contains no Menhir import/reference (no `_patch_graphiti`, no `Menhir`/`menhir` text).

## Claim Cross-Check

- Validator implemented as Pydantic `mode="before"` on `CombinedExtraction` only: yes
- `extracted_entities`/`edges` still declared required with unchanged descriptions; JSON schema
  still marks both required: yes
- Non-dict top-level returned unchanged; dict payloads copied, never mutated: yes
- Entity normalization matches the contracted generic rules exactly (precedence, trim, int
  coercion/-1 fallback, bool parity, drops, extras removed): yes
- Edge normalization matches the contracted generic rules exactly (required-string drops, exact
  string preservation, index filtering/`[0]` default, extras removed): yes
- Row order and valid-row behavior preserved: yes
- No Menhir receipt/hook/endpoint-closure/self-binding/marker/grounding/suppression/
  titled-list/audit-counter/policy behavior implemented or imported: yes
- Docs updated with the exact `provider compatibility` label and Phase F scoping: yes
- Tests/git/artifact validation executed by worker: no — prohibited by task contract; marked NOT RUN
- Only the named files touched (plus harness task file deletion): yes

## Completion Checklist

- [x] Native before-validator in `extract_nodes_and_edges.py` implementing the generic half of
      installer #2; no other behavior added
- [x] Focused tests in narrowly named new file `tests/test_combined_extraction_models.py`
- [x] `.agent/data_models.md` updated
- [x] `.agent/CHANGELOG.md` entry labeled exactly `provider compatibility`, generic-half-only
      scope with Phase F disclosure
- [x] Wrapup with honest NOT RUN worker verification and explicit commit provenance (Codex-owned)
- [x] Harness task file deleted (no harness task file present)
- [x] Focused pytest, Ruff check/format, changed-file Pyright, CI-shaped suite, repo-wide Ruff
      check, artifact validation, and `git diff --check` — all run by Codex (see Verification)
- [x] Product commit `a5c11c2d8c6f748043ebdf662380381e3b1c23b5` landed by Codex
- [ ] Worker test/lint/typecheck runs (worker-prohibited by task contract; NOT RUN, by design)
- [ ] Separate closeout commit for this wrapup (Codex-owned)

## Review Corrections

Phase C1e was independently reviewed by Codex across two review rounds; all five findings were
fixed (2x P1, 2x P2, 1x P3) with no open phase findings. All worker commands remain NOT RUN.
Honest pre-correction Codex evidence, as reported by the reviewer: first review focused run
18 passed / 1 failed; post-fix verification was subsequently completed by Codex (see
Verification).

- P1 test defect — `test_edge_episode_indices_filtering_and_default` claimed its third edge
  omitted `episode_indices`, but `{**VALID_EDGE}` inherited the fixture's `[1]` (actual `[1]` vs
  expected `[0]`; the cause of Codex's 1 failure). The edge is now built as a copy with the key
  removed (fixture not mutated), keeping expected `[0]`.
- P2 brittle architecture test — the original source scan banned broad tokens (endpoint, hook,
  receipt, ...) across the whole module via bare `open()`, which could fail on unrelated prompt
  prose and proved nothing behaviorally. Replaced with narrow evidence: a behavioral test
  asserting an edge whose target is absent from `extracted_entities` causes no entity synthesis
  (edge remains model-valid), plus a `pathlib` source check limited to asserting no Menhir
  import/reference (`Menhir`, `menhir`, `_patch_graphiti`) in the module.
- P3 reporting drift — the original wrapup claimed "17 tests" while the file collected 19 before
  correction (and 20 after replacing the source-scan test with two narrower tests). All count
  claims corrected to the verified static count of 20.
- P1 Pyright (second review) — `extract_nodes_and_edges.py:106` reported twice that
  `entity.get('entity_type_id')` is `Unknown|None` and cannot be passed to `int()`. Fixed by
  binding the raw value to an explicit `Any` boundary (`raw_type_id: Any = ...`) before the
  `int()` call; runtime semantics and the `except (TypeError, ValueError)` handling are unchanged.
- P2 Ruff format (second review) — Ruff format would collapse the two multiline
  schema-description asserts in `test_schema_marks_both_arrays_required_with_descriptions`. The
  asserts are now standard single-line form; behavior unchanged.

Current Codex evidence: focused pytest 20 passed / 1 pre-existing warning; Ruff check PASS;
Ruff format --check PASS (2 files already formatted); changed-file Pyright 0 errors / 0 warnings /
0 information; CI-shaped suite 462 passed / 11 skipped / 3 warnings, exit 0. All five findings
corrected; post-fix reruns complete — no pending Codex verification remains for this phase.

## Assumptions

- The task contract's inline specification of the generic Menhir normalization rules is
  authoritative; Menhir sources outside this worktree were not read (no Menhir repo access
  attempted).
- The task instruction "Mark READY FOR REVIEW only when edits are complete" overrides the generic
  guidance to use PARTIAL for uncommitted worker work; worker work was UNCOMMITTED when marked,
  and the product commit was subsequently landed by Codex with this wrapup pending its closeout
  commit, matching prior C1-phase wrapup conventions.
- "Nonblank string" means a string whose `.strip()` is non-empty; the retained value is
  preserved exactly without stripping (entities' retained names ARE stripped, per contract).

## Risks / Gaps

- Full-repository Pyright fails with 232 pre-existing optional-backend/example/test errors in this
  Windows selective-dependency environment (optional packages not installed); this is explicitly
  non-gating and NOT a C1e finding — the changed-file Pyright gate is clean.
- The validator intentionally diverges in behavior from strict upstream validation (malformed rows
  silently dropped instead of raising); this is the contracted provider-compatibility behavior and
  is disclosed in the changelog and data models doc.
- The Menhir-reference source check reads the module file at test time; if the module moves or
  packaging strips source, that one test could need adjustment.

## Acceptance / Closeout

C1e is independently accepted by Codex with all five review findings corrected and no open
findings.

- Product commit: `a5c11c2d8c6f748043ebdf662380381e3b1c23b5` — "feat: harden combined extraction
  models". Contains exactly the four product files (`graphiti_core/prompts/
  extract_nodes_and_edges.py`, `tests/test_combined_extraction_models.py`, `.agent/data_models.md`,
  `.agent/CHANGELOG.md`); this wrapup is the fifth phase file, pending a separate closeout commit.
- Final Codex acceptance evidence: focused suite 20 passed / 1 pre-existing warning; focused Ruff
  check PASS and Ruff format --check PASS (2 files already formatted); changed-file Pyright
  0 errors / 0 warnings / 0 information; CI-shaped no-external-database suite 462 passed / 11
  skipped / 3 warnings, exit 0; repository-wide Ruff check PASS; artifact validation 6 Graphiti
  records, no findings; `git diff --check` clean apart from informational LF->CRLF warnings on
  docs.
- Full-repository Pyright (232 pre-existing optional-backend/example/test errors) is explicitly
  non-gating and not attributable to C1e.
- Worker-run commands remain NOT RUN (prohibited by task contract throughout); worker
  verification statements are unchanged and honest.
- No push was performed and no Menhir patch-removal is claimed; the Menhir-specific policy half of
  installer #2 and all runtime-patch removals remain for Phase F.

## Follow-Up Tasks

- Codex: land the separate closeout commit for this wrapup.
- Phase C continuation: remaining Menhir installers per inventory.
- Phase F: remove Menhir-side runtime patches (including the Menhir-specific half of installer #2)
  once all installers are migrated.
