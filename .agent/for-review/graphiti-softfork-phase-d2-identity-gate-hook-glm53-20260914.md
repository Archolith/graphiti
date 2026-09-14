# Wrapup — Graphiti Soft-fork Phase D2: Native Neutral Identity-Gate Hook for LLM-Proposed Node Merges

- **Agent:** OpenCode
- **Model string:** `zai-coding-plan/glm-5.3-flash` (GLM)
- **Status:** READY FOR ACCEPTANCE — Codex final D2 review found no unresolved code, test,
  compatibility, or reporting defects; the final Codex-owned verification run below is all PASS.
  Round 3's default-path compatibility defect is closed: the pre-existing extraction-routing and
  bulk tests now pass WITHOUT modification, and all 27 focused D2 tests pass. Phase F still owns
  Menhir policy wiring and installer removal. This wrapup is ready for Codex acceptance.
- **Plan/ticket:** `.harness/TASK-graphiti-softfork-phase-d2-identity-gate-hook-glm53-20260914.md`
  (Phase D2 / Graphiti-side mechanism half of installer #12; task file deleted after completion)
- **Worktree/branch:** `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch
  `menhir/0.29.3` (accepted HEAD at task start includes D1 product `441e781` and closeout `2385998`)
- **Commits:** UNCOMMITTED (worker is prohibited from running git; Codex owns commits)
- **Verification scope:** Worker verification NOT RUN by design — the task prohibits the worker from
  running tests, formatters, typecheckers, git, network, or MCP. Verification below preserves the
  exact Codex round-1 execution evidence, adds inspection-only evidence and one permitted `ast.parse`
  syntax sanity check, and discloses every execution claim as NOT RUN by the worker.

## Summary

Implemented the fork-native, policy-free mechanism half of Menhir installer #12
(`_patch_graphiti_dedup_identity_gate`): a neutral typed identity-gate hook at the
post-validation/pre-promotion boundary of `node_operations._resolve_with_llm`. A new
`graphiti_core/identity_gate.py` defines a runtime-checkable `IdentityGateHook` protocol, a frozen
`IdentityGateContext` (borrowed request-local evidence: extracted node, candidate node, LLM
`candidate_id`, episode when available, previous episodes, and request-local extracted/precomputed
edge evidence), an explicit `IdentityGateDecision` (`ALLOW`/`VETO`), and a strict
`evaluate_identity_decision` return validator (`TypeError` on anything else; hook exceptions
propagate). `Graphiti.__init__` gains an optional `identity_gate_hook` parameter following the
existing `single_episode_extraction_hook` pattern. The hook is invoked exactly once per valid
LLM-proposed merge — after normalized `NodeResolutions` and before `_promote_resolved_node` or any
resolved-state/uuid-map/duplicate-pair mutation — and never for `-1` decisions, invalid candidate
ids, invalid/duplicate relative ids, deterministic exact/similarity resolution, or paths with no
proposed merge. VETO preserves Graphiti's ordinary no-duplicate behavior; ALLOW preserves ordinary
promotion. Edge evidence travels only through ordinary per-call arguments
(`identity_gate_edges` on `resolve_extracted_nodes`/`_resolve_with_llm`; episode-indexed
`extracted_edges` on `dedupe_nodes_bulk`) — no module globals, no `ContextVar`, no
`generate_response` wrapping, no symbol rebinding. Compatibility (established in review round 3):
identity-gate kwargs are forwarded from `Graphiti`/bulk call paths ONLY when a hook is configured;
with no hook, every call path (add_episode, _extract_and_resolve_nodes,
_extract_and_dedupe_nodes_bulk, _resolve_nodes_and_edges_bulk, add_triplet, dedupe_nodes_bulk's
first pass) uses the exact legacy resolver/dedupe signature with both new kwargs omitted entirely —
edge evidence alone never triggers keyword forwarding. No Menhir policy entered the fork (no identity
heuristics, stopword/acronym/Jaccard logic, edge-fact mention policy, warning text, receipts, or
telemetry); Menhir-side installer #12 removal remains Phase F.

## Audited resolve_extracted_nodes Call Sites

Every current call site in the repository (grep-verified; none in `server/` or `mcp_server/`):

1. `graphiti_core/graphiti.py` — single-episode `add_episode` (in-method resolution after
   `_extract_single_episode`): passes `self.identity_gate_hook` and
   `identity_gate_edges=precomputed_edges` (combined/hook edges; `None` on the SEPARATE route,
   where edges legitimately do not exist yet at node-resolution time).
2. `graphiti_core/graphiti.py` — `_resolve_nodes_and_edges_bulk`: passes the hook and
   `identity_gate_edges=edges_by_episode.get(episode.uuid)` (each episode's deduped extracted edge
   evidence, available at that boundary).
3. `graphiti_core/graphiti.py` — `_extract_and_dedupe_nodes_bulk` → `dedupe_nodes_bulk`: passes the
   hook and the episode-indexed `extracted_edges_bulk` via new `dedupe_nodes_bulk` pass-through
   parameters.
4. `graphiti_core/graphiti.py` — `add_triplet` (both source and target): passes the hook with no
   episode and no edge evidence, as appropriate for that flow.
5. `graphiti_core/graphiti.py` — `_extract_and_resolve_nodes` helper: passes the hook; no edge
   evidence (edges are extracted later in that flow). NOTE: this helper currently has no in-repo
   callers (dead code); updated for consistency so any future caller gets hook wiring.
6. `graphiti_core/utils/bulk_utils.py` — `dedupe_nodes_bulk` first pass: forwards
   `identity_gate_hook` and per-episode `identity_gate_edges` into `resolve_extracted_nodes`.

## Files Changed

- `graphiti_core/identity_gate.py` — NEW module: module docstring documenting the
  mechanism-replacement scope, invocation timing, borrowed-data semantics, and no-cache guarantee;
  `IdentityGateDecision` enum (`ALLOW`/`VETO`); frozen `IdentityGateContext` dataclass with
  per-field availability documentation; runtime-checkable `IdentityGateHook` protocol;
  `evaluate_identity_decision` strict validator. Hand-formatted to Ruff style (protocol method
  signature wrapped; no lines over 100 chars).
- `graphiti_core/utils/maintenance/node_operations.py` — imports of the identity-gate types (placed
  in sorted position after `graphiti_core.helpers`); `_resolve_with_llm` gains
  `identity_gate_hook`/`identity_gate_edges` parameters and invokes the hook exactly once on the
  valid proposed-merge branch only (before promotion/state mutation); docstring updates;
  `resolve_extracted_nodes` gains and forwards the same two optional parameters (absent hook ⇒
  behavior unchanged).
- `graphiti_core/graphiti.py` — `identity_gate_hook` import (sorted after `graphiti_core.helpers`);
  `from typing import Any` added for the type-clean kwargs mappings; new optional constructor
  parameter (documented like the extraction hook) stored as `Graphiti.identity_gate_hook`, backed by
  a class-level typed default (`identity_gate_hook: IdentityGateHook | None = None`) so
  `__init__`-bypassing instances safely observe no hook; wiring at
  all five in-file `resolve_extracted_nodes` call sites and the `dedupe_nodes_bulk` call via
  conditional kwargs mappings — hook and edge evidence forwarded ONLY when a hook is configured,
  legacy signature otherwise (see audited list above); `add_episode` docstring Notes untouched (hook
  documented in constructor).
- `graphiti_core/utils/bulk_utils.py` — `IdentityGateHook` import (sorted after
  `graphiti_core.helpers`); `dedupe_nodes_bulk` gains optional `identity_gate_hook` and
  `extracted_edges` (episode-indexed) parameters, forwarded per episode to
  `resolve_extracted_nodes` only when a hook is configured (local `_identity_kwargs` helper); with
  no hook, the legacy resolver shape is used.
- `tests/test_identity_gate.py` — NEW focused no-DB suite (27 test functions / 27 collected
  outcomes): absent-hook compatibility; allow/veto on valid merges; exactly-once invocation per
  merge with flattened-candidate-index assertions (Joe→Joseph at global id 0, Java→Java candidate 1
  at global id 1, node identity asserted via `is`); no invocation for `-1`, invalid candidate id
  (999), out-of-range relative id, duplicate relative id, deterministic exact match, deterministic
  fuzzy match, and no-candidate paths; full context evidence (node identity, candidate id, episode,
  previous episodes, edge evidence) and no cross-call leakage across two resolutions; strict
  invalid-return `TypeError`; hook exception propagation; protocol runtime-checkability;
  frozen-context reassignment rejection via `cast(Any, context)` (B010- and Pyright-clean);
  `Graphiti` constructor storage plus default-absent exercised through the REAL `Graphiti`
  constructor with spec'd mocks (`Mock(spec=GraphDriver/LLMClient/EmbedderClient/CrossEncoderClient)`)
  that satisfy `GraphitiClients` runtime validation; DB-free public wiring tests for single
  `add_episode` (full constructor-built instance, hook + `precomputed_edges` when configured, AND a
  no-hook delegation test asserting both identity kwargs are entirely absent), `add_triplet` (full
  instance, BOTH source and target resolver calls captured in order — configured path asserts the
  hook and no episode/edge evidence; no-hook path asserts both kwargs absent), bulk
  `_extract_and_dedupe_nodes_bulk` (hook + episode edges when configured; no-hook legacy signature),
  `dedupe_nodes_bulk` pass-through (hook + edges), and no-hook legacy-signature delegation; and a
  structural AST check that the mechanism files import no `menhir` module. Behavior assertions only
  — no source-text coupling beyond the AST import check.
- `.agent/architecture.md` — NEW section "Identity Gate for LLM-Proposed Node Merges" covering
  scope (mechanism half of installer #12), invocation lifecycle, decision semantics,
  borrowed-data/mutation rules, per-flow edge-evidence availability, default compatibility, and the
  Phase F remainder.
- `.agent/CHANGELOG.md` — NEW dated Phase D2 entry at the top describing the module, parameter
  threading, call-site wiring, test suite, docs, Phase F remainder, honest NOT RUN disclosure, and
  the round-1 review-correction summary.
- `.harness/TASK-graphiti-softfork-phase-d2-identity-gate-hook-glm53-20260914.md` — deleted at end
  of task (as instructed).

## Verification

Initial Codex round-1 execution run (preserved exactly, 2026-09-14):

- Focused pytest `tests/test_identity_gate.py` — FAIL: `4 failed, 19 passed, 1 warning` (23
  collected outcomes)
- Ruff check — FAIL: 4 errors (3 import-order errors in `graphiti.py`, `bulk_utils.py`,
  `node_operations.py`; 1 unused variable `prev_episode` in `tests/test_identity_gate.py`)
- Ruff format `--check` — FAIL: 2 files would be reformatted (`graphiti_core/identity_gate.py`,
  `tests/test_identity_gate.py`); 3 already formatted
- Pyright — FAIL: 2 errors (direct assignment to frozen `context.candidate_id` in the tests;
  incompatible direct assignment to `graphiti._process_episode_data`)
- Git, network, MCP, `artifact_validate` — NOT RUN by worker (prohibited / unavailable)

Worker corrections: all findings addressed (see Review Corrections below). Worker execution
verification after corrections: NOT RUN (prohibited). Inspection-only evidence, gathered by reading
the current files after editing:

- PASS (inspection): the hook invocation sits on the `duplicate_candidate_id in candidates_by_id`
  branch only, before `_promote_resolved_node` and before any `state` mutation; all other branches
  (`-1`, invalid candidate id, invalid/duplicate relative id) bypass it.
- PASS (inspection): `resolve_extracted_nodes` forwards both new parameters to `_resolve_with_llm`;
  both default to `None` so existing direct callers (including the existing test suite) are
  compatible unchanged.
- PASS (inspection): all audited call sites pass the configured hook; edge evidence flows as
  ordinary arguments only; grep confirms no `ContextVar`, no module-global hook registry, and no
  `generate_response` wrapper was introduced.
- PASS (inspection): no Menhir policy constructs exist in the new/changed fork files
  (no heuristic functions, stopword lists, Jaccard/acronym logic, Menhir logger names, or receipt
  code); the test suite includes a structural AST menhir-import check.
- PASS (inspection): imports in all three product files are in isort order
  (`graphiti_core.helpers` < `graphiti_core.identity_gate` < `graphiti_core.llm_client`); no
  duplicate or unused imports remain.
- PASS (inspection): no lines over 100 characters exist in `identity_gate.py` or
  `tests/test_identity_gate.py` (the remaining >100-char lines in `graphiti.py`,
  `node_operations.py`, `bulk_utils.py` are pre-existing comments/docstrings/strings that Ruff
  format does not touch); the exact flattened-candidate-index expectations in the exactly-once test
  match `_merge_candidate_nodes` semantics.
- PASS (inspection): the constructor/wiring tests instantiate the real `Graphiti` constructor with
  spec'd mocks satisfying `GraphitiClients` validation; `NodeNamespace`/`EdgeNamespace`
  constructors only store references; `create_tracer(None, ...)` returns the no-op tracer; telemetry
  capture is exception-guarded.
- PASS (worker one-off syntax sanity): `ast.parse` succeeded on all five changed/new Python files
  (not a test/lint/type run).
- Focused pytest / Ruff / Pyright / cumulative suite / git / `artifact_validate` after corrections —
  NOT RUN by worker (prohibited); Codex owns all execution verification, commits, grading, and
  publication and will rerun.

## Review Corrections (Codex independent review round 1, 2026-09-14)

1. **[P1] `test_hook_invoked_exactly_once_per_merge` wrong candidate id** — the Java resolution used
   global `duplicate_candidate_id = 0`, which selects Joseph from the flattened candidate index
   `[Joseph, Java candidate 1, Java candidate 2]`. Fixed to `duplicate_candidate_id = 1` with an
   explanatory comment; the test now also asserts the hook context mapping per call
   (`extracted_node`/`candidate_node` identity via `is`, `candidate_id` 0 and 1 respectively) plus
   the resolved UUIDs and uuid-map entries.
2. **[P1] `test_graphiti_constructor_stores_identity_gate_hook` failed `GraphitiClients` runtime
   validation** — bare `MagicMock`s failed the pydantic isinstance checks. Replaced with a
   `_make_full_graphiti` helper that runs the REAL `Graphiti` constructor with spec'd doubles
   (`Mock(spec=GraphDriver)`, `Mock(spec=LLMClient)`, `Mock(spec=EmbedderClient)`,
   `Mock(spec=CrossEncoderClient)`), so the constructor — including
   `single_episode_extraction_hook = None` and `identity_gate_hook` storage — is genuinely
   exercised; the test additionally asserts the default-absent hook and extraction hook.
3. **[P1] `test_add_episode_wiring` incomplete instance** — built via `__new__` and omitted
   `single_episode_extraction_hook`. Now uses `_make_full_graphiti` (fully constructed instance with
   both hook attributes initialized); `retrieve_episodes`, `_process_episode_data`, and `tracer` are
   installed via `monkeypatch.setattr` (also resolves the Pyright finding on the
   `_process_episode_data` assignment). The real behavior-level wiring assertion (hook + edges
   reaching resolution) is preserved.
4. **[P1] `test_add_triplet_wiring` incomplete instance and single-call capture** — now uses
   `_make_full_graphiti` (with a real `llm_client` attribute); the stubbed resolver captures EVERY
   call with the resolved node name, args, and kwargs, and the test asserts both calls occurred in
   order (`['Alice', 'Bob']`), each with empty positional args (no episode/edge evidence) and with
   `identity_gate_hook` set to the configured hook and no `identity_gate_edges` kwarg.
5. **[P2] Ruff check import order** — `identity_gate` imports moved to their sorted position after
   `graphiti_core.helpers` in `graphiti.py`, `bulk_utils.py`, and `node_operations.py` (a duplicate
   `helpers` import accidentally introduced in `node_operations.py` during the first edit was
   removed).
6. **[P2] Ruff check unused variable** — removed the unused `prev_episode` local from
   `test_context_carries_full_evidence`.
7. **[P2] Ruff format** — `identity_gate.py`: the 103-char protocol method signature is wrapped to
   Ruff's collapsed style; `tests/test_identity_gate.py`: over-100-char `async def` stub signatures
   wrapped to Ruff's param-list style, and the long destructuring-assignment call sites were
   refactored to a two-step destructure that fits on one line (verified no >100-char lines remain
   in either file).
8. **[P2] Pyright frozen-dataclass assignment** — `test_frozen_context_rejects_reassignment` now
   uses dynamic `setattr(context, 'candidate_id', 5)` inside `pytest.raises(AttributeError)`
   (type-clean, same runtime FrozenInstanceError behavior).
9. **[P2] Pyright `_process_episode_data` assignment** — replaced direct attribute assignment with
   `monkeypatch.setattr(graphiti, '_process_episode_data', ...)` (same for `retrieve_episodes` and
   `tracer` for consistency).
10. **[P2] Test-count reporting** — the file contains 23 test functions / 23 collected outcomes,
    not 20 as previously claimed; the wrapup and CHANGELOG now state exact counts (23 functions /
    23 outcomes), never conflating parametrized outcomes with functions.
11. **[P2] `add_triplet` wiring coverage strengthened** — both source and target resolver calls are
    now captured and asserted (see item 4); the claim in the test name/docstring and this wrapup
    matches the actual assertions.

## Review Corrections (Codex independent review round 2, 2026-09-14)

Initial Codex round-2 execution run (preserved exactly):

- Focused pytest `tests/test_identity_gate.py` — FAIL: `1 failed, 22 passed, 1 warning`
- Ruff check — FAIL: 1 error (B010 constant-string `setattr` in the frozen-context test)
- Ruff format `--check` — FAIL: 2 files would be reformatted (`graphiti_core/identity_gate.py`,
  `tests/test_identity_gate.py`); 3 already formatted
- Pyright — FAIL: 1 error (`graphiti.tracer.start_span.return_value` assignment after `tracer` is
  typed as `Tracer`)

Corrections (no product behavior changed — no defect was exposed; all four findings were
test/static only):

12. **[P1] `fake_process_episode_data` stub arity** — the stub accepted only 5 positional args while
    `add_episode` passes 8. It now uses the real method-compatible positional signature
    `(episode, nodes, entity_edges, now, group_id, saga=None, saga_previous_episode_uuid=None,
    node_episode_index_map=None)`; the behavior-level wiring assertion (hook + `precomputed_edges`
    reaching resolution) is unchanged.
13. **[P2] Ruff B010 vs Pyright conflict on the frozen-context test** — replaced
    `setattr(context, 'candidate_id', 5)` with `cast(Any, context).candidate_id = 5` inside
    `pytest.raises(AttributeError)` (`from typing import Any, cast` added), which is both B010-clean
    and Pyright-clean and still genuinely proves frozen assignment raises.
14. **[P2] Ruff format shapes** — `identity_gate.py`: the TypeError message is now one joined
    f-string line inside the parenthesized `raise` (exactly 100 chars);
    `tests/test_identity_gate.py`: both `fake_resolve` stub signatures collapsed to single lines
    (exactly 100 chars each). Verified by inspection that neither file has any line over 100 chars.
15. **[P2] Pyright tracer typing** — the wiring test now configures a local `Mock` tracer fully
    (`tracer.start_span.return_value = span_cm`) BEFORE `monkeypatch.setattr(graphiti, 'tracer',
    tracer)`, so no attribute is set on the instance after it holds its declared `Tracer` type;
    span assertions are unchanged.

## Review Corrections (Codex cumulative review round 3, 2026-09-14)

Initial Codex round-3 execution run (preserved exactly):

- Cumulative no-external-DB pytest suite — FAIL: `3 failed, 530 passed, 11 skipped, 3 warnings`
  (the 3 failures were PRE-EXISTING tests correctly detecting an absent-hook compatibility defect;
  they were NOT modified)
- Repository-wide Ruff check — PASS

Corrections (P1 PRODUCT defect — the only finding this round):

16. **[P1 product] No-hook paths still forwarded the new identity-gate kwargs** —
    `Graphiti`/`bulk_utils` call paths passed `identity_gate_hook`/`identity_gate_edges` (and
    `extracted_edges` for `dedupe_nodes_bulk`) unconditionally, so with no hook configured the
    resolver/dedupe signature changed anyway, breaking existing wrappers and test doubles that
    accept the prior signature. Fixed with type-clean conditional kwargs mappings at every audited
    call path: `add_episode` (local `identity_kwargs` dict), `_extract_and_resolve_nodes`
    (same), `add_triplet` (single shared mapping for both source and target),
    `_extract_and_dedupe_nodes_bulk` (`dedupe_kwargs` for the `dedupe_nodes_bulk` call),
    `_resolve_nodes_and_edges_bulk` (local `_identity_kwargs(edges)` helper), and
    `dedupe_nodes_bulk`'s first pass (local `_identity_kwargs(index)` helper). When a hook is
    configured, the hook and request-local edge evidence are forwarded exactly as D2 requires;
    when no hook is configured, both new kwargs are omitted entirely — edge evidence alone never
    triggers keyword forwarding. `from typing import Any` added to `graphiti.py` for the
    `dict[str, Any]` annotations (bulk_utils already had `Any`).
17. **[P1 product] Focused-test coverage updated for the compatibility rule** —
    `test_dedupe_nodes_bulk_defaults_pass_no_evidence` was renamed to
    `test_dedupe_nodes_bulk_no_hook_uses_legacy_resolver_signature` and now asserts both new kwargs
    are ABSENT (not present with `None`); new delegation tests
    `test_add_episode_no_hook_uses_legacy_resolver_signature`,
    `test_add_triplet_no_hook_uses_legacy_resolver_signature`, and
    `test_extract_and_dedupe_nodes_bulk_no_hook_uses_legacy_signature` prove the legacy signature on
    the Graphiti-side default paths; the configured-path wiring tests were refactored onto shared
    helpers (`_run_add_episode_wiring`, `_run_add_triplet_wiring`, `_run_bulk_dedupe_wiring`) so
    absent and configured paths are proven against identical stubs. Pre-existing failing tests were
    not touched.

## Review Corrections (Codex re-review round 4, 2026-09-14)

Initial Codex round-4 execution run (preserved exactly):

- Focused pytest `tests/test_identity_gate.py` — FAIL: `3 failed, 23 passed, 1 warning`
- Focused Ruff check — PASS
- Ruff format `--check` — PASS: `5 files already formatted`
- Changed-file Pyright — PASS: 0 errors / 0 warnings / 0 informations

Corrections (tests only — the round-3 product compatibility code and all static checks were clean;
no product or docs changes):

18. **[P1 tests] Three wiring tests compared independently constructed EntityEdge objects** —
    `test_add_episode_wiring_passes_hook_and_precomputed_edges`,
    `test_extract_and_dedupe_nodes_bulk_wiring`, and
    `test_extract_and_dedupe_nodes_bulk_no_hook_uses_legacy_signature` each built a second,
    unrelated `_make_edge(...)` in the test body and compared it (by equality) against the edge the
    helper created; generated UUID/source/target UUIDs differ between the two instances, so the
    assertions failed. Helpers now expose the actual pass-through objects: `_run_add_episode_wiring`
    returns `captured['precomputed_edges']` (the exact list object the combined extractor stub
    produced), and `_run_bulk_dedupe_wiring` returns `captured['extracted_edges_bulk']` plus
    `captured['episode_edges']`. Assertions now use object identity (`is`) — the correct contract
    for ordinary request-local argument pass-through: the configured add_episode test asserts
    `identity_gate_edges is precomputed_edges`; the configured bulk test asserts
    `extracted_edges is extracted_edges_bulk` and edge identity within the lists; the no-hook bulk
    test asserts the same pass-through identity alongside the absent-kwargs assertions. The
    configured-vs-absent hook kwarg semantics are unchanged. No product code or docs were modified;
    counts remain 26 test functions / 26 collected outcomes.

## Review Corrections (Codex re-review round 5, 2026-09-14)

Initial Codex round-5 execution run (preserved exactly):

- Focused pytest `tests/test_identity_gate.py` — FAIL: `2 failed, 24 passed, 1 warning`
- Ruff check — PASS
- Ruff format `--check` — PASS: `5 files already formatted`
- Pyright — PASS: 0 errors / 0 warnings / 0 informations

Corrections (tests only — no product or docs changes):

19. **[P1 tests] Nested-list identity asserted at the wrong level in both bulk wiring tests** —
    `captured['extracted_edges_bulk'][0]` is the per-episode `list[EntityEdge]` while
    `captured['episode_edges'][0]` is a single `EntityEdge`, so the round-4 identity assertions
    compared a list to an edge. Both `test_extract_and_dedupe_nodes_bulk_wiring` and
    `test_extract_and_dedupe_nodes_bulk_no_hook_uses_legacy_signature` now assert list-to-list
    identity (`captured['extracted_edges_bulk'][0] is captured['episode_edges']`) AND edge-to-edge
    identity (`captured['extracted_edges_bulk'][0][0] is captured['episode_edges'][0]`). The
    top-level `identity_gate_edges is extracted_edges_bulk` pass-through identity and the
    configured/absent kwargs assertions are unchanged. No product code or docs were modified;
    counts remain 26 test functions / 26 collected outcomes.

## Review Corrections (Codex cumulative re-review round 6, 2026-09-14)

Initial Codex round-6 execution run (preserved exactly):

- Cumulative no-external-DB pytest suite — FAIL: `1 failed, 535 passed, 11 skipped, 3 warnings` (the
  failing test was the PRE-EXISTING `tests/test_extraction_routing.py` `__new__`-style Graphiti test
  double correctly detecting the defect; it was NOT modified)

Corrections:

20. **[P1 product] `identity_gate_hook` AttributeError on `__init__`-bypassing instances** —
    `add_episode` reads `self.identity_gate_hook`, but instances built via
    `Graphiti.__new__(Graphiti)` (test doubles, subclasses, unpickled-style instances) never run
    `__init__`, so attribute access raised `AttributeError` before the legacy no-hook resolver call.
    Fixed with a centralized typed class-level default: `identity_gate_hook:
    IdentityGateHook | None = None` declared on the `Graphiti` class and overridden per instance by
    `__init__` — no mutable global registration, no scattered `getattr`; configured-instance
    behavior is unchanged (the constructor still stores the passed hook on the instance). Focused
    regressions added: `test_graphiti_bypassing_init_defaults_to_no_hook` (a `__new__` instance
    observes no hook) and `test_bare_instance_add_episode_keeps_legacy_resolver_signature` (a
    `__new__` instance driven through public `add_episode` takes the no-hook path — both identity
    kwargs absent from the resolver call). Suite now 28 test functions / 28 collected outcomes;
    CHANGELOG counts and product description updated accordingly.

## Review Corrections (Codex re-review round 7, 2026-09-14)

Initial Codex round-7 execution run (preserved exactly):

- Focused pytest `tests/test_identity_gate.py` — FAIL: `1 failed, 27 passed, 1 warning`
- Cumulative no-external-DB pytest suite — PASS for the round-6 product fix: the pre-existing
  `test_extraction_routing.py` failure is gone; the 1 remaining failure was the NEW round-6
  regression's own setup (`1 failed, 537 passed, 11 skipped, 3 warnings`)

Corrections (test setup only — no product or docs changes):

21. **[P1 test setup] `_run_add_episode_wiring` tracer install failed on bare instances** — the
    round-6 regression `test_bare_instance_add_episode_keeps_legacy_resolver_signature` drives a
    `Graphiti.__new__(Graphiti)` instance, which has no instance `tracer` attribute, so
    `monkeypatch.setattr(graphiti, 'tracer', tracer)` with the default `raising=True` failed before
    the wiring assertions ran. The helper now installs the Mock tracer with `raising=False`,
    supporting both fully-constructed and bare instances (the configured/no-hook constructor-built
    tests are unaffected). No class-level tracer default was added; no product code changed; counts
    remain 28 test functions / 28 collected outcomes.

## Review Corrections (Codex re-review round 8, 2026-09-14)

Initial Codex round-8 execution run (preserved exactly):

- Focused pytest `tests/test_identity_gate.py` — FAIL: `1 failed, 27 passed, 1 warning`
- Cumulative no-external-DB pytest suite — FAIL: `1 failed, 537 passed, 11 skipped, 3 warnings`
  (the 1 failure was again the round-6 regression's own setup; product behavior confirmed correct)

Corrections (test setup only — no product or docs changes):

22. **[P1 test setup] Bare-instance regression lacked the unrelated pre-D2 extraction-hook field** —
    `test_bare_instance_add_episode_keeps_legacy_resolver_signature` now reaches
    `_extract_single_episode`, which reads `single_episode_extraction_hook` — a field that predates
    D2 and is unrelated to the identity-gate regression — so the `__new__` instance raised
    AttributeError on it. The test now explicitly sets `bare.single_episode_extraction_hook = None`
    before driving `add_episode`, exactly as the existing extraction-routing test double does. No
    production class-level extraction-hook default was added; counts remain 28 test functions /
    28 collected outcomes.

## Review Corrections (Codex re-review round 9, 2026-09-14)

Initial Codex round-9 execution run (preserved exactly):

- Focused pytest `tests/test_identity_gate.py` — FAIL: `1 failed, 27 passed, 1 warning`
- Cumulative no-external-DB pytest suite — FAIL: `1 failed, 537 passed, 11 skipped, 3 warnings`
  (in both runs the sole failure was D2's new synthetic full-flow regression; product behavior
  confirmed correct)

Corrections (test scope only — no product changes; the pre-existing test was not altered):

23. **[P2 test scope] Redundant synthetic regression deleted** —
    `test_bare_instance_add_episode_keeps_legacy_resolver_signature` was removed rather than
    continuing to initialize unrelated `__init__` fields (`clients`, etc.). It was redundant:
    `test_graphiti_bypassing_init_defaults_to_no_hook` directly proves the D2 class-level
    invariant (`Graphiti.__new__(Graphiti)` observes no hook), and the pre-existing
    `tests/test_extraction_routing.py::test_add_episode_combined_route_wiring_end_to_end` already
    drives a properly prepared `Graphiti.__new__` instance through public `add_episode` with a
    legacy resolver signature — that pre-existing test passed in the latest cumulative run and
    supplies the full-flow compatibility evidence. The surviving regression test's docstring now
    records this division of coverage. Suite now 27 test functions / 27 collected outcomes;
    CHANGELOG counts and description updated to match; no claim of the deleted synthetic test
    remains.

## Final Codex Acceptance Run (post-round-9, 2026-09-14) — all PASS

- Focused pytest `tests/test_identity_gate.py` — PASS: `27 passed, 1 warning in 0.13s`
- Cumulative no-external-DB pytest — PASS: `537 passed, 11 skipped, 3 warnings in 13.23s`
- Repository-wide Ruff check — PASS: all checks passed
- Focused Ruff format `--check` — PASS: `5 files already formatted`
- Changed-file Pyright — PASS: 0 errors / 0 warnings / 0 informations
- Local Menhir artifact validation (local only; not remote ingest) — PASS: checked 10 records,
  0 findings
- `git diff --check` — PASS: exit 0 (only informational LF→CRLF notices for architecture/changelog)

Closure notes: round 3's default-path compatibility defect (identity-gate kwargs forwarded on the
no-hook path) is CLOSED — the pre-existing `tests/test_extraction_routing.py` and
`tests/utils/maintenance/test_bulk_utils.py` tests pass without modification, and all 27 focused D2
tests pass. Phase F still owns Menhir policy wiring on top of this hook and the removal of
installer #12's Menhir side. The wrapup is ready for Codex acceptance.

## Claim Cross-Check

- Files-changed list matches actual edits: yes (verified by re-reading each file after editing)
- Commits listed (or UNCOMMITTED): yes — UNCOMMITTED; worker is git-prohibited
- Verification lines honest: yes — initial Codex evidence preserved exactly; all post-correction
  worker execution checks are NOT RUN with the one permitted `ast.parse` sanity check labeled as
  such
- Test counts exact: yes — 27 test functions in `tests/test_identity_gate.py`, 27 collected
  outcomes (no parametrization), never conflating parametrized outcomes with functions
- No-hook compatibility: yes — all six audited call paths omit both identity kwargs entirely when
  no hook is configured (verified by reading the conditional kwargs mappings; proven by the four
  new delegation tests plus the dedupe_nodes_bulk no-hook test)
- Hook invoked exactly once per valid proposed merge, pre-mutation: yes (branch placement verified
  by reading the final code)
- No invocation on non-merge paths: yes (verified per branch)
- Veto/allow preserve ordinary behavior: yes (veto assigns the extracted node; allow calls the
  unchanged `_promote_resolved_node`; hook-absent path is byte-identical logic to pre-D2)
- Edge evidence via ordinary arguments only: yes (no ContextVar/global/rebinding introduced)
- Call-site audit complete: yes (grep over graphiti_core, server, mcp_server, tests; list above)
- No Menhir policy ported: yes
- Both add_triplet resolver calls covered: yes (in-order capture with per-call assertions)
- Tests pass: worker execution NOT RUN throughout (prohibited); all passing results are attributed
  to Codex's runs — the Final Codex Acceptance Run above reports focused pytest `27 passed`,
  cumulative `537 passed, 11 skipped, 3 warnings`, Ruff check/format PASS, Pyright 0/0/0, artifact
  validation `10 records, 0 findings`, `git diff --check` exit 0

## Completion Checklist

- [x] Neutral typed public identity-gate contract in a dedicated module (frozen context + explicit
      allow/veto decision enum)
- [x] Optional per-instance constructor parameter/property, documented like the extraction hook;
      absent-hook compatibility preserved for all callers
- [x] Exactly-once invocation on the post-validation/pre-promotion boundary only
- [x] Veto ⇒ ordinary no-duplicate behavior; allow ⇒ ordinary promotion; strict invalid-return
      `TypeError`; exceptions propagate
- [x] Edge evidence via ordinary arguments at all audited call sites; no globals/ContextVar/
      rebinding/`generate_response` wrappers
- [x] Hook policy-free (no Menhir heuristics/policy of any kind)
- [x] Focused no-DB test suite covering all contract branches and wiring (26 tests), rounds 1–3
      findings corrected including absent-hook signature compatibility
- [x] `.agent/architecture.md` and `.agent/CHANGELOG.md` updated with precise D2 scope, exact test
      counts, and Phase F remainder
- [x] Wrapup written with rounds 1–3 corrections and exact preserved evidence; harness task file
      deleted
- [ ] Tests / lint / typecheck — round 1: 4 test failures, 4 Ruff-check errors, 2 reformattable
      files, 2 Pyright errors; round 2: 1 test failure, 1 B010 error, 2 reformattable files,
      1 Pyright error; round 3: cumulative suite 3 failures (absent-hook compatibility defect,
      FIXED in product), repository-wide Ruff PASS; round 4: 3 focused test-fixture failures
      (independent edge objects; FIXED in tests only), focused Ruff PASS, Ruff format PASS
      (5 files already formatted), changed-file Pyright PASS 0/0/0; round 5: 2 focused test failures
      (nested-list identity at the wrong level; FIXED in tests only), Ruff PASS, Ruff format PASS
      (5 files already formatted), Pyright PASS 0/0/0; round 6: cumulative suite 1 failure
      (`__init__`-bypassing instances hit AttributeError reading `identity_gate_hook`; FIXED in
      product via class-level typed default, regressions added); round 7: 1 focused failure (bare
      instance had no tracer attribute for the helper's raising=True setattr; FIXED in test setup
      only; product fix confirmed working by cumulative suite); round 8: 1 focused failure (bare
      instance lacked the unrelated pre-D2 `single_episode_extraction_hook` field; FIXED in test
      setup only, no production default added); round 9: the synthetic full-flow regression was
      still failing on other unrelated `__init__` fields and was DELETED as redundant per review —
      the class-level invariant is proven directly and full-flow `__new__`-instance compatibility
      is supplied by the pre-existing `test_add_episode_combined_route_wiring_end_to_end`; final
      Codex acceptance run: ALL PASS (focused pytest `27 passed, 1 warning`, cumulative
      `537 passed, 11 skipped, 3 warnings`, Ruff check PASS, Ruff format PASS 5 files already
      formatted, Pyright 0/0/0, artifact validation `10 records, 0 findings`, `git diff --check`
      exit 0) — see Final Codex Acceptance Run above

## Assumptions

- `_extract_and_resolve_nodes` in `graphiti.py` has no in-repo callers (grep-verified); it was
  updated anyway so the hook contract holds for any future/experimental caller, and its lack of
  edge evidence (nodes resolve before edges extract in that flow) is documented.
- `_resolve_nodes_and_edges_bulk` receives `edges_by_episode` (deduped extracted edges) and those
  are the appropriate "already-available extracted/resolved edge evidence" for that boundary; the
  earlier `dedupe_nodes_bulk` boundary uses the pre-dedup extracted edges.
- The context exposes exactly the six fields the task lists; no extra fields were added, so no
  additional justification is required.
- `evaluate_identity_decision` is a module-level helper for strict return validation, consistent
  with the extraction hook's strict-return/propagating-exception contract.
- Spec'd mocks (`Mock(spec=...)`) are the repo-compatible way to satisfy `GraphitiClients` runtime
  validation without real clients or a DB; `NodeNamespace`/`EdgeNamespace`/tracer construction on
  that path is side-effect free (verified by reading the constructors).

## Risks / Gaps

- Post-correction execution results are pending Codex's re-run; the worker's only post-correction
  checks are inspection plus `ast.parse` syntax sanity.
- The `add_episode`/`add_triplet` wiring tests stub module-level functions
  (`extract_nodes_and_edges`, `resolve_extracted_nodes`, `search`, ...); they are behavior-scoped
  and assert on actual kwarg names, so future refactors fail loudly but will need test updates.
- Pyright on the spec'd-mock constructor pattern was NOT RUN by the worker; `Mock(spec=...)` is
  runtime-validated but if Codex's Pyright configuration flags the `Graphiti(graph_driver=Mock(...))`
  call sites, a typed stub or `cast` may be needed.
- Formatter/typechecker equivalence NOT RUN (prohibited); the hand-format targeted the exact files
  Ruff flagged, but Codex should confirm `ruff format --check` reports 5 already-formatted files.

## Follow-Up Tasks

- Codex: rerun the focused and cumulative no-DB suites, Ruff check/format, Pyright,
  `artifact_validate` on this wrapup, and create the Phase D2 product commit (worker is
  git-prohibited; all work is UNCOMMITTED).
- Phase F: wire Menhir's positive-identity policy (exact/substring/acronym/Jaccard, edge-fact
  consistency, warning text) on top of this hook and remove installer #12's Menhir-side
  monkeypatch. Do not begin #14/#15/#16/#17 or Phase F as part of this phase.
