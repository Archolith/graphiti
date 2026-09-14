# Wrapup — Graphiti Soft-fork Phase C1c: Native Prompt JSON Serialization

- Agent: opencode
- Model: zai-coding-plan/glm-5.3-flash
- Date: 2026-09-14
- Status: READY FOR REVIEW (pending Codex closeout commit of this wrapup; worker verification NOT RUN)
- Plan/Ticket: Phase C1c — migrate Menhir installer #3 (`_patch_graphiti_prompt_json`) into fork source
- Worktree/Branch: `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
- Commits: product commit `bd07c62eed12a42be06fc9404e8be03373283d9f` — "fix: harden prompt JSON
  serialization natively" (Fork-Label: provider compatibility); this wrapup is the fifth phase file,
  pending a separate closeout commit
- Baseline: accepted C1b closeout `c361c39` (branch clean at start of session)

## Summary

Replaced the Menhir runtime prompt-JSON patch behavior (installer #3) with a native, source-level
implementation in `graphiti_core/prompts/prompt_helpers.py::to_prompt_json`. The public API
(`data, ensure_ascii=False, indent=None -> str`) is unchanged; before `json.dumps`, the payload is
recursively copied and normalized: dict entries whose string key ends with `_embedding` are dropped,
as are entries whose value is a list/tuple longer than 64 whose first eight items are int/float
(bool excluded) — the reviewed sampled-head compatibility rule (length 64 is preserved). Unsupported
leaf values convert via callable `isoformat`, then `iso_format`, then `to_native` (called with no
arguments), recursing into non-primitive conversion results; a conversion returning the same object
falls back to `str(value)`, as does a value with no conversion method. Exceptions from malformed
conversion methods propagate. Caller input is never mutated; `ensure_ascii`/`indent` pass through to
`json.dumps` unchanged. No monkeypatching, symbol rebinding, sentinels, wrappers, project-specific
names, or compatibility aliases were introduced; all prompt modules and `search/search_helpers.py`
inherit the behavior through the existing shared-helper import, unpatched.

## Files Changed

- `graphiti_core/prompts/prompt_helpers.py` — native `_normalize`/`_looks_like_embedding_vector`/
  `_convert_value` helpers (private, upstream-neutral) and updated `to_prompt_json` implementation.
- `tests/test_prompt_json.py` — NEW focused regression suite, 19 tests after review corrections
  (see Verification for coverage list and Review Corrections for the additions).
- `.agent/architecture.md` — new "Prompt JSON Serialization" section; bootstrap-state wording updated
  to reflect Phase C migrations in progress (C1a/C1b/C1c) with Menhir patches present until Phase F.
- `.agent/CHANGELOG.md` — dated C1c entry naming only installer #3 as migrated, tests added, C1a/C1b
  context, patches still present until F, worker verification NOT RUN.

## Verification

All worker-run commands were PROHIBITED by the task contract; orchestrator owns verification.

- `make test` / targeted pytest on `tests/test_prompt_json.py`: NOT RUN
- `make lint` (ruff + pyright): NOT RUN
- `make format`: NOT RUN
- `artifact_validate(artifact_type="wrapups", ...)`: NOT RUN by worker (MCP prohibited); orchestrator
  pre-correction validation: PASS — 4 records, 0 findings
- Scope inspection (read-only grep of `to_prompt_json` call sites in this worktree): PASS — call
  sites import through `prompt_helpers`, so all inherit the native behavior; no other source files
  were edited.
- Orchestrator pre-correction verification (recorded by Codex review): PASS — 18 focused tests
  passed, Ruff clean, Pyright 0/0, CI-shaped run 425 passed / 11 skipped. Final rerun on the
  corrected diff is pending.

Test coverage written (worker runs NOT RUN): baseline JSON; ensure_ascii=False default;
ensure_ascii=True; indent; `_embedding` key removal with a non-vector value; structural removal at
length 65; preservation at length 64; sampled-head boundary (first 8 numeric → removal even with a
later nonnumeric item); long bool-list and long string-list preservation; nested dict/list/tuple
recursion and input non-mutation; `isoformat` (real datetime), `iso_format`, `to_native` fallbacks
including recursive non-primitive conversion; conversion returning self → str; plain unsupported
object → str; fallback precedence (isoformat wins over iso_format/to_native, later methods not
called); malformed conversion-method exception propagation via `pytest.raises`. All payloads are
type-correct with no ignores.

## Claim Cross-Check

- Public API preserved exactly (`data, ensure_ascii=False, indent=None`): yes
- No caller input mutation: yes (fresh dict/list structures built; only immutable leaves returned)
- Removal rule matches reviewed patch: key suffix `_embedding` on string keys; list/tuple, length
  strictly > 64, first eight items int/float not bool: yes (implemented and tested)
- Length 64 preserved, bool/string lists preserved: yes
- Fallback order isoformat → iso_format → to_native, no-arg calls, self-return → str, recursive
  non-primitive conversion, str final fallback, exceptions propagate: yes
- No edits outside the five scoped files: yes
- No monkeypatching/rebinding/sentinels/wrappers/aliases/project-specific names: yes
- No Phase C completion or patch-removal overclaim: yes (installer #3 only; patches remain until F)
- Tests/lint/typecheck executed: no — prohibited; marked NOT RUN

## Completion Checklist

- [x] Native implementation in `prompt_helpers.py` only; no prompt-module edits
- [x] Focused tests at the exact reviewed boundaries, including fallback precedence and
      exception-propagation locks added by review corrections
- [x] Architecture doc: boundary, rationale (prompt bloat / temporal serialization failure),
      exact >64/first-eight rule, shared-helper inheritance without runtime rebinding
- [x] Changelog: dated C1c entry, installer #3 only, C1a/C1b context, Phase F pending
- [x] Wrapup with honest NOT RUN worker verification; product commit `bd07c62` recorded, wrapup
      closeout commit pending
- [x] Task file deletion (per harness instruction)
- [x] Codex acceptance with no open findings
- [ ] Worker test/lint/typecheck runs (worker-prohibited; orchestrator/Codex verification passed)
- [ ] Separate closeout commit for this wrapup

## Review Corrections

C1c was independently reviewed by Codex: implementation correct, orchestrator pre-correction gates
green (18 focused tests passed, Ruff clean, Pyright 0/0, CI-shaped 425 passed / 11 skipped,
artifact validation 4 records / 0 findings; diff-check pending final rerun). Two findings were
fixed in this revision; worker commands remain NOT RUN.

- P2 coverage gap — fallback precedence and exception propagation were untested. Added
  `test_isoformat_takes_precedence_over_later_methods` (isoformat result wins; iso_format/to_native
  raise AssertionError if called) and `test_conversion_method_exception_propagates` (distinctive
  ValueError from the selected method propagates, via `pytest.raises`). `pytest` import added.
- P3 docs — `.agent/architecture.md` referenced "Prompt JSON Serialization above" although the
  section appears below that paragraph; directional wording corrected to "below".

## Acceptance / Closeout

C1c is independently accepted by Codex with both review findings corrected and no open findings.

- Product commit: `bd07c62eed12a42be06fc9404e8be03373283d9f` — "fix: harden prompt JSON
  serialization natively" (Fork-Label: provider compatibility). Contains the four product files
  (`graphiti_core/prompts/prompt_helpers.py`, `tests/test_prompt_json.py`, `.agent/architecture.md`,
  `.agent/CHANGELOG.md`); this wrapup is the fifth phase file, pending a separate closeout commit.
- Final Codex acceptance evidence: focused suite 20 passed / 1 pre-existing warning; Ruff all
  checks passed; Pyright 0 errors / 0 warnings; CI-shaped no-external-dependencies suite 427
  passed / 11 skipped / 3 warnings; artifact validation 4 records / 0 findings;
  `git diff --check` clean.
- Worker-run commands remain NOT RUN (prohibited by task contract throughout).
- No push was performed and no Menhir patch-removal is claimed; Menhir runtime patches remain
  installed until Phase F.

## Assumptions

- The behavioral contract embedded in the harness task (derived by Codex from the live Menhir
  installer) is authoritative; Menhir sources outside this worktree were not read, per instruction.
- "Recursively run the fallback on the converted value" is implemented as recursion through the
  same conversion chain (`_convert_value`), which matches the tested "conversion returning a
  non-primitive that itself converts" case; converted plain containers without conversion methods
  therefore reach the `str` fallback.
- Status remains READY FOR REVIEW pending the separate closeout commit for this wrapup; the product
  itself is committed as `bd07c62`, worker-run commands stayed NOT RUN, and verification evidence is
  Codex/orchestrator-owned.

## Risks / Gaps

- Untested by the worker: if ruff/pyright object to formatting or a typing detail, orchestrator
  runs may require trivial fixes.
- The sampled-head rule is intentional and must not be strengthened to inspect all 65+ items; a
  future reviewer should treat the first-eight boundary as a compatibility contract.
- Tuple values are serialized as JSON arrays after normalization (standard `json.dumps` behavior);
  normalized output replaces tuples with lists in the private copy only.

## Follow-Up Tasks

- Orchestrator: run `make check` (or targeted pytest + ruff + pyright) and fix any trivial
  formatting/type findings.
- Phase C continuation: remaining Menhir installers per inventory.
- Phase F: remove the Menhir-side runtime patches once all installers are migrated.
