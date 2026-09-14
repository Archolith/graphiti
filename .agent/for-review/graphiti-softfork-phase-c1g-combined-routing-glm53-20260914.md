# Wrapup — Graphiti Soft-fork Phase C1g: Single-Episode Combined-Extraction Routing + Neutral Extension Hook

- **Agent:** OpenCode
- **Model string:** `zai-coding-plan/glm-5.3-flash` (GLM)
- **Status:** READY FOR REVIEW — C1g independently accepted by Codex; product commit landed; wrapup awaits
  separate closeout commit.
- **Plan/ticket:** `.harness/TASK-graphiti-softfork-phase-c1g-combined-routing-glm53-20260914.md` (Phase C1g; task
  file deleted after completion)
- **Worktree/branch:** `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
  (prior phases through C1f at `5443217`)
- **Commits:** Product commit `432a2d4852a8ecff40315ebbc3f14315d8c18623` — `feat: route single episodes through
  combined extraction`. This wrapup document itself remains UNCOMMITTED pending the separate closeout commit.
- **Verification scope:** Worker-run verification NOT RUN (Codex-owned per task instructions: the worker ran no
  tests, formatters, typecheckers, git, network, or MCP). Independent Codex evidence recorded honestly below,
  including pre-correction failures.

## Summary

Implemented the fork-native half of Menhir installer #1 (`_patch_graphiti_combined_extraction`): ordinary
single-episode `add_episode` extraction now routes through Graphiti's existing combined extractor
(`combined_extraction.extract_nodes_and_edges`) by default, with a required fallback to the upstream separate
`extract_nodes`/`extract_edges` path when custom edge schemas (`edge_types`) are supplied — matching the
installer's fallback behavior. The combined default is a deliberate fork divergence from upstream v0.29.3, whose
hook-absent behavior is always the separate path. Edges produced by the combined route are carried into edge
resolution as per-call function arguments (`precomputed_edges` on `_extract_and_resolve_edges`, which skips
`extract_edges` when set) — no module-symbol rebinding, no `ContextVar` cache, no cross-request leakage, and no
reset ordering on exception or cancellation. A neutral typed extension hook (`graphiti_core/extraction_routing.py`)
lets a host observe extraction inputs and either force a built-in route or supply a complete extraction result;
Graphiti still owns resolution, attribute extraction, dedupe, and persistence in every case. The hook carries no
Menhir policy (receipt, canonical-self, repair, marker, grounding, titled-list, counter, scheduler, telemetry);
Phase F will wire Menhir policy on top and remove installers #1/#2. Bulk routing
(`extract_nodes_and_edges_bulk`) and C1a–C1f behavior are untouched.

## Files Changed

- `graphiti_core/extraction_routing.py` — NEW. Neutral routing types and default policy in a single module
  docstring (license header merged in):
  `ExtractionRoute` (`COMBINED`/`SEPARATE` str enum), frozen `SingleEpisodeExtractionContext` (borrowed
  request-local inputs: frozen container, shared mutable contents, no enforced immutability — hooks must not
  mutate them), frozen `SingleEpisodeExtractionResult` (nodes/edges/node_episode_index_map), runtime-checkable
  `SingleEpisodeExtractionHook` protocol (one method,
  `async extract_single_episode(context) -> ExtractionRoute | SingleEpisodeExtractionResult | None`), and
  `default_extraction_route(edge_types)` (`SEPARATE` iff `edge_types` non-empty, else `COMBINED`; docstring states
  the compatibility-boundary rationale accurately).
- `graphiti_core/graphiti.py` — `Graphiti.__init__` gains `single_episode_extraction_hook: SingleEpisodeExtractionHook | None = None`
  (stored on the instance; absent by default — with it absent, behavior is the fork's combined default route,
  itself a divergence from upstream, not upstream's separate path). NEW private `Graphiti._extract_single_episode`:
  invokes the hook once per call (raising `TypeError` on any return value other than `None`/`ExtractionRoute`/
  `SingleEpisodeExtractionResult`), returns `(nodes, edges_or_None, node_episode_index_map, route)` — hook results
  skip built-in extraction entirely, `COMBINED` calls `extract_nodes_and_edges`, `SEPARATE` calls `extract_nodes`.
  `_extract_and_resolve_edges` gains a `precomputed_edges: list[EntityEdge] | None = None` keyword: when set,
  `extract_edges` is skipped and those edges flow into `resolve_edge_pointers`/`resolve_extracted_edges` as
  before. `add_episode` now uses `_extract_single_episode` + `resolve_extracted_nodes` +
  `_extract_and_resolve_edges(..., precomputed_edges=...)` and records an `extraction.route` span attribute
  (`combined`, `separate`, or `hook`); its docstring documents the new routing. Imports added for
  `extraction_routing` types and `extract_nodes_and_edges` (sorted into the existing import block).
- `tests/test_extraction_routing.py` — NEW, 17 focused tests (no DB): default route selection (`None`/`{}` →
  COMBINED; custom edge types → SEPARATE); hook forces SEPARATE without edge types and forces COMBINED despite
  custom edge types; hook-provided `SingleEpisodeExtractionResult` skips both built-in extractors and returns the
  supplied nodes/edges/index-map with route `None`; hook receives the request-local context (the test passes its
  own edge-type map through so the identity assertion is meaningful); invalid hook return raises `TypeError`; hook
  exceptions propagate and the same instance routes normally afterward (nothing to reset); `asyncio.CancelledError`
  propagates and the instance remains usable; two concurrent routings (asyncio.gather) stay isolated;
  `precomputed_edges` skips `extract_edges` entirely while the separate fallback still calls it (with sync
  `resolve_edge_pointers` stubs matching the real signature); a DB-free mocked public `add_episode` wiring test
  (`test_add_episode_combined_route_wiring_end_to_end`) proving the combined extractor runs, precomputed edges
  reach `resolve_extracted_edges`, `extract_nodes`/`extract_edges` never run on the ordinary route, results and
  the `extraction.route=combined` span attribute come out correctly; a structural AST check that the routing
  mechanism imports no `menhir` module; protocol runtime-checkability. Tests stub module-level functions on
  `graphiti_core.graphiti` and build `Graphiti` via `__new__` (no driver/client construction).
- `.agent/architecture.md` — NEW "Single-Episode Extraction Routing (native, fork)" section (route policy with
  accurate fallback rationale, upstream-divergence disclosure, hook contract variants, per-call edge carrying,
  state semantics, Phase F scoping); "Current Bootstrap State" paragraph updated.
- `.agent/data_models.md` — NEW "Single-Episode Extraction Routing Contract (native, fork)" entry documenting the
  enum, default policy with the compatibility-boundary rationale, context/result dataclasses (borrowed-inputs
  wording), protocol contract, and state semantics.
- `.agent/CHANGELOG.md` — NEW dated C1g entry (fork half of installer #1 with the correct
  `_patch_graphiti_combined_extraction` name, Phase F remainder stated, honest verification evidence, and a
  Review Corrections bullet).
- `.harness/TASK-graphiti-softfork-phase-c1g-combined-routing-glm53-20260914.md` — deleted at end of task (as
  instructed).

## Verification

Worker-run verification: NOT RUN — the worker executed no tests, tools, git, network, or MCP; Codex owns
verification and git. All PASS/FAIL results below are Codex's independent runs.

Product commit: `432a2d4852a8ecff40315ebbc3f14315d8c18623` — `feat: route single episodes through combined
extraction` — accepted by Codex with exactly the following results:

Final Codex acceptance run (post-round-2 corrections):

- Focused pytest `uv run pytest tests/test_extraction_routing.py` — PASS `17 passed, 1 warning`
- Ruff check (three changed Python files) — PASS
- Ruff format check — PASS `3 files already formatted`
- Changed-file Pyright — PASS: 3 files, 0 errors / 0 warnings / 0 info
- Cumulative no-external-database suite — PASS `500 passed, 11 skipped, 3 warnings`
- Repository-wide Ruff — PASS
- Artifact validation — PASS `8 records, 0 findings`
- `git diff --check` — exit 0 (informational LF/CRLF warnings only)
- Change boundary — six product files (`graphiti_core/graphiti.py`, `graphiti_core/extraction_routing.py`,
  `tests/test_extraction_routing.py`, `.agent/architecture.md`, `.agent/data_models.md`, `.agent/CHANGELOG.md`)
  plus this separate wrapup file; harness task file absent

Historical round-by-round evidence (pre-correction, recorded for the review trail):

- Round 1: focused pytest FAIL `4 failed, 12 passed, 1 warning`; Ruff check FAIL 7x E402; Ruff format FAIL
  (2 files flagged); Pyright PASS 0/0/0
- Round 2: focused pytest FAIL `1 failed, 16 passed, 1 warning`; Ruff check PASS; Ruff format FAIL
  (`1 file would be reformatted, 2 already formatted`); Pyright FAIL 1 error
- Worker one-off `ast.parse` sanity check on `graphiti_core/extraction_routing.py` (module-docstring merge) —
  PASS (syntax parse only; not a test/lint/type run)

## Review Corrections (Codex independent review round 1, 2026-09-14)

1. **P1 test fixes** — (a) `test_hook_receives_request_local_context` now passes its own `edge_type_map` through
   `_route` to the routing call, so the identity assertion (`context.edge_type_map is edge_type_map`) is
   meaningful. (b) Both edge-resolution tests now stub `resolve_edge_pointers` as a plain sync function — the real
   function is sync; the previous async stubs passed unawaited coroutines into `resolve_extracted_edges`. (c) The
   no-Menhir prose-token scan (which rejected the module's own policy-neutral documentation containing
   "receipt") was replaced with a structural AST import check (finding 8).
2. **P2 module contract** — `graphiti_core/extraction_routing.py` previously had two consecutive top-level string
   literals: the license block followed by a descriptive string that was a dead expression, so the intended
   documentation was not `__doc__` and Ruff reported 7x E402 on the imports. The license header and module
   documentation are now merged into one real module docstring.
3. **P2 factual fix** — installer #1 is `_patch_graphiti_combined_extraction`, not
   `_patch_graphiti_add_episode_combined`; corrected in the changelog and this wrapup.
4. **P2 compatibility claim corrected** — the phase intentionally changes hook-absent single-episode behavior from
   upstream's separate path to the fork's combined default. "Public defaults unchanged" claims were removed from
   changelog, data models, and this wrapup; the hook parameter defaults to absent while the route default is the
   fork's own combined behavior, disclosed as a deliberate divergence.
5. **P2 fallback rationale corrected** — the SEPARATE custom-schema fallback is documented as a deliberate
   compatibility boundary, not a functional gap: combined-route edges also pass through `resolve_extracted_edges`
   (which performs the downstream typed-attribute work), so the earlier "the combined extractor lacks the
   typed-attribute machinery" claim was false and was removed from the module docstring, architecture, data
   models, changelog, and this wrapup.
6. **P2 context overclaim corrected** — `SingleEpisodeExtractionContext` is now documented as borrowed
   request-local inputs over a frozen container whose list/dict/model contents are shared with the in-flight call
   and NOT copied; no runtime immutability is enforced, and hooks must not mutate them. "Snapshot"/"read-only
   view" wording removed from module docstring, data models, changelog, and this wrapup.
7. **P2 coverage gap** — added `test_add_episode_combined_route_wiring_end_to_end`: a DB-free, narrowly mocked
   public `Graphiti.add_episode` test proving the method actually invokes the combined extractor, carries
   precomputed edges into edge resolution (`resolve_edge_pointers`/`resolve_extracted_edges` receive them),
   never calls the separate extractors on the ordinary route, and records the `extraction.route=combined` span
   attribute. Test count 16 → 17.
8. **P3 brittle coupling test** — the forbidden prose-token scan was replaced by a structural AST check that
   `extraction_routing.py` and `graphiti.py` import no `menhir` package/module; policy-neutral documentation may
   name excluded concepts.
9. **P3 format** — `graphiti_core/graphiti.py` and `tests/test_extraction_routing.py` hand-adjusted toward Ruff
   format style (e.g., the two over-length `fake_resolve_extracted_edges` signatures wrapped in the test file);
   the worker may not run the formatter, so post-round-1 `ruff format --check` remained pending Codex.

## Review Corrections (Codex independent review round 2, 2026-09-14)

10. **P2 test determinism** — `test_default_policy_routes_combined` compared `index_map` against a fresh
    `_combined_fixture()[2]`, whose node UUIDs never match the nodes returned by the stubbed call. The assertion
    now derives the expectation from the same result: `index_map == {node.uuid: [0] for node in nodes}`.
11. **P2 Pyright** — the integration test's instance assignment of `fake_process_episode_data` failed Pyright
    because the stub's first parameter was named `ep` (method protocol requires `episode`). The stub now uses the
    real method's exact parameter names and defaults: `(episode, nodes, entity_edges, now, group_id, saga=None,
    saga_previous_episode_uuid=None, node_episode_index_map=None)`.
12. **P3 exact formatter output** — the `add_episode` unpacking assignment was rewritten to Ruff format's exact
    shape (parenthesized tuple with one target per line, then `= await self._extract_single_episode(` with
    arguments at the reduced indent). Additionally, the bare-LF line ending on `_extract_and_resolve_edges`'s
    `Returns` docstring line was normalized to the file's CRLF endings; the file now contains zero bare-LF lines.

Cumulative review tally: 12 corrected findings — 1x P1, 8x P2, 3x P3. All closed, no open findings; the final
Codex acceptance run above verifies the corrected state.

## Claim Cross-Check

- Files-changed list matches actual edits: yes (matches the verified six-product-file boundary)
- Commits listed (or UNCOMMITTED): yes — product commit `432a2d4852a8ecff40315ebbc3f14315d8c18623` recorded; this
  wrapup itself UNCOMMITTED pending the separate closeout commit
- Verification lines honest: yes — worker runs NOT RUN; historical pre-correction failures preserved as FAIL;
  final PASS results attributed to Codex's acceptance run
- Routes single-episode `add_episode` through the combined extractor by default: yes (`_extract_single_episode`
  default policy; verified by reading code, not execution)
- Preserves separate-path fallback for custom edge schemas: yes (default policy routes SEPARATE when `edge_types`
  non-empty; `extract_edges` still runs on that route)
- Edges carried without global symbol rebinding / cross-request leakage: yes — plain per-call arguments; no
  module globals, class state, or `ContextVar` introduced
- Hook is neutral and policy-free: yes — no Menhir-specific semantics; structural test asserts no `menhir` import
- Hook-absent behavior honestly characterized: yes — the hook parameter defaults to absent; the hook-absent route
  default is the fork's combined path, a deliberate divergence from upstream's separate path (not upstream
  defaults)
- C1a–C1f behavior altered: no — no edits to prompts, `combined_extraction.py`, `nodes.py`, `edges.py`,
  `bulk_utils.py`, or prior-phase tests

## Route / State Semantics

1. `add_episode` builds `edge_type_map_default` as before, then calls `_extract_single_episode`.
2. Hook (if installed) is awaited once with the frozen context container (borrowed, must-not-mutate contents);
   `None` → `default_extraction_route(edge_types)`; `ExtractionRoute` → forced route; result → skip built-in
   extraction.
3. `COMBINED` route: one LLM call yields nodes, edges, and the episode index map; edges are returned to
   `add_episode` and passed as `precomputed_edges`, skipping `extract_edges`; resolution
   (`resolve_edge_pointers` → `resolve_extracted_edges`, including typed-attribute work) is unchanged, so
   invalidation and dedupe behavior is preserved for every route.
4. `SEPARATE` route (custom edge schemas, or hook-forced): `extract_nodes` then `extract_edges` run exactly as
   the pre-C1g path; `precomputed_edges` is `None`.
5. State: everything lives in per-call locals/arguments. Concurrent `add_episode` calls are isolated; exceptions
   (including hook errors and cancellation) propagate with nothing to reset.

## Completion Checklist

- [x] Ordinary single-episode `add_episode` routed through combined extractor natively
- [x] Required fallback preserved for custom edge schemas (custom edge types → separate path, compatibility
      boundary documented accurately)
- [x] Edges carried without global symbol rebinding or cross-request leakage
- [x] Documented neutral typed extension hook adequate for Phase F; hook parameter defaults to absent, with the
      fork's combined default route disclosed as a divergence from upstream
- [x] Focused tests: route selection, edge delivery/fallback, public add_episode wiring, exception/cancellation
      semantics, isolation/concurrency, structural no-Menhir-import check
- [x] Docs updated (`architecture.md`, `data_models.md`) and changelog entry written
- [x] Wrapup written; `.harness` task file deleted
- [x] Tests / lint / format — NOT RUN by worker (prohibited); independently run and PASSED by Codex (see
      Verification: `17 passed`, Ruff check PASS, format `3 files already formatted`, Pyright 0/0/0)

## Assumptions

- "Unsupported/custom schemas" means custom edge schemas (`edge_types` non-empty), matching the authoritative
  task context that installer #1 "falls back to the original separate edge extractor for custom edge schemas".
  The rationale recorded everywhere is the accurate one: a deliberate compatibility boundary preserving the
  pre-C1g custom-schema path, not an absence of attribute machinery (combined edges do reach
  `resolve_extracted_edges`).
- A hook-forced `COMBINED` route with `edge_types` present passes `edge_types` through to
  `extract_nodes_and_edges` (which accepts them); attribute handling remains the resolver's job, same as any
  combined-route edge.
- `node_episode_index_map` keys are pre-resolution extracted-node UUIDs, identical to the existing separate-path
  semantics of `extract_nodes`, so `build_episodic_edges` behavior is unchanged.

## Risks / Gaps

- Closed: post-correction pytest/Ruff/Pyright reruns are done and PASS (see Verification); the hand-applied
  formatting was confirmed clean by Codex's `3 files already formatted` result.
- The routing tests stub module functions on `graphiti_core.graphiti`; they verify wiring, not live LLM behavior.
  The public `add_episode` test is mocked, not DB-backed; a future `graph_driver`-fixture integration check of
  the combined route could add end-to-end confidence.
- Closed: the cumulative no-external-database suite (`500 passed, 11 skipped`) and repository-wide Ruff PASS
  cover the prior "bulk tests / test_graphiti_mock.py unverified" gap.
- Closed: artifact validation ran — `8 records, 0 findings`.

## Follow-Up Tasks

- Codex: create the separate closeout commit for this wrapup document (the product commit
  `432a2d4852a8ecff40315ebbc3f14315d8c18623` has landed; the wrapup itself is still uncommitted).
- Phase F: wire Menhir's policy onto `SingleEpisodeExtractionHook` at Menhir startup and remove installers #1/#2
  (and the remaining runtime patches) on the Menhir side.
