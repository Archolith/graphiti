# Wrapup — Graphiti Soft-Fork Phase D4: Untyped Attribute Preservation

- **Agent:** OpenCode
- **Model:** zai-coding-plan/glm-5.3-flash
- **Status:** READY FOR ACCEPTANCE — Codex final D4 review found no unresolved source, test,
  compatibility, or reporting defects; the final Codex-owned verification run is all PASS.
- **Plan/ticket:** `.harness/TASK-graphiti-softfork-phase-d4-untyped-attribute-preservation-glm53-20260914-r2.md` (Phase D4 / installer #15; task file deleted after completion per task instruction)
- **Worktree:** `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
- **Base state at start:** D3 accepted at product `cb989fd`, closeout `b3a1b86`
- **Product commit:** `d329037` (`fix: preserve untyped node attributes`)

## Summary

Implemented the fork-native replacement for Menhir installer #15
(`_patch_graphiti_untyped_attribute_preservation`): the untyped/no-schema path of
`graphiti_core/utils/maintenance/node_operations._extract_entity_attributes` now returns a
defensive shallow dict copy of `node.attributes` (absent/falsy attributes treated as empty)
instead of `{}`, with no LLM call in that path. Because `extract_attributes_from_nodes`
assigns the returned dict back to `node.attributes`, pre-existing externally owned properties
survive replacement-save persistence, and the returned mapping never aliases the original node
attribute dict. The typed-schema path is unchanged: episode context build, LLM call, capped
overlay merge via `apply_capped_attributes`, shape validation, and return.

**Policy exclusions honored:** generic and policy-free — no Menhir predicates/names in runtime
code, no hooks, telemetry, logging policy, candidate policy, adaptive dedupe (#16), #17
behavior, monkeypatching, globals, ContextVars, or dependency changes.

## Files Changed

1. `graphiti_core/utils/maintenance/node_operations.py` — `_extract_entity_attributes`
   untyped/no-schema branch: `return dict(node.attributes or {})` (shallow defensive copy)
   instead of `{}`, with a short explanatory comment; typed path untouched
2. `tests/utils/maintenance/test_untyped_attribute_preservation.py` — NEW focused no-DB D4
   test module (6 test functions)
3. `.agent/architecture.md` — NEW section "Untyped Attribute Preservation in Attribute
   Extraction (fork half of installer #15)" (before "Fork / Upstream Topology")
4. `.agent/CHANGELOG.md` — NEW Phase D4 entry prepended, naming installer #15 and deferring
   patch removal to Phase F

## Test Coverage (tests/utils/maintenance/test_untyped_attribute_preservation.py)

- `test_untyped_entity_type_preserves_attributes_without_llm` — `entity_type=None` preserves
  existing attributes; LLM not awaited
- `test_untyped_result_is_distinct_shallow_copy` — result is not the original mapping; caller
  mutation does not touch the node
- `test_empty_fields_schema_preserves_attributes_without_llm` — Pydantic model with zero
  `model_fields` behaves as untyped
- `test_untyped_empty_attributes_returns_empty_dict` — absent attributes yield `{}`
- `test_extract_attributes_from_nodes_preserves_untyped_attributes` — public path with typed
  (`Person`) + untyped nodes in one call; untyped node keeps attributes via a non-aliased copy
  (asserted against the captured pre-call mapping); typed node receives the LLM overlay;
  embedder `create_batch` stubbed as an AsyncMock so the embedding step completes
- `test_typed_schema_overlay_keeps_fields_omitted_by_llm` — typed path: LLM response wins for
  extracted fields, prior values retained for omitted/external fields

No existing tests were modified or weakened.

## Verification

All execution checks are **NOT RUN by worker** (task prohibits tests, formatters, typecheckers,
git, network, MCP; Codex owns all execution verification, commits, grading, publication):

- Focused pytest (`tests/utils/maintenance/test_untyped_attribute_preservation.py` and
  `tests/utils/maintenance/test_node_operations.py`) — NOT RUN by worker
- Cumulative no-DB pytest gate — NOT RUN by worker
- `make lint` (ruff + pyright) — NOT RUN by worker
- `make format` — NOT RUN by worker (code hand-written to canonical single-quote/100-col style)
- Git diff/commit — NOT RUN by worker (UNCOMMITTED)

Static self-review performed by reading the final files: the branch is the smallest possible
change at the existing boundary; `node.attributes` on `EntityNode` defaults to a dict, so
`dict(node.attributes or {})` covers absent/falsy cases; the typed path below the branch is
byte-identical to the prior code.

### Final Codex acceptance run (post-round-1, 2026-09-14) — all PASS

- Focused pytest (D4 plus existing node-operation tests) — PASS:
  `39 passed, 1 warning in 0.36s`
- Cumulative no-external-DB pytest — PASS: `574 passed, 11 skipped, 3 warnings in 32.17s`
- Repository-wide Ruff check — PASS: all checks passed
- Focused Ruff format `--check` — PASS: `2 files already formatted`
- Changed-file Pyright — PASS: 0 errors / 0 warnings / 0 informations
- Local Menhir artifact validation (local only; not remote ingest) — PASS: checked 12 records,
  0 findings
- `git diff --check` — PASS: exit 0 (informational LF-to-CRLF notices only)

Closure notes: round 1's non-awaitable embedder fixture, invalid post-assignment alias assertion,
formatter drift, and stale task-deletion checkbox are closed. The source change itself required no
review correction and remains the minimal defensive shallow-copy behavior matching installer #15.

## Claim Cross-Check

- Untyped/no-schema path returns defensive shallow copy, not `{}` — yes (source read; copy test)
- No LLM call in the untyped path — yes (`llm_generate.assert_not_awaited()` in 4 tests)
- Typed-schema behavior unchanged (context, LLM, capped overlay merge, shape validation) — yes
  (typed branch untouched; overlay test asserts prior-value retention)
- Returned untyped mapping does not alias the original; equal attributes retained through
  `extract_attributes_from_nodes` — yes (`is not` assertion + public-path test)
- Empty/absent attributes treated as empty — yes (dedicated test)
- No Menhir predicates/names, hooks, globals, ContextVars, telemetry/logging policy, deps — yes
  (runtime diff is 5 lines incl. comment)
- Docs updated (architecture.md + CHANGELOG.md, installer #15 named, Phase F deferral) — yes
- Execution checks NOT RUN by worker; work UNCOMMITTED — yes (disclosed above)

## Completion Checklist

- [x] Minimal fork-native change at `_extract_entity_attributes` boundary
- [x] Defensive shallow copy; no aliasing; no LLM in untyped path
- [x] Typed-schema path preserved unchanged
- [x] Dedicated no-DB D4 test module (direct + public paths, aliasing/empty cases, typed overlay)
- [x] Existing tests not weakened or rewritten
- [x] `.agent/architecture.md` + `.agent/CHANGELOG.md` updated
- [x] Wrapup document written (this file)
- [x] Task file deleted (r2 harness task file deleted at end of initial session)
- [x] Codex review/tests/grading/commits (Codex-owned; all final gates above PASS)

## Assumptions

- The wrapup filename follows the task-specified path
  `.agent/for-review/graphiti-softfork-phase-d4-untyped-attribute-preservation-glm53-20260914.md`
  (the harness task file itself carries the `-r2` retry suffix; the task's required product
  name was used verbatim).
- `artifact_validate` MCP is prohibited by the task ("no MCP"), so this wrapup could not be
  validated with the local tooling; Codex's review gate covers it.
- A shallow copy is sufficient (matches installer #15's `dict(...)` semantics); nested values
  are shared by design.

## Risks / Gaps

- Worker execution remained NOT RUN by design; Codex's final acceptance run is all PASS.
- The `attribute_results` list in `extract_attributes_from_nodes` now holds per-node copies for
  untyped nodes; memory overhead is one shallow dict per node (negligible).
- If any other caller relied on the old `{}`-erasure semantics of the untyped path, behavior
  changes (that erasure is exactly the bug being fixed); a repo search found no such caller.

### Codex review round 1 (worker corrections applied; Codex re-run pending)

Codex evidence: focused pytest `1 failed, 38 passed, 1 warning`; Ruff check PASS; Pyright 0
errors; `git diff --check` PASS; Ruff format flagged five multi-line helper calls in the test
file. Source behavior had no finding. Corrections, all applied by editing the D4 test file only
(worker remains prohibited from executing tests/formatters/typecheckers/git):

1. Public `extract_attributes_from_nodes` test failed because `embedder.create_batch` was a
   plain `MagicMock` and could not be awaited. FIXED: the shared client fixture's `create_batch`
   is now an `AsyncMock` with `side_effect=lambda inputs: [[0.0, 0.0] for _ in inputs]`
   (valid per-node embedding result); attribute extraction is not bypassed.
2. The public-path test asserted `result[1].attributes is not untyped_node.attributes`, but
   `result[1]` is the same `EntityNode` object, so post-assignment identity was guaranteed.
   FIXED: the original untyped attributes mapping is captured before the call; the assertions
   now check the post-call mapping equals — and is not — that captured original, preserving the
   top-level non-aliasing proof.
3. Ruff formatter shapes applied: each of the five three-line
   `_extract_entity_attributes(clients.llm_client, node, None, None, ...)` calls collapsed to
   one line.
4. LLM awaited-once assertion in the public test still refers to attribute extraction only:
   with `episode=None` and no summaries, `_extract_entity_summaries_batch` issues no LLM call.

Wrapup completion checklist corrected: the r2 task file was deleted at the end of the initial
session; the item is now marked complete.

## Phase F Remainder

Removal of the Menhir-side runtime patch installer #15
(`_patch_graphiti_untyped_attribute_preservation`), together with the remaining installers
(#12 runtime side, #14 runtime side, #16, #17) and the deferred remote structural ingest.
