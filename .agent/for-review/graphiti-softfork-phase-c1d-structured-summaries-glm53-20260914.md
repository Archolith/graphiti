# Wrapup — Graphiti Soft-fork Phase C1d: Native Structured Summary Prompts

- Agent: opencode
- Model: zai-coding-plan/glm-5.3-flash
- Date: 2026-09-14
- Status: READY FOR REVIEW (pending separate closeout commit of this wrapup; worker verification NOT RUN)
- Plan/Ticket: Phase C1d — migrate Menhir installer #4 (`_patch_graphiti_summarize`) into fork source
- Worktree/Branch: `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
- Commits: product commit `7de1792beec1fb603aa9f189116225b9f974cb1e` — "feat: make structured summaries
  native" (Fork-Label: Menhir policy divergence); this wrapup is the fifth phase file, pending a separate
  closeout commit
- Baseline: accepted C1c closeout `0f9a8a6` (branch clean at start of session)

## Summary

Replaced the Menhir runtime structured-summary patch behavior (installer #4) with native, source-level
implementations in `graphiti_core/prompts/summarize_nodes.py`. `summarize_context` and `summarize_pair`
now each return exactly two messages (system + user) whose system strings request structured `key:value`
output only, and whose user prompts specify the pipe-separated `key:value` format with a 150-character
total ceiling. `summarize_context` restricts the model to the provided MESSAGES, focuses on what the
entity is, its role/status, and key attributes, requires omitting filler words, and includes a GOOD/BAD
key:value-vs-prose example; `summarize_pair` merges two summaries keeping the most current/specific values
and dropping duplicates. Context values interpolate via raw f-string `context.get(..., '')` defaults, so
missing keys are tolerated without raising; `to_prompt_json` is deliberately not used in these two
functions. The user-policy wording migrates the exact live Menhir installer text (including the
`project:cth.mcp.memory` Good/Bad examples and the `Summaries:` label), not a paraphrase. `Summary`, `SummaryDescription`, `summary_description` (still using shared JSON serialization),
the `MAX_SUMMARY_CHARS`-backed response descriptions, and the `versions` mapping (pointing directly at the
native functions, declarative, no later rebinding) are otherwise unchanged; the now-unused
`summary_instructions` import was removed. Deliberate Menhir policy divergence, disclosed in docs:
`summarize_context` no longer instructs attribute extraction and omits `context['attributes']`
(upstream v0.29.3 renders an ATTRIBUTES block), mirroring the active Menhir patch.

## Files Changed

- `graphiti_core/prompts/summarize_nodes.py` — native structured-policy bodies for `summarize_context`
  and `summarize_pair`; removed unused `summary_instructions` import; everything else unchanged.
- `tests/test_structured_summary_prompts.py` — NEW focused regression suite, 15 tests after review
  corrections (see Verification and Review Corrections).
- `.agent/architecture.md` — new "Structured Summary Policy" section; provider-seams paragraph and
  bootstrap-state wording updated to include the C1d migration and the disclosed attributes-omission
  divergence.
- `.agent/CHANGELOG.md` — dated C1d entry naming only installer #4, labeled Menhir policy divergence,
  tests added, C1a/C1b/C1c context, patches still present until Phase F, worker verification NOT RUN.
- `.agent/for-review/graphiti-softfork-phase-c1d-structured-summaries-glm53-20260914.md` — this wrapup.

## Verification

All worker-run commands were PROHIBITED by the task contract; orchestrator owns verification.

- Targeted pytest on `tests/test_structured_summary_prompts.py`: NOT RUN
- `make test` / CI-shaped unit gate: NOT RUN
- `make lint` (ruff + pyright): NOT RUN
- `make format`: NOT RUN
- `artifact_validate(artifact_type="wrapups", ...)`: NOT RUN by worker (MCP prohibited by task contract)
- Scope inspection (read-only; only the five named files edited): PASS
- Orchestrator verification: PENDING (owned by orchestrator/Codex)

Test coverage written (worker runs NOT RUN): exact two-message role order for both functions; exact
system strings for both; exact distinguishing user-policy phrases for both functions (context:
"Summarize the ENTITY using ONLY facts from the MESSAGES.", format line, "Focus on: what it is, its
role/status, key attributes. Omit filler words."; pair: "Merge these two summaries into one structured
key:value summary.", format line, "Keep the most current/specific values. Drop duplicates.", "Summaries:"
label); exact Good/Bad example text lock; tagged-section interpolation (`<MESSAGES>`, inline `<ENTITY>`,
inline `<ENTITY CONTEXT>`) of the four context values; missing-key and empty-value tolerance without
KeyError; no ATTRIBUTES block and no attributes content even when an `attributes` key is supplied; raw
pair-summary interpolation; `versions['summarize_context'] is summarize_context`,
`versions['summarize_pair'] is summarize_pair`, `versions['summary_description'] is
summary_description`; `summary_description` still renders via shared JSON serialization
(`to_prompt_json`). All tests use type annotations with no ignores; imports sorted per Ruff.

## Claim Cross-Check

- `summarize_context`/`summarize_pair` return exactly two messages with the exact contracted system
  strings: yes
- User prompts contain the contracted policy content (ONLY MESSAGES, `' | '` separator, 150-char
  ceiling, focus/role-status/key attributes, filler omission, GOOD/BAD example; pair: merge, same
  format/ceiling, current/specific, drop duplicates): yes
- Missing keys tolerated via raw f-string `context.get(..., '')` interpolation; no `to_prompt_json` in
  the two migrated functions: yes
- Attributes omission implemented and disclosed as deliberate Menhir policy divergence in
  architecture/changelog/wrapup: yes
- `Summary`, `SummaryDescription`, `summary_description`, `MAX_SUMMARY_CHARS` descriptions unchanged;
  `to_prompt_json` retained for `summary_description`; `summary_instructions` import removed as now
  unused: yes
- `versions` mapping declarative, direct, no rebinding/wrappers/sentinels/aliases/config/hooks/
  monkeypatching: yes
- Only the five named files edited: yes
- No Phase C completion or patch-removal overclaim: yes (installer #4 only; patches remain until Phase F)
- Tests/lint/typecheck executed by worker: no — prohibited by task contract; marked NOT RUN
- No external Menhir paths read; no git/dependency/network/MCP/nested-delegation actions performed: yes

## Completion Checklist

- [x] Native structured-summary bodies in `summarize_nodes.py` only; bottom `versions` mapping left
      declarative and untouched
- [x] Focused tests locking exact behavior, tolerance, identity mapping, and JSON-serialization retention
- [x] Architecture doc: policy boundary, 150-char key:value shape, direct native functions/no rebinding,
      explicit attributes-omission divergence disclosure
- [x] Changelog: dated C1d entry, installer #4 only, Menhir policy divergence label, C1a/C1b/C1c
      context, Phase F pending
- [x] Wrapup with honest NOT RUN worker verification; product commit `7de1792` recorded, wrapup
      closeout commit pending
- [x] Harness task file deletion (per harness instruction)
- [x] Codex acceptance with no open findings
- [ ] Worker test/lint/typecheck runs (worker-prohibited; orchestrator/Codex verification passed)
- [ ] Separate closeout commit for this wrapup

## Review Corrections

C1d was independently reviewed by Codex: two findings were fixed in this revision; worker commands
remain NOT RUN. Pre-correction Codex evidence, recorded honestly: 16 focused tests passed, Pyright
0 errors / 0 warnings, artifact validation 5 records / 0 findings, `git diff --check` clean, Ruff
1 I001 finding.

- P2 prompt-policy parity — the original implementation paraphrased the active Menhir installer's
  user-policy wording instead of migrating it. Both user bodies now use the exact live Menhir text
  (including the `project:cth.mcp.memory` Good/Bad examples, the `Summaries:` label, and the
  current/specific slash and hyphen wording), and the focused tests were strengthened to lock the exact
  distinguishing phrases and example text so a future paraphrase fails.
- P2 lint — Ruff `--no-cache` reported I001 for the test imports; the import list is now sorted exactly
  as Ruff requests (`summarize_context`, `summarize_pair`, `summary_description`, `versions`).

## Acceptance / Closeout

C1d is independently accepted by Codex with both review findings corrected and no open findings.

- Product commit: `7de1792beec1fb603aa9f189116225b9f974cb1e` — "feat: make structured summaries
  native" (Fork-Label: Menhir policy divergence). Contains the four product files
  (`graphiti_core/prompts/summarize_nodes.py`, `tests/test_structured_summary_prompts.py`,
  `.agent/architecture.md`, `.agent/CHANGELOG.md`); this wrapup is the fifth phase file, pending a
  separate closeout commit.
- Final Codex acceptance evidence: focused suite 15 passed / 1 pre-existing warning; Ruff all checks
  passed; Pyright 0 errors / 0 warnings; CI-shaped no-external-dependencies suite 442 passed / 11
  skipped / 3 warnings; artifact validation 5 records / 0 findings; `git diff --check` clean.
- Worker-run commands remain NOT RUN (prohibited by task contract throughout).
- No push was performed and no Menhir patch-removal is claimed; Menhir runtime patches remain
  installed until Phase F.

## Assumptions

- The code-derived contract in the harness task is authoritative; Menhir sources outside this worktree
  were not read, per instruction. The exact live user-policy wording was supplied by the review and is
  now migrated verbatim.
- The `<MESSAGES>` / `<ENTITY>` / `<ENTITY CONTEXT>` tagged-section layout mirrors the existing upstream
  structure of the function and the contracted interpolation requirements.

## Risks / Gaps

- Untested by the worker: if pyright objects to a typing detail, or if a test assertion needs
  adjustment, orchestrator runs may require trivial fixes.
- `summarize_context` output is now summary-only (no extracted attributes); callers relying on upstream
  attribute extraction from this prompt will see the divergence — this is the disclosed intentional
  Menhir policy, not a regression to fix.

## Follow-Up Tasks

- Orchestrator: run targeted pytest + ruff + pyright (or `make check`) and fix any trivial findings;
  commit the product and closeout.
- Phase C continuation: remaining Menhir installers per inventory.
- Phase F: remove the Menhir-side runtime patches once all installers are migrated.
