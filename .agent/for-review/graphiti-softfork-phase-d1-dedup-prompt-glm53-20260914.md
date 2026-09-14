# Wrapup — Graphiti Soft-fork Phase D1: Native Anti-Conflation Counterexample in Per-Entity Dedup Prompts

- **Agent:** OpenCode
- **Model string:** `zai-coding-plan/glm-5.3-flash` (GLM)
- **Status:** READY FOR ACCEPTANCE — Codex re-review found no further code or test issues; final Codex-owned
  verification run below is all PASS
- **Plan/ticket:** `.harness/TASK-graphiti-softfork-phase-d1-dedup-prompt-glm53-20260914.md` (Phase D1 / installer
  #11; task file deleted after completion)
- **Worktree/branch:** `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
- **Commits:** UNCOMMITTED (worker is prohibited from running git; Codex owns commits)
- **Verification scope:** Worker verification NOT RUN by design — the task prohibits the worker from running tests,
  formatters, typecheckers, git, network, or MCP. Verification below is inspection-only evidence plus an explicit
  NOT RUN disclosure for every execution claim.

## Summary

Implemented the fork-native half of Menhir installer #11 (`_patch_graphiti_dedup_prompt`): both active per-entity
dedup prompts in `graphiti_core/prompts/dedupe_nodes.py` — `node` (single extracted entity) and `nodes` (batch) —
now render Menhir's anti-conflation counterexample exactly once in their `<EXAMPLE>` blocks. The counterexample is
NEW ENTITY `"the suburbs"` against an existing `Chicago` (Location, summary "A city where someone lives"),
resolving to `duplicate_candidate_id = -1`, with the explanation that relative/descriptive locations (the suburbs,
downtown, countryside) are never the same object as a specific named city merely because they are related or
appear in movement context. The policy is implemented directly in the existing prompt functions and the
`versions` map (which already points at those functions): no wrappers, no runtime rebinding. Rendered JSON braces
use the surrounding native `{{...}}` f-string style; the escaped-brace artifacts of the Menhir runtime patch were
deliberately not carried over. `node_list` (the separate UUID-grouping prompt) is outside installer #11 and was
not modified. No identity-gate logic (#12), telemetry, adaptive dedupe, structural candidate filtering, or other
Menhir policy was added; C1a–C1g behavior is untouched.

## Files Changed

- `graphiti_core/prompts/dedupe_nodes.py` — the `node` and `nodes` `<EXAMPLE>` blocks each gain one counterexample
  (the `the suburbs` vs `Chicago` case with `duplicate_candidate_id = -1` and the
  relative/descriptive-location explanation), placed after the "Marco's car" synonym example. No other lines
  changed: existing examples, system messages, task instructions, response contract, and `node_list` are byte-
  identical to the pre-D1 state; `versions` is unchanged and still maps directly to the three functions.
- `tests/test_dedupe_nodes_prompt.py` — NEW, 10 focused tests (no DB): both direct functions and the
  `versions['node']`/`versions['nodes']` entries render the counterexample marker and result line exactly once,
  each asserted against its correct native label (`NEW ENTITY: "the suburbs"` for `node`, `ENTITY: "the suburbs"`
  for `nodes`); a dedicated native-braces test proves the rendered Chicago example line uses single JSON braces
  and no `{{`/`}}` escaped-brace artifact appears in either rendered prompt; the `versions` map keeps its original
  shape and function identity (`is` checks against `node`/`nodes`/`node_list`); `node_list` output contains no
  leakage of the new example (`the suburbs`, `Chicago`, both markers, and the result line all absent); existing
  examples and response-contract phrasing remain intact in both prompts; and none of the three prompt functions
  mutates its context argument (deepcopy comparison).
- `.agent/architecture.md` — NEW section "Dedup Prompt Anti-Conflation Policy (fork half of installer #11)"
  between "Structured Summary Policy" and "Single-Episode Extraction Routing", scoping the policy to `node`/`nodes`
  (+ `versions` entries), noting exactly-once rendering, native brace style (no patch artifacts), the
  `node_list` exclusion, and that identity-gate logic remains installer #12.
- `.agent/CHANGELOG.md` — NEW dated Phase D1 entry at the top describing the prompt change, the new test suite,
  the documentation update, the Phase F remainder (Menhir-side runtime-patch removal), and honest
  verification-not-run disclosure.
- `.harness/TASK-graphiti-softfork-phase-d1-dedup-prompt-glm53-20260914.md` — deleted at end of task (as
  instructed).
- Template limitation: the canonical wrapup template at `C:\Users\thron\IdeaProjects\.agent\WRAPUP-TEMPLATE.md`
  could not be read (read permission denied in this session); this wrapup follows the format of the accepted
  Phase C1g wrapup in the same directory instead. The worker could not run `artifact_validate` (MCP prohibited);
  Codex's local Menhir artifact validation (local only, not remote ingest) ran in the final acceptance run —
  PASS, 9 records, 0 findings.

## Verification

Worker-run execution verification: NOT RUN (prohibited). Inspection-only evidence, gathered by reading the
current files:

- PASS (inspection): `dedupe_nodes.py` contains the counterexample exactly once in `node` (lines ~153–155) and
  exactly once in `nodes` (lines ~222–224); `versions` still maps `node`/`node_list`/`nodes` to the module
  functions with no rebinding or wrappers.
- PASS (inspection): `node_list` body is unchanged and contains no `the suburbs`/`Chicago` text.
- PASS (inspection): existing examples (Sam, NYC, Java, Marco's car) and all response-contract phrasing are
  present and unmodified in both prompts; `NodeDuplicate`/`NodeResolutions` models untouched.
- PASS (inspection): rendered braces use the file's native `{{...}}` f-string escaping, consistent with the
  surrounding examples; no `{{{{`-style patch artifacts exist in the file.
- PASS (inspection): `.agent/architecture.md` and `.agent/CHANGELOG.md` contain the new D1 sections; the
  changelog entry accurately scopes the work as the fork half of installer #11 with Phase F remaining.

Execution verification — worker: NOT RUN (prohibited). All execution results below are Codex's independent
runs, recorded honestly: the initial pre-correction run (with failures) and the final post-correction acceptance
run (all PASS).

Codex initial verification run (pre-correction, 2026-09-14):

- Focused pytest `tests/test_dedupe_nodes_prompt.py` — FAIL: `2 failed, 7 passed, 1 warning`
- Ruff check — PASS
- Ruff format `--check` — FAIL: one file would be reformatted (`tests/test_dedupe_nodes_prompt.py`)
- Pyright — PASS: 0 errors / 0 warnings / 0 info
- Git, network, MCP, `artifact_validate` — NOT RUN by worker (prohibited / unavailable)

Final Codex acceptance run (post-round-1 corrections, 2026-09-14) — all PASS:

- Focused pytest `tests/test_dedupe_nodes_prompt.py` — PASS: `10 passed, 1 warning in 0.03s`
- Focused Ruff check — PASS: all checks passed
- Focused Ruff format `--check` — PASS: `2 files already formatted`
- Changed-file Pyright — PASS: 0 errors / 0 warnings / 0 informations
- Repository-wide Ruff check — PASS: all checks passed
- Cumulative no-external-database pytest — PASS: `510 passed, 11 skipped, 3 warnings in 19.25s`
- Local Menhir artifact validation (local only; not remote ingest) — PASS: checked 9 records, 0 findings
- `git diff --check` — PASS: exit 0 (informational LF→CRLF notices for architecture/changelog only)

## Review Corrections (Codex independent review round 1, 2026-09-14)

1. **P1 test fix** — the shared `ANTI_CONFLATION_MARKER` was `NEW ENTITY: "the suburbs"`, but the native batch
   `nodes` prompt labels its example `ENTITY: "the suburbs"`, so two focused tests failed. Fixed in the tests
   only (the prompt was NOT changed to fit the marker): separate `ANTI_CONFLATION_NODE_MARKER`
   (`NEW ENTITY: "the suburbs"`) and `ANTI_CONFLATION_NODES_MARKER` (`ENTITY: "the suburbs"`) constants, each
   exactly-once assertion now uses its prompt's correct native label across both direct and `versions` paths.
2. **P2 test count** — CHANGELOG and wrapup claimed 10 tests while the file had 9. Added a meaningful tenth
   scoped test, `test_rendered_json_braces_are_native_single_braces`, closing the acceptance-gap the task
   called out: it asserts the rendered Chicago `EXISTING ENTITIES` line uses normal single JSON braces and that
   no `{{`/`}}` escaped-brace patch artifact appears in either rendered prompt. All counts remain 10 and are
   now exact.
3. **P2 format** — the over-length response-contract assertion in
   `test_node_prompt_preserves_existing_examples_and_response_contract` was hand-wrapped to Ruff format style
   (parenthesized assertion); the whole file was re-inspected for >100-character lines (none remain). Formatter
   itself NOT RUN (prohibited); Codex to confirm with `ruff format --check`.

## Claim Cross-Check

- Files-changed list matches actual edits: yes (five items above; verified by reading current file contents)
- Commits listed (or UNCOMMITTED): yes — UNCOMMITTED; worker is git-prohibited
- Verification lines honest: yes — worker execution NOT RUN throughout; Codex's initial failures preserved as
  FAIL; final PASS results attributed to Codex's post-correction acceptance run
- Both active per-entity prompts render the counterexample exactly once: yes (inspection of lines 153–155 and
  222–224; `versions` entries point at the same functions, so they inherit the behavior)
- `node_list` unchanged and no leakage: yes (function body unedited; verified textually)
- Existing examples and response contracts intact: yes (no lines outside the two `<EXAMPLE>` insertions changed)
- Native brace style, no patch artifacts: yes (file uses `{{...}}` consistently; no quadruple braces present)
- No wrappers / runtime rebinding: yes (policy lives inside the two functions; `versions` unchanged)
- No #12 identity-gate, telemetry, adaptive dedupe, or structural candidate filtering added: yes
- C1a–C1g behavior altered: no (no files from prior phases touched)

## Completion Checklist

- [x] `node` prompt renders the anti-conflation counterexample exactly once
- [x] `nodes` prompt renders the anti-conflation counterexample exactly once
- [x] `versions['node']`/`versions['nodes']` render it exactly once (direct function mapping, verified)
- [x] `node_list` unchanged, no leakage
- [x] Existing prompt examples and response contracts intact
- [x] Rendered JSON braces consistent with native examples; no escaped-brace patch artifacts
- [x] Focused test file covers direct functions + versions entries against each prompt's correct native label,
      exact-once, no context mutation, native single-brace rendering, and `node_list` non-leakage
- [x] `architecture.md` + `CHANGELOG.md` accurately scoped as fork-native installer #11
- [x] Wrapup written; harness task file deleted
- [x] Tests / lint / typecheck — initial Codex run had 2 test failures and 1 format finding (preserved above);
      corrected by worker; final Codex acceptance run all PASS (focused pytest `10 passed`, Ruff check/format
      PASS, Pyright 0/0/0, cumulative suite `510 passed, 11 skipped`, artifact validation `9 records, 0 findings`)

## Assumptions

- "Both active per-entity dedup prompts" means the `node` and `nodes` functions plus their `versions` entries;
  `node_list` is the separate UUID-grouping prompt and is outside installer #11, per the task's verified
  current-code evidence.
- The counterexample text follows the task's prescribed wording; the em dash inside the explanation was rendered
  as "such as ... are never" phrasing matching the task text (a comma-style list), and the em-dash synonym
  example above it was left untouched.
- Phase F (Menhir-side removal of `_patch_graphiti_dedup_prompt`) is out of scope, consistent with prior phases.

## Risks / Gaps

- Closed: the round-1 corrections (marker split, tenth test, hand formatting) were rerun by Codex — focused
  pytest `10 passed, 1 warning`, Ruff check/format PASS, Pyright 0/0/0 (see final acceptance run).
- Closed: Ruff format equivalence confirmed by Codex's `2 files already formatted` result.
- The exactly-once assertions count string occurrences in rendered prompt content; the nodes marker
  (`ENTITY: "the suburbs"`) is a substring of the node marker, so the node prompt is only ever asserted with the
  `NEW ENTITY:` marker and vice versa — intentional, since each label is that prompt's native form.

## Follow-Up Tasks

- Codex: accept and create the Phase D1 product commit (worker is git-prohibited; all work is UNCOMMITTED).
- Phase F: remove the Menhir-side runtime patch `_patch_graphiti_dedup_prompt` (installer #11) once the fork
  native policy is accepted; installer #12 (identity gate) remains a separate phase.
