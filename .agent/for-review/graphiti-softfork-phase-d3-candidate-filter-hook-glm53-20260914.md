# Wrapup — Graphiti Soft-Fork Phase D3: Native Candidate-Filter Hook

- **Agent:** OpenCode
- **Model:** zai-coding-plan/glm-5.3-flash
- **Status:** READY FOR ACCEPTANCE — Codex final D3 review found no unresolved code, test,
  compatibility, or reporting defects; the final Codex-owned verification run is all PASS.
- **Plan:** `.harness/TASK-graphiti-softfork-phase-d3-candidate-filter-hook-glm53-20260914.md` (materialized task file; deleted after completion per task instruction)
- **Worktree:** `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
- **Base HEAD at start:** `1ede8ca` (D2 closeout; product `2cee0a5`)
- **Product commit:** `cb989fd` (`feat: add native dedup candidate filter hook`)

## Summary

Implemented the fork-native neutral candidate-filter hook (Phase D3), replacing only the
Graphiti-side *mechanism* of Menhir installer #14 (`_patch_graphiti_structural_candidate_isolation`,
which wrapped `node_operations._collect_candidate_nodes` after merge/dedup). New module
`graphiti_core/candidate_filter.py` defines a frozen borrowed-data `CandidateFilterContext`
(extracted `EntityNode` + unique merged candidate `EntityNode`), an explicit
`CandidateFilterDecision` enum (`INCLUDE`/`EXCLUDE`), a runtime-checkable `CandidateFilterHook`
protocol, and a strict `evaluate_candidate_filter_decision` validator (non-enum returns raise
`TypeError`; hook exceptions propagate unchanged).

`resolve_extracted_nodes` gained an optional `candidate_filter_hook` parameter. Filtering runs in
new `_apply_candidate_filter` immediately after `_collect_candidate_nodes` (i.e. after semantic
search results and `existing_nodes_override` are merged/uuid-deduplicated by
`_merge_candidate_nodes`), exactly once per unique candidate per extracted node, preserving
candidate order, before deterministic exact/fuzzy handling and before LLM candidate indexing.
All-excluded yields the ordinary no-candidate behavior: node kept as new, no dedupe LLM call, no
identity-gate invocation. `Graphiti` gained `candidate_filter_hook` (ctor param + class-level
typed `None` default, mirroring D2's `__new__`-safe pattern) and forwards it conditionally at
every `resolve_extracted_nodes` call path. `dedupe_nodes_bulk` gained a matching pass-through.
Composition with D2 is independent: each hook's kwargs are forwarded exactly when its own hook is
configured, and `identity_gate_edges` only when the identity hook is present. With no hook, all
call paths omit the new kwarg entirely (legacy signature preserved exactly).

**Policy exclusions honored:** no `structure_role`/`is_view`/`view_kind`/`view_class` predicate,
no canonical-self/receipt/enforce logic, no Menhir logger text/counts, no structural labels, no
source policy, no adaptive dedupe (#16), no untyped attribute preservation (#15), no runtime
monkeypatch/ContextVar/global registration, no Menhir files, no dependency changes.

## Files Changed

1. `graphiti_core/candidate_filter.py` — NEW: decision enum, frozen context, protocol, validator
2. `graphiti_core/utils/maintenance/node_operations.py` — import; `_apply_candidate_filter`;
   `resolve_extracted_nodes` optional param + post-merge filtering + docstring
3. `graphiti_core/graphiti.py` — import; class attr; ctor param + docstring + storage; conditional
   forwarding at 5 call paths (`_extract_and_resolve_nodes`, `_extract_and_dedupe_nodes_bulk`,
   `_resolve_nodes_and_edges_bulk`, add_episode combined route, add_triplet ×2 calls)
4. `graphiti_core/utils/bulk_utils.py` — import; `dedupe_nodes_bulk` optional param +
   pass-through via reworked `_identity_kwargs` kwargs builder + docstring
5. `tests/test_candidate_filter.py` — NEW: 31 test functions
6. `.agent/architecture.md` — NEW "Candidate Filter for Node-Dedupe Candidate Pools" section
7. `.agent/CHANGELOG.md` — NEW Phase D3 entry

## Audited Call Sites

All current `resolve_extracted_nodes` / `dedupe_nodes_bulk` call paths were enumerated by search
and wired: `graphiti.py` `_extract_and_resolve_nodes`, add_episode combined route,
`_extract_and_dedupe_nodes_bulk` → `dedupe_nodes_bulk` → first-pass `resolve_extracted_nodes`,
`_resolve_nodes_and_edges_bulk`, `add_triplet` (source + target resolutions);
`bulk_utils.dedupe_nodes_bulk` first pass. `node_operations._collect_candidate_nodes` retains its
original 3-arg signature (existing direct callers/tests unaffected); `_apply_candidate_filter`
runs in `resolve_extracted_nodes`, so all resolution paths (deterministic and LLM escalation)
are protected.

## Verification

All execution checks are **NOT RUN by worker** (task prohibits tests, formatters, typecheckers,
git, network, MCP; Codex owns all execution verification, commits, grading, publication):

- `make test` / focused pytest on `tests/test_candidate_filter.py` + `tests/test_identity_gate.py` — NOT RUN by worker
- `make lint` (ruff + pyright) — NOT RUN by worker
- `make format` — NOT RUN by worker (code written to canonical single-quote/100-col style by hand)
- Git diff/commit — NOT RUN by worker (UNCOMMITTED)

Static self-review performed by reading final files: signatures, kwarg-forwarding conditions
(independent per hook), class-level default placement, import ordering, and docstrings were
re-checked against the D2 patterns they mirror.

### Final Codex acceptance run (post-round-2, 2026-09-14) — all PASS

- Focused pytest (`tests/test_candidate_filter.py` + `tests/test_identity_gate.py`) — PASS:
  `58 passed, 1 warning in 0.47s`
- Cumulative no-external-DB pytest — PASS: `568 passed, 11 skipped, 3 warnings in 27.13s`
- Repository-wide Ruff check — PASS: all checks passed
- Focused Ruff format `--check` — PASS: `5 files already formatted`
- Changed-file Pyright — PASS: 0 errors / 0 warnings / 0 informations
- Local Menhir artifact validation (local only; not remote ingest) — PASS: checked 11 records,
  0 findings
- `git diff --check` — PASS: exit 0 (informational LF-to-CRLF notices only)

Closure notes: the round-1 fixture, lint, type-check, and formatting findings and the round-2
single-line formatting finding are closed. The final code preserves the legacy no-hook call shape,
keeps Menhir policy outside the fork, and composes independently with the D2 identity gate.

### Codex review round 2 (worker fix applied; Codex re-run pending)

Functional/static results clean: focused pytest `58 passed/1 warning`, Ruff check PASS, Pyright
0 errors, `git diff --check` PASS. One formatting-only finding remained: Ruff format --check
requires the `resolve_extracted_nodes(clients, [extracted_a, extracted_b],
candidate_filter_hook=hook)` call in `test_same_candidate_evaluated_once_per_extracted_node`
(tests/test_candidate_filter.py ~line 362) collapsed onto one line (98 chars ≤ 100). FIXED by
that single edit only; no other files touched.

### Codex review round 1 (worker fixes applied; confirmed clean by round 2 except formatting)

Focused pytest `tests/test_candidate_filter.py tests/test_identity_gate.py`: `1 failed, 57
passed`. Findings and corrections, all applied by editing source only (worker remains prohibited
from executing tests/formatters/typecheckers/git):

1. `test_same_candidate_evaluated_once_per_extracted_node` stubbed semantic search with one
   candidate group for two extracted nodes, tripping the resolver's intentional
   `zip(..., strict=True)`. FIXED: fixture now returns one candidate list per extracted node
   (both entries the same candidate object); the once-per-extracted-node assertion is retained.
2. Ruff I001 in `bulk_utils.py`: `candidate_filter` import moved before the
   `graphiti_core.driver.driver` import block (correct isort position). FIXED.
3. Ruff F841 in `test_all_excluded_runs_no_llm_and_no_identity_gate`: unused `candidate_node`
   assignment removed. FIXED.
4. Pyright: `BadReturnHook` statically incompatible with `CandidateFilterHook`. FIXED with
   `cast(CandidateFilterHook, BadReturnHook())` at the call boundary; the runtime TypeError
   assertion is unchanged.
5. Ruff format --check on `candidate_filter.py`, `graphiti.py`, `tests/test_candidate_filter.py`:
   FIXED by hand — wrapped the `CandidateFilterHook` protocol method signature and the two >100
   -column test defs/calls; root-caused the `graphiti.py` flag to mixed CRLF/LF line endings
   introduced by edits and normalized all changed files to LF (protocol signature wrap applied
   in `candidate_filter.py`; test wraps in the bulk-resolve and dedupe pass-through tests).

## Tests Written (31 functions, tests/test_candidate_filter.py)

Contract types: protocol runtime-checkability; frozen-context reassignment rejection.
Resolver behavior: no-hook ordinary merge; EXCLUDE search candidate (no LLM); INCLUDE
preserves candidate; EXCLUDE `existing_nodes_override` candidate; merge/dedup-before-filter
(duplicate search+override candidate → one hook call); order preservation + exactly-once per
unique candidate + surviving order indexed by LLM; filter before deterministic exact resolution
(exact-name candidate EXCLUDEd → stays new, no LLM) and INCLUDE twin (deterministic merge, no
LLM); same candidate evaluated once per different extracted node; all-excluded → no LLM and no
identity-gate calls; invalid return → TypeError; hook exception propagates; no cross-call
leakage. Wiring: constructor storage; `Graphiti.__new__` default; add_episode (hook forwarded /
no-hook legacy shape / composition with identity gate + `precomputed_edges` identity); add_triplet
(hook, no episode/edges / legacy shape); `_extract_and_dedupe_nodes_bulk` (hook / legacy /
both-hooks); `_resolve_nodes_and_edges_bulk` (hook / legacy / both-hooks);
`dedupe_nodes_bulk` pass-through (hook / legacy shape). Structural: no `menhir` imports in the
mechanism modules. Reused D2 test helper patterns (GraphitiClients stub, spec'd-mock constructor,
wiring captures) with behavior-level, exact-count assertions.

## Claim Cross-Check

- New module contains only mechanism, no Menhir predicate — yes (read final file)
- Filter strictly after merge/dedup, once per unique candidate per extracted node, order preserved — yes (`_apply_candidate_filter` input is `_merge_candidate_nodes` output; single pass in order)
- All-excluded → no-candidate behavior, no dedupe LLM / identity gate — yes (empty pool → `if not candidates: continue` in resolver; verified by test assertions)
- No-hook paths omit the kwarg entirely at every call path — yes (all five `graphiti.py` sites conditional; `bulk_utils` builder returns `{}` entries only when configured)
- D2 composition independent, edge evidence only with identity hook — yes (separate `if` blocks; covered by wiring tests)
- Class-level `__new__`-safe default — yes (`candidate_filter_hook: CandidateFilterHook | None = None` on the class)
- Pre-existing tests not modified — yes (no existing test file touched)
- No Menhir files, no deps, no git/network/MCP actions — yes
- Tests/lint executed — no (prohibited by task; NOT RUN disclosure above)

## Completion Checklist

- [x] Typed public contract (frozen context, decision enum, runtime-checkable protocol)
- [x] Per-instance ctor param/property + class-level typed None default
- [x] Filter boundary/order/exactly-once semantics
- [x] Strict return validation; exception propagation; borrowed-data semantics documented
- [x] D2 composition across all five call paths; legacy signature when unconfigured
- [x] Policy-free fork hook
- [x] Focused no-DB tests (31 functions)
- [x] `.agent/architecture.md` + `.agent/CHANGELOG.md` updated
- [x] Wrapup document written; task file deleted (immediately after this document)
- [x] Execution verification — worker checks remained NOT RUN by design; Codex final acceptance
      run passed all focused, cumulative, lint, format, type, artifact, and diff gates listed above

## Assumptions

- The `candidate_filter_hook` parameter name and `filter_candidate` protocol method mirror the
  D2 naming style; Phase F wiring will reference these names.
- `CandidateFilterContext` intentionally exposes only extracted_node + candidate_node: episode
  and edges are not structurally required by installer #14's replacement mechanism and adding
  them now would freeze unstable surface; they remain available to Phase F via the identity-gate
  context or a later extension.
- `dedupe_nodes_bulk`'s inner kwargs builder kept the name `_identity_kwargs` (now composes both
  hooks) to minimize diff noise; it is module-private.

## Risks / Gaps

- Tests/lint remained unexecuted by the worker by design; Codex's final acceptance run is all PASS.
- The order-preservation test asserts LLM candidate ordering via the rendered prompt string
  (`str(call_args.args[0])`); if the prompt renderer changes shape, that assertion may need
  updating (it is behavior-level, not format-critical).
- `resolved[0].name == 'Joseph'` in the order test assumes 'Joe' does not deterministically match
  'Josephine' before the LLM; both plausible paths still select 'Joseph'.

## Phase F Remainder

Menhir's installer #14 structural/View predicate (`structure_role`/`is_view`/`view_kind`/
`view_class` lookup + drop counting/logging) stays outside the fork and must be wired on top of
`CandidateFilterHook` during Phase F, together with removal of the remaining Menhir installers
(#12 runtime side, #14 runtime side, #15, #16, #17) and the deferred remote structural ingest.
