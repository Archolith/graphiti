# Changelog — graphiti

## 2026-09-27 — Publish the Archolith Graphiti 0.30.2 fork

- Published `archolith-graphiti-core==0.30.2.post1` from merged fork commit
  `dbb0e33fbf7fdfbbe66bd1de7e2942d2dc4198bd` and tag `v0.30.2.post1`.
  The distribution keeps the `graphiti_core` import name and contains the
  native Menhir hooks; its public wheel SHA-256 is
  `a748f98e0b09d64ab1eb29bd3449f52b552250a31663e86c4d99dc51ddf991f0`.
- Declared the wheel's package path explicitly, updated the root lock, and
  verified package metadata and hook files in the OIDC publishing workflow.
- Moved the fork's Socket Firewall check to available hosted runners and tested
  the public no-key fallback while retaining keyed-path checks when a key exists.
- Added a concise public `CHANGELOG.md` and linked it from the README after
  publication. The already-published tag and wheel remain unchanged.

## 2026-09-26 — Reconcile maintenance fork with stable Graphiti 0.30.2

- `pyproject.toml`, `uv.lock`: declare the directly imported `httpx` dependency;
  clean CI installs no longer obtain it transitively from newer OpenAI releases.

- Merge upstream v0.30.2 (`eaa4128681bc53487138a4bbc22d58336ebe70d2`) into
  `menhir/0.30.2`, preserving the existing native extraction, identity, candidate,
  pre-resolution and OpenAI request-guard changes.
- `graphiti_core/graphiti.py`: carry request-scoped clients through fork extraction
  routes and bulk deduplication; preserve upstream concurrency/database routing.
- Routing and hook tests: adapt persistence fakes to the upstream client argument;
  exercise non-default database extraction through persistence and interleaved hooks.
- `.github/workflows/menhir-compatibility.yml`: service-free compatibility and build
  gate for maintenance branches using hosted runners.
- README, architecture and conventions: update the maintained upstream baseline.
- Local verification: 366 focused fork/upstream regression tests passed; scoped
  Pyright passed. Live graph/model quality is not established by this check.

## 2026-09-14 — Phase E: OpenAI-compatible request guard + context-length normalization (fork mechanism half of installer #17)

- `graphiti_core/llm_client/request_guard.py`: NEW generic, policy-free request-guard
  module. Conservative documented request sizing (`CHARS_PER_TOKEN = 3`, ceiling
  division, minimum 1) over the fully assembled provider messages immediately before
  send; `RequestCeilingResolver` seam resolves the per-request effective ceiling
  (`None` = no ceiling, `0` = deliberate opt-out that stays disabled, positive int =
  enforced pre-send); `enforce_request_ceiling` raises the public
  `GraphitiRequestTooLargeError` (D5) BEFORE the provider call with structural
  diagnostics only (message count, total chars, largest index/role/char tuples —
  never prompt content); `is_context_length_error` classifies provider context-limit
  rejections narrowly (structured `code` and `body.error.code` inspected
  INDEPENDENTLY so a nested `context_length_exceeded` classifies even under a
  generic top-level code such as `invalid_request_error`, plus the textual
  `context_length_exceeded` / `maximum context length` fallbacks); frozen
  `LLMRequestContext` carries request-scoped structural facts including
  `group_id` (a partition/namespace, NOT a per-episode key) and `prompt_name` (an
  operation name), plus an opaque `correlation_id` consumers supply per request (via
  the correlation provider below — never by writing the measured context);
  bounded `LLMResponseMetadata` (provider status, response id, raw length,
  240-char raw-response preview, duration) delivered to hooks so consumers can
  reproduce their own diagnostics without fork logging policy; immutable
  `RequestGuard` bundles optional `ceiling_resolver`, `lifecycle_hook`
  (`on_request_started`/`on_request_completed(context, response)`), and
  `failure_listener` (`on_request_failed(context, error, phase, response)`) typed
  protocols, plus a `correlation_provider` (`resolve_correlation(context) ->
  str | None`) invoked once per request — a deliberately narrow correlation seam:
  the fork creates the final context via `dataclasses.replace(correlation_id=...)`
  and a non-string return is ignored, so consumer code cannot alter fork-measured
  structural sizing facts — all for the
  Menhir-owned halves (ceiling derivation policy, failure diagnostics, lifecycle
  telemetry) to be wired in Phase F. No module-global mutable request state, no
  `ContextVar`, no symbol rebinding.
- `graphiti_core/llm_client/openai_generic_client.py`: `OpenAIGenericClient` gains an
  optional `request_guard` constructor parameter. With a guard, each assembled
  request is sized and ceiling-enforced before send; every provider/send/parse
  failure — rate limits, context-length normalization, empty responses, parse
  failures — is observed EXACTLY ONCE through the failure seam with the metadata
  available at that point (`response=None` for pre-send ceiling rejections and
  provider-call failures such as rate limits/transport; full metadata including
  status/id/raw preview on parse and empty-response failures); a provider
  context-length rejection (structured or textual classification) raises
  `GraphitiRequestTooLargeError` chained from the provider error and is NOT retried
  by the tenacity wrapper (`is_server_or_retry_error` does not match it), so
  deterministic oversized payloads never enter an unchanged retry loop; D5's
  bisection catches it; translated Graphiti `RateLimitError` and retry behavior are
  preserved. Rejected requests emit `failure_listener('ceiling_rejected')` and never
  `on_request_started`. Also: responses with no `choices` raise a clear
  `EmptyResponseError` instead of `IndexError`; prose-wrapped JSON is normalized by
  a left-to-right `json.JSONDecoder.raw_decode` scan returning the FIRST decodable
  object/array — robust against later brace fragments, multiple payload-looking
  spans, and trailing malformed braces (unparseable output still raises the original
  `JSONDecodeError`, keeping retry classification unchanged); gpt-5*/o1/o3/o4 models
  send `max_completion_tokens` instead of legacy `max_tokens`; a subclass tenacity
  retry wrapper (mirroring the base configuration) threads request-scoped
  `group_id`/`prompt_name` to `_generate_response`. With no guard the client behaves
  exactly as before: unchanged `json_schema`/`json_object` handling, code-fence
  tolerance, empty-response behavior, tracing, multilingual instructions,
  attribute-extraction framing, and retry semantics for genuinely retryable
  failures.
- `tests/llm_client/test_request_guard.py`: NEW. Estimation/diagnostics units;
  disabled (`None`/`0`), within-ceiling, and oversize-rejection semantics with
  structural diagnostics and the exact public exception; narrow context-length
  classification (structured and textual positives; nested `body.error.code`
  classification under a generic top-level code; unrelated bad requests,
  parse/schema/auth failures, non-dict bodies negative).
- `tests/llm_client/test_openai_generic_client_guard.py`: NEW no-network suite:
  pre-send rejection with zero provider calls; exact exception type; no unchanged
  retry for both pre-send and provider context-length failures; disabled/within-
  ceiling behavior; structured/text provider classification; resolver context
  contents; no-guard and empty-guard legacy compatibility (wire shape, `max_tokens`
  kwarg); lifecycle started/completed with bounded response metadata (status, id,
  raw length/preview, duration) and suppression on rejection; failure-metadata seam:
  parse/empty-response failures expose status/id/raw length/bounded preview exactly
  once, rate-limit failures are observed exactly once with the translated Graphiti
  error and `response=None`, context-length failures observed exactly once before
  normalization, ceiling rejections carry no response metadata; correlation
  provider populates `correlation_id` for all hooks with no cross-request leakage,
  and a hostile test proves a tampered non-string return cannot alter any measured
  fact (model/endpoint/counts/sizes/estimated tokens/largest messages) and leaves
  `correlation_id` None;
  adversarial payload extraction (first decodable payload before later fragments,
  invalid leading spans, multiple valid spans, trailing malformed braces, arrays,
  nested objects, end-to-end); `max_completion_tokens` vs `max_tokens` model
  families.
- `.agent/architecture.md`: NEW "OpenAI-Compatible Request Guard and Context-Length
  Normalization (fork half of installer #17)" section, separating the generic fork
  mechanism from Menhir policy.
- Scope: installer #17 Graphiti-side mechanisms only. Ceiling derivation policy
  (endpoint probing/caching), failure diagnostics, lifecycle telemetry, and the
  concise-prompt/truncation-escalation retry loop remain Menhir-owned and outside
  the fork; Menhir wiring plus installer-#17 removal are Phase F.

## 2026-09-14 — Phase D5: adaptive dedupe request bisection + neutral node pre-resolution (fork mechanism half of installer #16)

- `graphiti_core/errors.py`: NEW public `GraphitiRequestTooLargeError` (`GraphitiError`
  subclass). D5 catches only this exception during node deduplication; Phase E owns
  making real OpenAI-compatible clients raise it.
- `graphiti_core/utils/maintenance/node_operations.py`: NEW recursive
  `_resolve_unresolved_indices` helper. When the combined `_resolve_with_llm` call in
  `resolve_extracted_nodes` raises `GraphitiRequestTooLargeError`, the unresolved index
  batch bisects at its stable midpoint and resolves left then right recursively; each
  retry's dedupe candidate index is rebuilt only from the candidate pools of that subset
  (order-preserving, uuid-deduplicated); a singleton that still raises propagates
  unchanged. Search, candidate filtering, and deterministic similarity run exactly once
  before LLM escalation and are never rerun on split; resolved nodes, uuid maps, and
  duplicate pairs merge without duplicate promotions; `identity_gate_hook` and
  `identity_gate_edges` are forwarded unchanged to every initial and retry LLM call. The
  no-error one-request path is behavior-identical.
- `graphiti_core/node_pre_resolution.py`: NEW neutral typed pre-resolution hook module.
  `NodePreResolutionHook` runs exactly once per extracted node before semantic candidate
  search, receives a frozen borrowed `NodePreResolutionContext` (extracted node,
  `GraphitiClients` access, episode, previous episodes, entity types, request-local edge
  evidence when available), and must return a frozen `PreResolutionResult`: `DEFER`
  (with `resolved_node=None`) leaves ordinary resolution; `RESOLVE` carries the fully
  resolved `EntityNode`. Validation is strict — a bare `EntityNode`, any non-result
  type, any unknown decision, or an invalid decision/payload combination (DEFER with a
  node, RESOLVE without an EntityNode) raises `TypeError`; hook exceptions propagate.
  Pre-resolved nodes are excluded from search, candidate filter, deterministic
  similarity, and the dedupe LLM, committed to final state/uuid map exactly once, and
  record a duplicate pair exactly when the resolved UUID differs; ordinary nodes are
  searched once and realigned to original order. Edge evidence travels a dedicated
  `node_pre_resolution_edges` channel (never `identity_gate_edges`), used only when the
  hook is configured.
- `graphiti_core/graphiti.py`: `Graphiti` gains an optional `node_pre_resolution_hook`
  constructor parameter plus a class-level typed default (so `__init__`-bypassing
  instances observe "no hook"), forwarded — together with the dedicated
  `node_pre_resolution_edges` evidence channel — at every `resolve_extracted_nodes` call
  path and to `dedupe_nodes_bulk` only when configured. No hook keeps the exact legacy
  resolver shapes; `identity_gate_edges` remains an independent channel forwarded only
  with the identity hook.
- `graphiti_core/utils/bulk_utils.py`: `dedupe_nodes_bulk` gains matching optional
  `node_pre_resolution_hook` and episode-indexed `node_pre_resolution_edges` pass-throughs
  to its first-pass `resolve_extracted_nodes`.
- `tests/test_node_pre_resolution.py`: NEW focused no-DB D5 suite: no-error single LLM
  call; 4-way batch fail-then-stable-2+2; recursive 4→2→1 splitting; singleton rethrow
  identity; subset candidate isolation/order; search/filter/deterministic exactly once
  across splits; D2 identity hook + edge evidence preserved in split batches; hook
  DEFER/RESOLVE result-object contract (strict validation, bare EntityNode rejected);
  exception propagation; different-UUID duplicate bookkeeping; withholding from
  search/filter/LLM; independent `node_pre_resolution_edges` channel vs
  `identity_gate_edges`; constructor/class defaults; all
  public/bulk wiring and legacy no-hook shapes; no Menhir imports/policy strings.
  Verification: NOT RUN by the worker (Codex owns tests, static checks, git, grading,
  and publication).
- `.agent/architecture.md`: NEW "Adaptive Dedupe Request Bisection and Node
  Pre-Resolution (native, fork half of installer #16)" section; generic
  bisection/pre-resolution mechanisms are explicitly separated from Menhir
  canonical-self/telemetry policy, with policy wiring plus installer removal deferred to
  Phase F and exception-raising owned by Phase E.
- Scope: installer #16 Graphiti-side mechanisms only. No Menhir predicates/names in
  runtime code. Menhir canonical-self policy wiring and installer-#16 removal are Phase
  F; raising `GraphitiRequestTooLargeError` from real OpenAI-compatible clients is
  Phase E.

## 2026-09-14 — Phase D4: untyped attribute preservation in extraction (fork half of installer #15)

- `graphiti_core/utils/maintenance/node_operations.py`: `_extract_entity_attributes` no longer
  returns `{}` when no typed attribute schema applies (`entity_type is None` or the schema has
  no `model_fields`). It now returns a defensive shallow dict copy of `node.attributes`,
  treating absent/falsy attributes as empty, with no LLM call in that path. Because
  `extract_attributes_from_nodes` assigns the returned dict back to `node.attributes`,
  pre-existing externally owned properties survive replacement-save persistence, and the
  returned mapping never aliases the original. Typed-schema behavior is unchanged (context,
  LLM call, capped overlay merge via `apply_capped_attributes`, shape validation, return).
  This is the Graphiti-side *mechanism* replacement for Menhir installer #15
  (`_patch_graphiti_untyped_attribute_preservation`, which wrapped `_extract_entity_attributes`);
  Menhir-side patch removal is deferred to Phase F.
- `tests/utils/maintenance/test_untyped_attribute_preservation.py`: NEW focused no-DB D4 suite
  covering: entity_type=None preservation with no LLM call; distinct shallow copy (no aliasing,
  caller mutation does not touch the node); empty-fields Pydantic model; empty/absent attributes;
  the public `extract_attributes_from_nodes` path (typed + untyped nodes in one call); and
  typed-schema LLM call with overlay semantics (LLM-omitted fields keep prior values).
  Verification: NOT RUN by the worker (Codex owns tests, static checks, git, grading, and
  publication).
- `.agent/architecture.md`: NEW "Untyped Attribute Preservation in Attribute Extraction (fork
  half of installer #15)" section.
- Scope: installer #15 only. No Menhir predicates/names in runtime code; installers #16
  (adaptive dedupe) and #17 remain out of scope. Menhir-side patch removal is Phase F.

## 2026-09-14 — Phase D3: native neutral candidate-filter hook for dedupe candidate pools (fork mechanism half of installer #14)

- `graphiti_core/candidate_filter.py`: NEW neutral typed extension hook module.
  `CandidateFilterHook` is a runtime-checkable protocol invoked exactly once per unique
  merged dedupe candidate per extracted node — after semantic search results and
  `existing_nodes_override` have been merged and deduplicated (`_merge_candidate_nodes`)
  and before deterministic exact/fuzzy handling or LLM candidate indexing. The hook
  receives a frozen `CandidateFilterContext` (borrowed request-local extracted
  `EntityNode` and unique merged candidate `EntityNode`; shared, not copied, and
  read-only by contract) and must return `CandidateFilterDecision.INCLUDE` (keep the
  candidate in the pool) or `CandidateFilterDecision.EXCLUDE` (remove it from that
  extracted node's pool). Order is preserved; the same candidate is evaluated once per
  different extracted node; when all candidates for an extracted node are excluded, the
  ordinary no-candidate behavior applies (node kept as new) and neither the dedupe LLM
  nor an installed identity-gate hook runs for it. Any other return value raises
  `TypeError`; hook exceptions propagate with no state to reset. This is the
  Graphiti-side *mechanism* replacement for Menhir installer #14
  (`_patch_graphiti_structural_candidate_isolation`, which wrapped
  `node_operations._collect_candidate_nodes` after merge/dedup); Menhir's
  structural/View predicate (`structure_role`/`is_view`/`view_kind`/`view_class`,
  logging counts) stays outside the fork and is wired on top during Phase F.
- `graphiti_core/utils/maintenance/node_operations.py`: `resolve_extracted_nodes` gains
  an optional `candidate_filter_hook` parameter; filtering runs in `_apply_candidate_filter`
  immediately after `_collect_candidate_nodes`, once per unique candidate per extracted
  node, before any resolution. Plain per-call argument; no module globals, no
  `ContextVar`, no symbol rebinding.
- `graphiti_core/graphiti.py`: `Graphiti.__init__` gains `candidate_filter_hook:
  CandidateFilterHook | None = None` (class-level `candidate_filter_hook:
  CandidateFilterHook | None = None` default overridden per instance by `__init__`, so
  `__new__`-constructed subclasses/test doubles safely observe "no filter"). Wired at
  every `resolve_extracted_nodes` call path with conditional forwarding — single
  `add_episode`, `_extract_and_resolve_nodes`, `_extract_and_dedupe_nodes_bulk`,
  `_resolve_nodes_and_edges_bulk`, and `add_triplet`; with no hook configured the kwarg
  is omitted entirely (never passed as `None`) so every call path keeps the exact legacy
  signature. Composition with D2 is independent: the candidate-filter kwarg and the
  identity-gate kwargs are each forwarded exactly when their own hook is configured,
  and `identity_gate_edges` is forwarded only when the identity hook is present.
- `graphiti_core/utils/bulk_utils.py`: `dedupe_nodes_bulk` gains an optional
  `candidate_filter_hook` pass-through to its first-pass `resolve_extracted_nodes`
  calls; with no hook configured, no kwarg is forwarded.
- `tests/test_candidate_filter.py`: NEW focused no-DB suite (31 test functions) covering
  no-hook compatibility and exact legacy delegate shapes at add_episode, add_triplet,
  bulk extract/dedupe, bulk resolve, and `dedupe_nodes_bulk` boundaries; filtering of
  both search candidates and `existing_nodes_override`; merge/dedup occurring before the
  hook runs (duplicate search+override candidate seen once); order preservation into the
  LLM candidate index; exactly-once per unique candidate; INCLUDE/EXCLUDE semantics;
  same candidate evaluated once per different extracted node; all-excluded no-candidate
  behavior with no dedupe LLM and no identity-gate invocation; filter applied before
  deterministic exact resolution (exact-name candidate excluded → node stays new,
  LLM not called) and its INCLUDE twin resolving deterministically without the LLM;
  strict invalid-return `TypeError`; hook exception propagation; no cross-call
  leakage; protocol runtime-checkability; frozen-context reassignment rejection;
  `Graphiti.__new__` class-level default; real constructor storage; D2 composition
  (both hooks forwarded together; candidate filter alone → no identity kwargs); and a
  structural AST check that the mechanism imports no `menhir` module. Verification: NOT
  RUN by the worker (Codex owns tests, static checks, git, grading, and publication).
- `.agent/architecture.md`: NEW "Candidate Filter for Node-Dedupe Candidate Pools"
  section (filter boundary/order, borrowed-data semantics, no-hook compatibility rule,
  D2 composition, policy exclusions, Phase F remainder).
- Scope: Graphiti-side mechanism of installer #14 only. No Menhir predicate entered the
  fork; installers #15 (untyped attribute preservation), #16 (adaptive dedupe), and #17
  remain out of scope. Menhir's installer #14 structural/View predicate is removed from
  the runtime side in Phase F.

## 2026-09-14 — Phase D2: native neutral identity-gate hook for LLM-proposed node merges (fork mechanism half of installer #12)

- `graphiti_core/identity_gate.py`: NEW neutral typed extension hook module. `IdentityGateHook` is a
  runtime-checkable protocol invoked exactly once per valid LLM-proposed node merge during dedup —
  after normalized `NodeResolutions` are available and before `_promote_resolved_node` or any
  resolved-state/uuid-map/duplicate-pair mutation; never for negative (`-1`) decisions, invalid
  candidate ids, invalid/duplicate relative ids, deterministic exact/similarity resolution, or paths
  with no LLM-proposed merge. The hook receives a frozen `IdentityGateContext` — borrowed
  request-local evidence (extracted node, candidate node, `candidate_id`, episode when available,
  previous episodes, request-local extracted/precomputed `EntityEdge` evidence) that is shared, not
  copied, and must not be mutated — and must return `IdentityGateDecision.ALLOW` (ordinary
  promotion) or `IdentityGateDecision.VETO` (ordinary no-duplicate behavior). Any other return value
  raises `TypeError`; hook exceptions propagate with no state to reset. This is the Graphiti-side
  *mechanism* replacement for Menhir installer #12 (`_patch_graphiti_dedup_identity_gate`, which
  wrapped `_resolve_with_llm` and temporarily replaced `llm_client.generate_response`); Menhir
  policy stays outside the fork until Phase F.
- `graphiti_core/utils/maintenance/node_operations.py`: `resolve_extracted_nodes` and
  `_resolve_with_llm` gain optional `identity_gate_hook` and `identity_gate_edges` parameters (plain
  per-call arguments; no module globals, no `ContextVar`, no `generate_response` wrapping). The hook
  is consulted only on the valid proposed-merge branch.
- `graphiti_core/graphiti.py`:   `Graphiti.__init__` gains `identity_gate_hook: IdentityGateHook | None
  = None` (a class-level `identity_gate_hook: IdentityGateHook | None = None` default is overridden
  per instance by `__init__`, so subclasses/test doubles/unpickled-style instances that bypass
  `__init__` safely observe "no hook" and keep the legacy no-hook signature; default absent keeps
  prior behavior exactly).
  Wired at every `resolve_extracted_nodes` call site with conditional forwarding: when a hook is
  configured, single `add_episode` passes the combined/hook `precomputed_edges` as edge evidence
  (`None` on the separate route, where edges legitimately do not exist yet at node resolution),
  bulk `_resolve_nodes_and_edges_bulk` passes each episode's deduped `edges_by_episode`,
  `add_triplet` passes the hook with no episode and no edge evidence, and the unused
  `_extract_and_resolve_nodes` helper (no in-repo callers) passes the hook with no edge evidence;
  when no hook is configured, none of the identity-gate kwargs are forwarded and every call path
  keeps the exact legacy resolver/dedupe signature (so existing wrappers and test doubles are
  unaffected).
- `graphiti_core/utils/bulk_utils.py`: `dedupe_nodes_bulk` gains optional `identity_gate_hook` and
  episode-indexed `extracted_edges` pass-throughs so the hook sees each episode's already-extracted
  edge evidence as ordinary arguments; with no hook configured, neither kwarg is forwarded.
- `tests/test_identity_gate.py`: NEW focused no-DB suite (27 test functions / 27 collected outcomes)
  covering absent-hook
  compatibility (promotion preserved), allow and veto on valid merges, exactly-once invocation per
  merge, no invocation for `-1`/invalid candidate id/out-of-range or duplicate relative ids/
  deterministic exact and fuzzy paths/no-candidate paths, full context evidence (node identity,
  candidate id, episode, previous episodes, edges) and no cross-call leakage, strict invalid-return
  `TypeError`, hook exception propagation, protocol runtime-checkability, frozen-context
  reassignment rejection, Graphiti constructor storage, DB-free public wiring tests for single
  `add_episode` (hook + precomputed edges),   `add_triplet` (hook, no episode/edges), bulk
  (`_extract_and_dedupe_nodes_bulk` hook + episode edges, `dedupe_nodes_bulk` pass-through with both
  populated evidence, and no-hook legacy-signature delegation tests proving both new kwargs are
  entirely absent on the default path at the add_episode, add_triplet, bulk-dedupe, and
  dedupe_nodes_bulk boundaries), a regression test proving `Graphiti.__new__(Graphiti)` observes no
  hook (full-flow compatibility for such instances through public `add_episode` is supplied by the
  pre-existing `tests/test_extraction_routing.py::test_add_episode_combined_route_wiring_end_to_end`),
  and a structural AST check that the mechanism
  imports
  no `menhir` module.
- `.agent/architecture.md`: NEW "Identity Gate for LLM-Proposed Node Merges" section (invocation
  lifecycle, decision semantics, borrowed-data/mutation rules, per-flow edge-evidence availability,
  default compatibility, Phase F remainder).
- Scope: Graphiti-side mechanism of installer #12 only. No Menhir policy (identity heuristics, edge-
  fact mention policy, warning text, receipts, telemetry) entered the fork; installer #12's Menhir
  side is removed in Phase F. Verification: NOT RUN by the worker (Codex owns tests, static checks,
  git, grading, and publication). Codex round-1 review: focused pytest `4 failed, 19 passed,
  1 warning` (23 collected), Ruff check 4 errors (3 import-order, 1 unused variable), Ruff format
  2 files reformattable, Pyright 2 errors; all corrected by the worker (tests use the real
  `Graphiti` constructor with spec'd doubles and the flattened candidate index, imports reordered,
  frozen-context test uses dynamic `setattr`, exact counts recorded). Codex round-2 review: focused
  pytest `1 failed, 22 passed, 1 warning`, Ruff check 1 B010 error, Ruff format 2 files
  reformattable, Pyright 1 error; all corrected (stub signature, `cast(Any, ...)` frozen pattern,
  formatter shapes, pre-configured tracer Mock). Codex round-3 cumulative review: cumulative
  no-external-DB suite `3 failed, 530 passed, 11 skipped, 3 warnings` — pre-existing tests failed
  because the no-hook path still forwarded identity-gate kwargs, violating absent-hook
  compatibility; fixed by conditional kwargs forwarding (kwargs omitted entirely when no hook is
  configured) at every Graphiti/bulk call path, with new legacy-signature delegation tests.
  Repository-wide Ruff PASS at round 3. Round 4: 3 focused test-fixture failures (independent edge
  objects) fixed in tests; focused Ruff/format/Pyright PASS. Round 5: 2 focused test failures
  (nested-list identity level) fixed in tests; Ruff/format/Pyright PASS. Round 6: cumulative suite
  `1 failed, 535 passed, 11 skipped, 3 warnings` — `Graphiti.__new__`-style doubles hit an
  AttributeError reading `identity_gate_hook`; fixed with a class-level typed default overridden by
  `__init__`, plus a direct regression test. Round 7–8: two focused failures were setup gaps in the
  new synthetic full-flow regression (missing unrelated `__init__` fields); per review, the
  synthetic test was deleted in round 9 as redundant — the class-level invariant is proven directly
  and full-flow `__new__`-instance compatibility is already covered by the pre-existing
  `test_add_episode_combined_route_wiring_end_to_end`. Codex re-run pending.

## 2026-09-14 — Phase D1: native anti-conflation counterexample in per-entity dedup prompts (fork half of installer #11)

- `graphiti_core/prompts/dedupe_nodes.py`: both active per-entity dedup prompts (`node` and `nodes`,
  including the `versions['node']`/`versions['nodes']` entries, which already point directly at the
  functions) now render Menhir's anti-conflation counterexample exactly once in their `<EXAMPLE>`
  blocks: NEW ENTITY `"the suburbs"` against an existing `Chicago` (Location, summary "A city where
  someone lives") resolving to `duplicate_candidate_id = -1`, explaining that relative/descriptive
  locations (the suburbs, downtown, countryside) are never the same object as a specific named city
  merely because they are related or appear in movement context. Rendered JSON braces match the
  surrounding native examples; the escaped-brace artifacts of the Menhir runtime patch
  (`_patch_graphiti_dedup_prompt`) were not carried over. Existing examples and the response contract
  are unchanged; `node_list` (the separate UUID-grouping prompt) is deliberately untouched — it is
  outside installer #11. This implements the policy directly in the existing prompt functions: no
  wrappers, no runtime rebinding, no identity-gate logic (#12), no other Menhir policy.
- `tests/test_dedupe_nodes_prompt.py`: NEW focused suite (10 tests) covering both direct prompt
  functions and the `versions` entries: the counterexample renders exactly once in each, the
  `versions` map keeps its original shape and function identity, `node_list` shows no leakage of the
  new example, existing examples and the response contract remain intact, and no prompt function
  mutates its context argument.
- `.agent/architecture.md`: NEW "Dedup Prompt Anti-Conflation Policy" section. Scope: fork half of
  installer #11 only; Menhir-side runtime patch removal remains Phase F. Verification: NOT RUN by the
  worker (Codex owns tests, static checks, git, grading, and publication).

## 2026-09-14 — Phase C1g: native single-episode combined-extraction routing + neutral extraction hook (fork half of installer #1)

- `graphiti_core/graphiti.py`: `add_episode` now routes ordinary single-episode extraction through the existing
  combined extractor (`combined_extraction.extract_nodes_and_edges`) natively — one LLM call for both nodes and
  edges — replacing the separate `extract_nodes` then `extract_edges` sequence. This is the **fork half** of Menhir
  installer #1 (`_patch_graphiti_combined_extraction`); the Menhir policy wiring and removal of installers #1/#2
  remain Phase F. The combined default is a deliberate fork divergence from upstream v0.29.3 (whose hook-absent
  behavior is always the separate path). Route selection lives in new `Graphiti._extract_single_episode`: default
  route is `COMBINED`, with a required `SEPARATE` fallback when custom edge schemas (`edge_types`) are supplied —
  a deliberate compatibility boundary that keeps custom-schema episodes on the exact pre-C1g upstream/installer
  path (NOT because attribute handling is missing: combined-route edges also pass through
  `resolve_extracted_edges`, which performs the downstream typed-attribute work). Combined/hook edges are carried
  into resolution via a new
  `precomputed_edges` keyword on `Graphiti._extract_and_resolve_edges` (which skips `extract_edges` when set) —
  plain per-call arguments, no module-symbol rebinding, no `ContextVar`, no cross-request leakage, nothing to
  reset on exception or cancellation. The `add_episode` span gains an `extraction.route` attribute (`combined`,
  `separate`, or `hook`). Bulk routing is unchanged.
- `graphiti_core/extraction_routing.py`: NEW neutral typed extension hook module.
  `SingleEpisodeExtractionHook` is a runtime-checkable protocol invoked once per `add_episode` call with a frozen
  `SingleEpisodeExtractionContext` — borrowed request-local inputs (clients, episode, previous episodes,
  entity/edge type maps, custom instructions); the container is frozen but its contents are shared with the call
  and hooks must not mutate them (no runtime immutability is enforced). Returns: `None` (default routing),
  `ExtractionRoute` (`COMBINED`/`SEPARATE` forced; Graphiti
  still extracts), or `SingleEpisodeExtractionResult` (nodes/edges/index-map supplied by the hook; Graphiti skips
  its own extraction and resolves/persists them natively). Any other return value raises `TypeError`; hook
  exceptions (including `asyncio.CancelledError`) propagate with no state to reset. The hook parameter defaults to
  absent; the fork's combined default route itself is the new hook-absent behavior (a divergence from upstream).
  The hook carries no Menhir receipt, canonical-self, repair, marker, grounding,
  titled-list, counter, scheduler, or telemetry policy — it is deliberately policy-free for Phase F to build on.
- `tests/test_extraction_routing.py`: NEW focused suite (17 tests) covering default route selection (combined for
  `None`/`{}`, separate for custom edge schemas), hook route forcing in both directions, hook-provided results
  skipping built-in extraction, request-local context delivery (identity of the edge-type map asserted), invalid
  hook returns (`TypeError`), exception and
  cancellation propagation with a still-usable instance, concurrent routing isolation across two instances,
  precomputed edges skipping `extract_edges` while the separate fallback still calls it, a DB-free public
  `add_episode` wiring test proving the combined route invokes the combined extractor, carries precomputed edges
  into `resolve_extracted_edges`, never calls `extract_nodes`/`extract_edges`, and records the
  `extraction.route=combined` span attribute, a structural AST check that the routing mechanism imports no
  `menhir` module, and protocol runtime-checkability. No DB required.
- `.agent/architecture.md`: NEW "Single-Episode Extraction Routing" section (route policy, hook contract, edge
  carrying, state semantics, upstream divergence disclosure); bootstrap-state line updated. `.agent/data_models.md`:
  NEW routing contract entry.
- Scope: fork half of installer #1 only. Follows C1a–C1f. Menhir-side runtime patches (installers #1/#2 included)
  are NOT removed yet (Phase F). Verification: the worker ran no tests/tools (Codex owns verification and git).
  Codex final acceptance run (post-correction): focused pytest `17 passed, 1 warning`; Ruff check on the three
  changed Python files PASS; Ruff format `3 files already formatted`; changed-file Pyright 3 files,
  0 errors/warnings/info; cumulative no-external-database suite `500 passed, 11 skipped, 3 warnings`;
  repository-wide Ruff PASS; artifact validation `8 records, 0 findings`; `git diff --check` exit 0 (informational
  LF/CRLF warnings only). Change boundary: six product files (`graphiti_core/graphiti.py`,
  `graphiti_core/extraction_routing.py`, `tests/test_extraction_routing.py`, `.agent/architecture.md`,
  `.agent/data_models.md`, `.agent/CHANGELOG.md`) plus the separate wrapup file; harness task file absent.
- Review Corrections (Codex independent review round 1, 2026-09-14): (1) P1 test fixes — the context test now
  passes its own edge-type map through to the routing call so the identity assertion is meaningful; both
  edge-resolution tests define `resolve_edge_pointers` as a plain sync function (the real function is sync; async
  stubs leaked unawaited coroutines); the no-Menhir prose-token scan was replaced with a structural AST check
  (imports only; policy-neutral documentation may name excluded concepts). (2) P2 module contract — the new
  `extraction_routing.py` previously had two consecutive top-level string literals (the second a dead expression,
  triggering 7x E402 and losing `__doc__`); the license header and module documentation are now one real
  docstring. (3) P2 factual fix — installer #1 is `_patch_graphiti_combined_extraction`, not
  `_patch_graphiti_add_episode_combined`; corrected everywhere. (4) P2 compatibility claim corrected — hook-absent
  behavior deliberately changes from upstream's separate path to the fork's combined default; "public defaults
  unchanged" claims removed; the hook parameter defaults to absent while the route default is the fork's own.
  (5) P2 fallback rationale corrected — the SEPARATE custom-schema route is a deliberate compatibility boundary;
  combined edges do reach `resolve_extracted_edges` (typed-attribute work happens there), so the previous
  "machinery absent" claim was false and is removed from module docstring, architecture, data models, changelog,
  and wrapup. (6) P2 context overclaim corrected — `SingleEpisodeExtractionContext` is documented as borrowed,
  request-local inputs over a frozen container with shared mutable contents (no enforced immutability); hooks must
  not mutate them. (7) P2 coverage — added `test_add_episode_combined_route_wiring_end_to_end`, a DB-free mocked
  public `add_episode` test proving wiring, edge carrying, no separate extractor on the ordinary route, and the
  span attribute; test count 16 → 17. (8) P3 — structural menhir-import check replaces token scanning.
  (9) P3 — `graphiti_core/graphiti.py` and the test file hand-adjusted toward Ruff format style.
- Review Corrections (Codex independent review round 2, 2026-09-14): (10) P2 test determinism —
  `test_default_policy_routes_combined` compared `index_map` against a fresh `_combined_fixture()[2]` with new
  UUIDs; it now asserts `{node.uuid: [0] for node in nodes}` over the nodes returned in the same result.
  (11) P2 Pyright — the integration test's `fake_process_episode_data` stub renamed its first parameter `ep` to
  `episode` and was given the full compatible parameter list
  (`episode, nodes, entity_edges, now, group_id, saga=None, saga_previous_episode_uuid=None,
  node_episode_index_map=None`) so instance-attribute assignment matches the real method signature.
  (12) P3 exact formatter output — the `add_episode` unpacking assignment rewritten to Ruff format's exact
  tuple-shape (`(a, b, c, d) = await ...` with one element per line), and the bare-LF line ending on
  `_extract_and_resolve_edges`'s   `Returns` docstring line normalized to the file's CRLF endings (zero bare-LF
  lines remain in the file). Cumulative review tally: 12 corrected findings — 1x P1, 8x P2, 3x P3; all closed,
  no open findings. Codex final acceptance verification recorded in the Scope bullet above.

## 2026-09-14 — Phase C1f: native entity-record tolerance + neutral group-id resolver hook (provider compatibility)

- `graphiti_core/nodes.py`: `get_entity_node_from_record` now implements the **fork half** of Menhir installer #6
  (`_patch_graphiti_entity_record_group_id`) natively — labeled `provider compatibility`; the Menhir namespace /
  group-id policy itself is NOT in the fork and remains for Phase F. The outer record is defensively copied before
  any mutation; dict `attributes` are copied before the upstream key-pops and non-None `labels` are copied to a
  fresh list (including the `Entity_<group>` prefix strip), so caller-owned records, nested attributes, and label
  containers are never mutated. KUZU JSON-string `attributes` handling and valid-record behavior are unchanged.
  When the record's `group_id` is None, a neutral process-level resolver hook is consulted:
  `set_entity_record_group_id_resolver(resolver | None)` registers (or resets) an
  `EntityRecordGroupIdResolver = Callable[[Mapping[str, Any], GraphProvider], str | None]` as startup
  configuration — not per-record monkeypatching. The resolver receives the defensive record copy plus the
  provider; a `str` return
  (including `''`) is authoritative, `None` falls back to `helpers.get_default_group_id(provider)` (FalkorDB `'_'`,
  `''` elsewhere), non-string/non-None raises `TypeError`, and resolver exceptions propagate. Non-None stored
  group ids bypass the hook unchanged. A `created_at` string ending exactly in `Z[UTC]` has only its terminal
  suffix normalized to `+00:00` before `parse_db_date`; other values pass through unchanged. Null-group and
  timestamp repairs log once per record key (uuid/name) via sets capped at 512 keys per category with arbitrary
  eviction at capacity (`set.pop()`, matching current Menhir semantics; first-seen keys always log once per
  retention window; retained repeats never re-log; evicted repeats re-log); logs carry identity and the chosen
  group/original timestamp, never attributes.
  `search/search_utils.py` imports this function object directly, so all search call sites gain the native
  behavior with no symbol rebinding. The hook is entity-record-specific by name
  (`EntityRecordGroupIdResolver` / `set_entity_record_group_id_resolver`) and the resolver's authority is
  return-value-only: it receives a read-only mapping over an isolated shallow snapshot (nested attributes dict and
  labels list copied again), so top-level mutation attempts raise on the mapping and nested mutation (if
  attempted) affects neither caller input nor the returned node.
- `tests/test_entity_record_tolerance.py`: NEW focused regression suite (21 tests) covering valid NEO4J records,
  caller non-mutation (outer dict / attributes / labels list and tuple), provider defaults for null groups without
  a resolver (NEO4J/FALKORDB), resolver contract (isolated read-only snapshot received, named-group inference,
  null-group-only invocation, reset, `None` fallback, empty-string authority, `TypeError` on non-string, exception
  propagation, non-callable rejection, return-only authority incl. nested-mutation isolation), exact `Z[UTC]`
  repair vs other timestamp handling (plain terminal `Z` still parses via `fromisoformat` with no repair
  diagnostic), diagnostics identity/dedupe/512 bound with eviction (600 unique keys → 600 first-seen logs at 512
  storage; retained repeats silent, evicted repeats re-log), search_utils native behavior without rebinding, KUZU
  string attributes, and a no-Menhir-reference source check. An autouse fixture resets the global resolver and
  diagnostic sets between tests.
- `.agent/data_models.md`: documents the Entity Record Tolerance contract (resolver hook lifecycle, copy
  semantics, timestamp repair, bounded diagnostics). `.agent/architecture.md` intentionally not extended — it has
  no extension/hooks section, so the contract stays in `data_models.md`.
- Scope: fork half of installer #6 only; the Menhir-side runtime patch remains installed until Phase F, and
  Menhir's namespace-to-group policy remains Phase F work. Tests/lint/typecheck NOT RUN by the worker
  (orchestrator owns verification and git).
- Review Corrections (Codex independent review, 2026-09-14): (1) P1 resolver authority leak — the resolver now
  receives a read-only `MappingProxyType` over a separate shallow snapshot whose attributes dict and labels list
  are copied again; return-only authority enforced, isolated from caller input and the returned node. (2) P1
  bounded log-once — `_log_once` now evicts one existing key at the 512 capacity before adding and logging a new
  key, matching the Menhir helper's eviction semantics; retained repeats stay silent, evicted repeats re-log.
  (3) P2 timestamp test — plain terminal `Z` (accepted by `datetime.fromisoformat` on Python 3.12) now asserted to
  parse successfully as an aware UTC datetime with no repair diagnostic instead of expecting `ValueError`.
  (4) P2 API rename — `set_group_id_resolver`/`GroupIdResolver` renamed to
  `set_entity_record_group_id_resolver`/`EntityRecordGroupIdResolver` everywhere so the hook is
  entity-record-specific. (5) P2 Ruff import order — `import graphiti_core.search.search_utils as search_utils`
  moved into the sorted local import block. (6) P3 count drift — test count corrected to the final static count of
  21 test functions. (7) P2 resolver snapshot assertion — the test's snapshot-attributes expectation corrected to
  `{'custom': 'value', 'sneaky': 'x'}` (Graphiti strips reserved keys from the working attributes copy before the
  resolver is invoked); source unchanged. (8) P2 arbitrary set eviction — the bound test no longer assumes which
  key `set.pop()` evicts; it derives a retained key from the actual set and an evicted key from the universe minus
  the set, asserting no log for the retained repeat, exactly one log for the evicted repeat, and bounded size 512;
  source unchanged. (9) P2 caplog isolation in the bound test — the repeat assertions now call `caplog.clear()`
  before each repeat so they examine only new records rather than all 600 previously captured ones; source
  unchanged. (10) P2 Ruff format line wrapping — the two long `_resolve(_valid_record(...))` calls in the bound
  test were wrapped exactly as standard Ruff formatting renders them; source unchanged. Cumulative review tally:
  10 corrected findings — 2x P1, 7x P2, 1x P3. Codex independent verification (post-format): focused pytest
  `21 passed, 1 warning`; Ruff format `2 files already formatted`; Ruff check PASS; changed-file Pyright
  0 errors/warnings/info; cumulative `483 passed, 11 skipped, 3 warnings`; repository-wide Ruff PASS; artifact
  validation `7 records, 0 findings`; diff check exit 0 (informational line-ending warnings only).

## 2026-09-14 — Phase C1e: native combined-extraction model sanitization (provider compatibility)

- `graphiti_core/prompts/extract_nodes_and_edges.py`: `CombinedExtraction` gains a Pydantic
  `mode="before"` validator that sanitizes malformed LLM provider rows natively. This is the
  **generic half** of Menhir installer #2 (`_patch_graphiti_combined_extraction_models`) only —
  labeled `provider compatibility`; the Menhir-specific half of installer #2 remains for Phase F.
  Non-dict top-level payloads pass through unchanged (normal Pydantic validation); dict payloads
  are copied, never mutated. Entity rows: non-dicts dropped; name resolution uses the valid
  nonblank canonical `name` else first valid nonblank alias in `entity_name` then `entity`
  (trimmed); unrecoverable rows dropped; `entity_type_id` coerced via `int()` with `-1` fallback
  on `TypeError`/`ValueError` (int(True)==1 intentionally preserved); extras dropped. Edge rows:
  non-dicts and rows with a missing/non-str/blank `source_entity_name`, `target_entity_name`,
  `relation_type`, or `fact` dropped; retained strings preserved exactly; list `episode_indices`
  filtered to non-bool ints with `[0]` fallback; non-list/missing defaults to `[0]`; extras
  dropped. Row order preserved. `extracted_entities` and `edges` stay declared required with their
  descriptions — the schema still marks both arrays required even though the validator supplies
  `[]` for missing/non-list arrays.
- `tests/test_combined_extraction_models.py`: NEW focused regression suite, 20 tests (required
  schema + descriptions unchanged; missing/null/non-list arrays to `[]`; non-dict top-level still
  fails via `ValidationError`; entity canonical/alias precedence, trimming, int coercion and `-1`
  fallback incl. bool parity, malformed-row drops, extra-field drops; edge required-field drops,
  exact string preservation, index filtering/default with a genuinely missing-key edge,
  extra-field drops; input non-mutation; mixed valid/malformed row-order preservation; behavioral
  no-endpoint-synthesis check for edges referencing unknown entities; pathlib source check that
  the module contains no Menhir import/reference).
- `.agent/data_models.md`: documents the `CombinedExtraction` sanitization contract.
- Scope: only the generic half of installer #2. Follows C1a (installers #5/#7/#8), C1b (#9/#10),
  C1c (#3), and C1d (#4). Menhir-side runtime patches are NOT removed yet (Phase F); other
  installers remain un-migrated. Tests/lint/typecheck NOT RUN by the worker (orchestrator owns
  verification).

## 2026-09-14 — Phase C: native structured summary prompts (migrates Menhir installer #4)

- `graphiti_core/prompts/summarize_nodes.py`: `summarize_context` and `summarize_pair` now implement the Menhir
  structured-summary policy natively (Menhir installer #4, `_patch_graphiti_summarize`), labeled a **Menhir policy
  divergence**. Both return exactly two messages with exact structured-output system strings and request
  `key:value` pairs separated by `' | '` with a 150-character total ceiling. `summarize_context` restricts the
  model to the provided MESSAGES only, focuses on what the entity is, role/status, and key attributes, requires
  omitting filler words, and includes a GOOD/BAD key:value-vs-prose example; `summarize_pair` merges two summaries
  keeping the most current/specific values and dropping duplicates. Context values interpolate via raw f-string
  `context.get(...)` defaults, so missing keys are tolerated without raising; `to_prompt_json` is not used in these
  two functions. DELIBERATE DIVERGENCE DISCLOSED: `summarize_context` no longer instructs attribute extraction and
  omits `context['attributes']` (upstream renders an ATTRIBUTES block), mirroring the active Menhir patch. The
  `versions` mapping points directly at the native functions — no rebinding, wrappers, sentinels, aliases, config
  surfaces, or hooks. `Summary`, `SummaryDescription`, `summary_description` (still using shared JSON
  serialization), and the `MAX_SUMMARY_CHARS`-backed response descriptions are unchanged; the now-unused
  `summary_instructions` import was removed.
- `tests/test_structured_summary_prompts.py`: NEW focused regression tests (exact two-message role order and exact
  system strings for both functions, format separator and 150-char ceiling and policy instructions, tagged-section
  interpolation, missing/empty-key tolerance, no ATTRIBUTES block even when an attributes key is supplied, raw
  pair-summary interpolation, direct `versions` identity mapping, `summary_description` JSON serialization).
- `.agent/architecture.md`: documents the structured summary policy boundary, the 150-char key:value shape, the
  direct native `versions` mapping, and the disclosed deliberate attributes-omission divergence.
- Scope: only Menhir installer #4 (`_patch_graphiti_summarize`) is migrated. Follows C1a (installers #5/#7/#8),
  C1b (installers #9/#10), and C1c (installer #3). The Menhir-side runtime patches are NOT removed yet (Phase F);
  other installers remain un-migrated. Tests/lint/typecheck NOT RUN by the worker (orchestrator owns verification).

## 2026-09-14 — Phase C: native prompt JSON serialization (migrates Menhir installer #3)

- `graphiti_core/prompts/prompt_helpers.py`: `to_prompt_json` now implements the prompt-serialization
  boundary natively (Menhir installer #3, `_patch_graphiti_prompt_json`). Before `json.dumps`, the payload
  is recursively copied/normalized: dict entries whose string key ends with `_embedding` are dropped, as
  are entries whose value is a list/tuple of length strictly greater than 64 whose first eight items are
  int/float (bool excluded) — the intentional sampled-head compatibility rule (length 64 is preserved).
  Non-JSON-native values convert via callable `isoformat`, then `iso_format`, then `to_native` (each called
  without arguments), recursing into non-primitive conversion results, with `str` fallback when a
  conversion returns the same object or no conversion applies; exceptions from malformed conversion methods
  propagate. Input is never mutated; `ensure_ascii`/`indent` pass through unchanged. No monkeypatching or
  symbol rebinding — all prompt modules and `search/search_helpers.py` inherit the behavior through the
  existing shared-helper import.
- `tests/test_prompt_json.py`: NEW focused regression tests (baseline JSON, ensure_ascii default/True,
  indent, `_embedding` key removal for non-vector values, structural removal at 65 vs preservation at 64,
  sampled-head boundary, bool/string long-list preservation, nested dict/list/tuple recursion, input
  non-mutation, isoformat/iso_format/to_native fallbacks including recursive conversion,
  self-returning conversion → str, plain unsupported object → str).
- `.agent/architecture.md`: documents the native prompt-serialization boundary and the exact
  >64/first-eight rule; bootstrap-state wording updated to reflect Phase C migrations in progress.
- Scope: only Menhir installer #3 (`_patch_graphiti_prompt_json`) is migrated. Follows C1a (installers
  #5/#7/#8) and C1b (installers #9/#10). The Menhir-side runtime patches are NOT removed yet (Phase F);
  other installers remain un-migrated. Tests/lint/typecheck NOT RUN by the worker (orchestrator owns
  verification).

## 2026-09-14 — Phase C: native response normalization for prompts (migrates Menhir installers #9/#10)

- `graphiti_core/prompts/extract_nodes.py`: `ExtractedEntity` gains a `_normalize_provider_fields` before
  model-validator (Menhir installer #9) — alias recovery only when the canonical `name` key is absent
  (`entity_name`, string `entity`, key typos like `name-`/`name_`/`Name `), degenerate
  `{<entity name>: <int type id>}` payloads, and `entity_type_id` fallbacks in installer precedence
  (`type_id`; integer `type`; present `type_name` → 0, winning over `entity_type`/`entity`; integer
  `entity_type` else 0; remaining `entity` coerced to int else 0; default 0). Missing name still raises.
  `episode_indices` deliberately KEEPS the upstream v0.29.3 default `[0]`, correcting the stale Menhir
  replacement-model default `[]` whose comment wrongly claimed it mirrored upstream.
- `graphiti_core/prompts/dedupe_nodes.py`: `NodeResolutions` gains an `_normalize_entity_resolutions` before
  model-validator (Menhir installer #10) — fresh-list default; missing/null/non-sequence top-level values
  fail safe to `[]`; non-dict and non-integer-id entries dropped (bools never accepted as ids); `name`
  null→`''`; `duplicate_candidate_id` null/non-integer (bools included)→`-1`, integer-coercible→`int`.
- `tests/test_response_model_normalization.py`: NEW focused regression tests for both validators (canonical
  passthrough, all aliases/typos, singleton mapping, type defaults, missing-name failure, upstream
  `episode_indices=[0]` correction, mixed/degenerate rows, fail-safe empties, no shared mutable defaults).
- `.agent/data_models.md`: documents the native response-normalization contract and the deliberate
  `episode_indices=[0]` correction versus the stale Menhir patch.
- Scope: only Menhir installers #9 (`_patch_graphiti_entity_extraction`) and #10
  (`_patch_graphiti_dedupe_resolutions`) are migrated. The Menhir-side runtime patches are NOT removed yet;
  other installers remain un-migrated. Tests/lint/typecheck NOT RUN by the worker (orchestrator owns
  verification).

## 2026-09-14 — Phase C: native None-hardening for models (migrates Menhir installers #5/#7/#8)

- `graphiti_core/nodes.py`: `EntityNode` gains a `coerce_none_summary` before-validator (explicit `summary=None`
  becomes `''`); `EntityNode.generate_name_embedding` and `CommunityNode.generate_name_embedding` harden a `None`
  name to `''` before embedding (Menhir installer #5 semantics, and #7 for the summary coercion).
- `graphiti_core/edges.py`: `EntityEdge` gains a `coerce_none_fields` before model-validator — explicit `None` for
  `uuid`/`episodes` drops the key so default factories run; explicit `None` for `group_id`, `name`, `fact`,
  `source_node_uuid`, `target_node_uuid` coerces to `''` (Menhir installer #8). `generate_embedding` hardens a `None`
  fact to `''` before embedding (Menhir installer #5). `episodes` default changed from `default=[]` to
  `default_factory=list`.
- `tests/test_model_none_hardening.py`: focused regression tests covering all of the above, distinct default
  instances, unchanged non-None values, and embedder inputs.
- `.agent/data_models.md`: documents the native None-hardening contract.
- Scope: only Menhir installers #5 (`_patch_graphiti_none_replace`), #7 (`_patch_graphiti_node_summary_none`), and #8
  (`_patch_graphiti_edge_none_fields`) are migrated. The Menhir-side runtime patches themselves are NOT removed yet;
  other installers remain un-migrated. Tests/lint/typecheck NOT RUN by the worker (orchestrator owns verification).

## 2026-09-14 — Phase 1A review corrections (Codex independent review)

- `.agent/data_models.md`: corrected model inventory against `graphiti_core/nodes.py`/`edges.py` at v0.29.3 — added
  `fact_triple` to `EpisodeType`; fixed `Node`/`Edge` base fields; listed exact fields for `EpisodicNode`, `EntityNode`,
  `CommunityNode`, `EpisodicEdge`, `EntityEdge` (`name` is the relation name, not a `relation` field), `CommunityEdge`.
- `.agent/architecture.md`: replaced the no-monkeypatching implication with current facts — unmodified v0.29.3 baseline
  on this branch, Menhir still applies 17 runtime Graphiti patches, runtime symbol rebinding removal deferred to later
  migration phases. Clarified there is no repository-wide Neo4j password default (server example `password`, MCP/Docker
  commonly `demodemo`).
- `.agent/for-review/graphiti-softfork-phase1a-bootstrap-glm53-20260914.md`: wrapup updated to list review corrections
  and drop the unverified no-invented-fields claim.

## 2026-09-14 — Archolith soft-fork Phase 1A bootstrap documentation

- `README.md`: added "Archolith soft-fork maintenance" section documenting purpose, baseline (upstream `v0.29.3`,
  exact SHA `021d3a57d511f21b10adaf7fa923bd5c1fce5e9d`), branch `menhir/0.29.3`, origin/upstream roles, exact-SHA
  consumer pinning, maintenance procedure, and the four mandatory fork-commit labels. Upstream README content preserved.
- `.agent/README.md`: replaced scaffold with fork-specific read order, role, fork boundary, branch/worktree rule, and
  the structural-ingest deferral note. Upstream `AGENTS.md`/`CLAUDE.md` remain authoritative for source conventions.
- `.agent/architecture.md`: documented Graphiti core modules, data flow, provider seams, fork/upstream topology,
  exact-SHA consumption, no-runtime-monkeypatch goal, and bootstrap-vs-future-migration state.
- `.agent/data_models.md`: documented core `EpisodicNode`/`EntityNode`/`CommunityNode`, `EpisodicEdge`/`EntityEdge`/
  `CommunityEdge`, `EpisodeType`, and canonical definition locations from the v0.29.3 tree.
- `.agent/workflows/code_conventions.md`: documented Python 3.10+, Ruff/Pyright/Pytest conventions, unit vs integration
  commands, telemetry-disabled testing, and fork branch/worktree/no-blind-upstream rules.
- Public fork: https://github.com/Archolith/graphiti. Docs scaffold created by Agent Smith was completed here; remote
  Menhir structural ingest remains deferred (known unavailable capability). No source, tests, CI, or packaging changes;
  no test runs were performed in this session.
