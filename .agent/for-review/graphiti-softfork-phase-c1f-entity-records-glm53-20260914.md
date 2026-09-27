# Wrapup — Graphiti Soft-fork Phase C1f: Entity-Record Tolerance + Neutral Group-Id Resolver Hook

- **Agent:** OpenCode
- **Model string:** `zai-coding-plan/glm-5.3-flash` (GLM)
- **Status:** READY FOR REVIEW — C1f independently accepted by Codex; product commit landed; wrapup awaits
  separate closeout commit.
- **Plan/ticket:** `.harness/TASK-graphiti-softfork-phase-c1f-entity-records-glm53-20260914.md` (Phase C1f)
- **Worktree/branch:** `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`, branch `menhir/0.29.3`
- **Commits:** Product commit `25b8f12508a57c0fbff598006bdd01b227912545` — `feat: tolerate malformed entity
  records`. This wrapup document itself remains UNCOMMITTED pending the separate closeout commit.
- **Verification scope:** NOT RUN (Codex-owned). No tests, lint, typecheck, artifact validation, git, or MCP calls were executed by this worker, per task instructions.

## Summary

Implemented the fork-native half of Menhir installer #6 (`_patch_graphiti_entity_record_group_id`) in
`graphiti_core/nodes.py`: generic entity-record tolerance (defensive copies, legacy `Z[UTC]` timestamp repair,
bounded log-once diagnostics with eviction) plus a neutral, typed process-level entity-record group-id resolver
hook (`set_entity_record_group_id_resolver` with `EntityRecordGroupIdResolver`). Menhir namespace policy
deliberately does NOT enter the fork; the hook is policy-free startup configuration and the Menhir
namespace/group-id choice remains Phase F work. Because `search/search_utils.py` already imports the function
object, all search call sites get the native behavior with no symbol rebinding.

## Files Changed

- `graphiti_core/nodes.py` — added `EntityRecordGroupIdResolver` type alias, `_entity_record_group_id_resolver`
  module global, `set_entity_record_group_id_resolver` public setter (accepts callable or `None`; rejects
  non-callables with `TypeError`), `_record_log_key` / `_log_once` / `_repair_created_at` / `_resolver_snapshot`
  private helpers, and two 512-key bounded diagnostic sets (`_logged_null_group_keys`,
  `_logged_repaired_timestamp_keys`). Rewrote `get_entity_node_from_record` to: shallow-copy the outer record
  first; copy dict `attributes` before the upstream key-pops (KUZU JSON-string path unchanged); copy non-None
  `labels` to a fresh list; invoke the resolver only for `group_id is None`, passing a read-only
  `MappingProxyType` over an isolated shallow snapshot whose attributes dict and labels list are copied again
  (return-only authority: top-level assignment fails on the mapping; nested mutation affects only the snapshot —
  str return authoritative including `''`; `None` falls back to `helpers.get_default_group_id(provider)` —
  FalkorDB `'_'`, `''` elsewhere; non-str/non-None raises `TypeError`; resolver exceptions propagate); normalize
  only an exact terminal `Z[UTC]` suffix to `+00:00` before `parse_db_date`; log null-group and timestamp repairs
  once per record key (uuid/name) via sets capped at 512 keys per category with arbitrary eviction at
  capacity — first-seen keys always log once per retention window, retained repeats never re-log — with identity
  and chosen group/original timestamp only, never attributes. Non-None stored group ids bypass the hook unchanged.
- `tests/test_entity_record_tolerance.py` — NEW, 21 focused tests: valid NEO4J record parity; caller outer-dict /
  attributes / labels (list and tuple) non-mutation; missing-labels default; provider defaults for null group
  without resolver (NEO4J `''`, FALKORDB `'_'`); resolver receives an isolated read-only snapshot (top-level
  assignment raises, nested mutation confined to the snapshot) and infers a named group; hook called only for null
  group; reset restores defaults; `None` fallback; empty-string authority; `TypeError` on non-string return;
  resolver exception propagation; non-callable setter rejection; exact `Z[UTC]` repair vs other timestamp handling
  (plain terminal `Z` parses successfully via `fromisoformat` as aware UTC with no repair diagnostic); diagnostics
  log identity but not attributes, dedupe repeat keys, and are bounded at 512 with eviction semantics (600 unique
  keys → 600 first-seen logs at 512 storage; evicted repeats re-log, retained repeats silent); search_utils native
  behavior without rebinding; KUZU string attributes supported; source contains no Menhir reference. An autouse
  fixture resets the global resolver and both diagnostic sets before and after each test.
- `.agent/data_models.md` — added "Entity Record Tolerance Contract (native, fork, `provider compatibility`)"
  section documenting copy semantics, resolver hook lifecycle/contract, timestamp repair, bounded diagnostics, and
  the explicit statement that this is installer #6's fork half with Menhir namespace policy remaining Phase F.
- `.agent/CHANGELOG.md` — new dated entry labeled exactly `provider compatibility` with the same Phase F scoping
  statement.
- `.agent/architecture.md` — intentionally NOT modified: its current structure has no extension/hooks section, so
  per task instructions the resolver contract lives in `data_models.md`; no new architecture section was created.
- `.harness/TASK-graphiti-softfork-phase-c1f-entity-records-glm53-20260914.md` — deleted at end of task (as
  instructed).

## Verification

The worker did not run tests, lint, typecheck, artifact validation, or git — verification and git are Codex-owned;
all results below are Codex's independent runs.

- `uv run pytest tests/test_entity_record_tolerance.py` — PASS `21 passed, 1 warning` per Codex's independent
  post-format verification
- `ruff check` — PASS per Codex (both focused and repository-wide)
- `ruff format --check` — PASS (`2 files already formatted`) per Codex's post-format verification
- `pyright` (changed files) — PASS 0 errors / 0 warnings / 0 info per Codex
- Cumulative unit suite — PASS `483 passed, 11 skipped, 3 warnings` per Codex
- Artifact validation — PASS `7 records, 0 findings` per Codex
- Diff check — exit 0 (informational line-ending warnings only) per Codex
- Manual consistency re-read of the edited `nodes.py` against the task contract and all review findings — PASS
  (design review only; no execution)

**Review disposition:** all 10 findings corrected, no open findings — 2x P1, 7x P2, 1x P3.

**Product files (exact, in product commit `25b8f12508a57c0fbff598006bdd01b227912545`):**
`graphiti_core/nodes.py`, `tests/test_entity_record_tolerance.py`, `.agent/data_models.md`, `.agent/CHANGELOG.md`.

## Claim Cross-Check

- Files-changed list matches actual edits: yes
- Commits listed (or UNCOMMITTED): yes (product commit `25b8f125` recorded; this wrapup itself UNCOMMITTED
  pending the separate closeout commit)
- Verification lines honest: yes (worker did not run tests; all results attributed to Codex)
- No Menhir import/reference/policy in fork source: yes (`nodes.py` mentions no Menhir symbol; test asserts this
  via `inspect.getsource` at runtime)
- No runtime symbol rebinding introduced: yes (behavior is native in the imported function object; search_utils
  untouched)

## Hook Lifecycle

1. Startup: host application calls `set_entity_record_group_id_resolver(resolver)` once (startup configuration;
   not per-record monkeypatching).
2. Per record: `get_entity_node_from_record` invokes the resolver only when the record's `group_id` is None,
   passing a read-only `MappingProxyType` over an isolated shallow snapshot (attributes dict and labels list
   copied again) of the defensive record copy, plus the `GraphProvider`.
3. Result: `str` (including `''`) is authoritative; `None` falls back to `get_default_group_id(provider)`;
   non-str/non-None raises `TypeError`; resolver exceptions propagate so policy bugs are visible.
4. Reset: `set_entity_record_group_id_resolver(None)` restores provider-default behavior.

## Global-State Isolation

Module globals introduced: `_entity_record_group_id_resolver`, `_logged_null_group_keys`,
`_logged_repaired_timestamp_keys`.
The test suite's autouse fixture resets all three before and after every test, so tests are order-independent.

## Behavior Tradeoffs

- The defensive copy adds a per-record dict/list copy cost on every entity node read (accepted: correctness over
  micro-performance; matches the installer contract that caller-owned data is never mutated). When a resolver is
  registered, one extra shallow snapshot plus nested container copies are made so the resolver's authority is
  strictly return-only.
- Resolver is process-global, not driver-scoped: one resolver per process. Sufficient for the installer's
  process-level semantics; can be widened later if multi-policy hosts appear.
- Diagnostics are per-process bounded sets (512 keys per category) with arbitrary eviction at capacity
  (`set.pop()`, matching current Menhir semantics): a
  new key at capacity evicts one existing key, is added, and logs; retained repeats stay silent; a previously
  evicted key re-appearing is first-seen again and re-logs. This trades unbounded memory for per-key
  log-once-within-retention-window semantics.
- Only the exact terminal suffix `Z[UTC]` is repaired; other legacy formats pass through unchanged (deliberately
  narrower than any speculative repair). Plain terminal `Z` is not repaired but parses natively via
  `datetime.fromisoformat`.

## Completion Checklist

- [x] Resolver extension point: typed, neutral, public setter, `None` reset, no Menhir mention
- [x] Defensive copy of record / dict attributes / non-None labels; KUZU JSON behavior preserved
- [x] `Z[UTC]` exact-suffix repair only; other values untouched
- [x] Bounded log-once diagnostics (≤512 keys/category), identity without attributes
- [x] Focused new test file, narrowly named, global state isolated
- [x] `data_models.md` + `CHANGELOG.md` updated, labeled `provider compatibility`, Phase F remainder stated
- [x] `architecture.md` left unmodified (no extension/hooks section exists)
- [x] Wrapup created; `.harness` task file deleted
- [x] Tests / lint / typecheck — NOT RUN by the worker; independently run and PASSED by Codex (see Verification)

## Review Corrections (Codex independent review, 2026-09-14)

1. **P1 resolver authority leak** — the resolver previously received the same mutable working dict later used to
   construct the `EntityNode`, allowing a callback to corrupt the returned node via `name`/`attributes`/`labels`.
   Fixed: the resolver now receives a read-only `MappingProxyType` over a separate shallow snapshot whose
   attributes dict and labels list are separately copied again (`_resolver_snapshot`). Top-level assignment raises
   `TypeError` on the mapping; nested mutation (if attempted) affects only the isolated snapshot — never caller
   input or the returned node. Exceptions stay visible; authority is return-only.
2. **P1 broken bounded log-once** — `_log_once` previously stopped adding keys at 512 but still logged every
   unseen key, so post-cap records re-logged on every occurrence. Fixed: at capacity, one existing key is evicted
   before the new key is added and then logged, matching the Menhir helper's bounded eviction semantics exactly.
   The bound test now asserts 600 unique keys produce 600 first-seen logs at 512 storage, an evicted key re-logs
   when repeated, and a retained key produces no extra log.
3. **P2 incorrect timestamp test** — Python 3.12 `datetime.fromisoformat` accepts a plain terminal `Z`, so the
   test no longer expects `ValueError`; it asserts the aware UTC parse succeeds and that no repaired-timestamp
   diagnostic fires ("other values pass through unchanged").
4. **P2 ambiguous public API** — renamed `set_group_id_resolver` → `set_entity_record_group_id_resolver` and
   `GroupIdResolver` → `EntityRecordGroupIdResolver` everywhere (source, tests, docs), making the hook explicitly
   entity-record-specific rather than appearing to govern every node/group-id path.
5. **P2 Ruff import order** — moved `import graphiti_core.search.search_utils as search_utils` into the sorted
   local import block ahead of the `from graphiti_core...` imports.
6. **P3 reporting drift** — corrected the test count to the final static count of 21 test functions in the
   CHANGELOG and this wrapup (previously reported as 19).
7. **P2 resolver snapshot assertion** — the test wrongly expected the resolver's snapshot attributes to still
   contain the reserved `attributes.uuid`. Graphiti strips reserved keys from the working attributes copy before
   the resolver is invoked, so the snapshot carries `{'custom': 'value'}` before the test adds `sneaky`; the
   assertion was corrected to `{'custom': 'value', 'sneaky': 'x'}`. Source unchanged.
8. **P2 arbitrary set eviction assumption** — Python `set.pop()` eviction is arbitrary (matching current Menhir
   semantics), so the bound test wrongly assumed `u-0` was evicted and `u-599` retained. Fixed: after 600 inserts,
   the test derives one retained key from the actual set and one evicted key from the 600-key universe minus the
   set, first repeats the retained key asserting no new log, then repeats the evicted key asserting exactly one
   new log and bounded size 512. Source unchanged.
9. **P2 caplog isolation in the bound test** — the retained/evicted repeat assertions previously examined all
   600 previously captured records instead of only the new repeats. Fixed: `caplog.clear()` is called immediately
   before the retained-key repeat (asserting zero matching records), and again before the evicted-key repeat
   (asserting exactly one new matching log). Dynamic retained/evicted key selection and the 512 size bound are
   preserved. Source unchanged.
10. **P2 Ruff format line wrapping** — the two long `_resolve(_valid_record(...))` calls in the bound test
    exceeded the 100-character limit and were wrapped exactly as standard Ruff formatting renders them. Source
    unchanged.

Cumulative review tally: 10 corrected findings total — 2x P1, 7x P2, 1x P3. Codex has independently verified
post-format: focused pytest `21 passed, 1 warning`; Ruff format `2 files already formatted`; Ruff check PASS;
changed-file Pyright 0 errors/warnings/info; cumulative `483 passed, 11 skipped, 3 warnings`; repository-wide
Ruff PASS; artifact validation `7 records, 0 findings`; diff check exit 0 (informational line-ending warnings
only).

## Assumptions

- "Extension/hooks section" in `architecture.md` was interpreted strictly: the file has only per-installer
  narrative sections and a "Provider seams" paragraph, no extension/hooks section, so the contract stayed in
  `data_models.md`.
- Exact pass counts were pending Codex's verification run at draft time; Codex has since independently verified
  (see Review Corrections and Verification). Final closeout (acceptance and commit SHAs) follows the product
  commit.

## Risks / Gaps

- Resolved: the earlier "unverified" gap is closed — Codex independently ran the focused suite, Ruff, Pyright,
  the cumulative unit suite, artifact validation, and the diff check (all PASS; see Verification).
- The diagnostic key falls back to `'<unknown>'` when both uuid and name are missing; such records dedupe to a
  single log entry.

## Follow-Up Tasks

- Closeout: commit this wrapup document (product commit `25b8f12508a57c0fbff598006bdd01b227912545` has landed;
  the wrapup itself is still uncommitted).
- Phase F: remove the Menhir-side `_patch_graphiti_entity_record_group_id` runtime patch and implement Menhir's
  namespace→group-id policy (registering it via `set_entity_record_group_id_resolver` at Menhir startup).
