# Graphiti v0.31.1 fork rebase plan

Date: 2026-10-15
Working branch: `menhir/upgrade-0.31.1`
New upstream base: `getzep/graphiti@v0.31.1` (`5961293865a57844fe13d4eb061deb3501971fb2`)
Current released fork: `archolith-graphiti-core==0.30.2.post7`
Current long-lived fork branch: `menhir/main`

## Goal

Reconstruct the Archolith maintenance fork on the clean upstream v0.31.1 tag. Do not merge upstream `main` wholesale and do not blindly replay the complete 0.30.2 patch history. Port only behavior that is still required by Menhir, adapting it to upstream's new extraction, prompt, dedup, and temporal APIs.

The first releasable result should be a reviewed `archolith-graphiti-core==0.31.1.post1`. Menhir's dependency pin must not move until the fork gates and Menhir compatibility/live Neo4j gates pass.

## Patch disposition

| Fork change | Source | Initial disposition | Reason / v0.31.1 interaction |
|---|---|---|---|
| Native extraction, candidate-filter, identity, pre-resolution and request-guard hooks; single-episode combined route | 0.30.2.post1 | PORT / ADAPT | Upstream gained configurable prompts and LLMRuntime, but not Menhir's policy hook contract or GraphitiRequestTooLargeError behavior. Rebase hooks around current GraphitiClients/LLMRuntime and current combined extraction path. |
| Untyped/prior node-attribute preservation | post1 | DROP where upstream-equivalent | Upstream v0.31.1 contains the generic preservation correction. Keep regression tests proving equivalence; do not carry duplicate implementation. |
| Related-location anti-conflation / identity behavior | post1 | REVIEW / PORT ONLY POLICY DELTA | v0.31.x substantially changes exact-name/identity resolution. Keep Menhir-specific anti-conflation policy only if upstream does not meet the contract under tests. |
| Prompt merge-lineage filtering (`merge_audit`, `merged_from`, `last_merge_op_id`) | PR #2 / post2 | PORT | Menhir-specific persisted fields; upstream has no equivalent policy. Reapply at prompt serialization / attribute-extraction boundary compatible with configurable prompts. |
| Combined-extraction static prefix caching | PR #3 / post2 | REWRITE | Upstream made C2 the default combined prompt and added configurable prompt plumbing. Preserve C2 semantics, then create a stable cacheable static boundary rather than replaying the old byte split. Benchmark/token-cache gate required. |
| `edge_expiry_hook` + WORLD_END semantic | PR #5 / post3 | PORT / ADAPT | Upstream temporal normalization handles future plans/deadlines but does not expose Menhir's world-end-vs-supersession policy seam. |
| Reconcile multiple copies of one stored edge, earliest expiry/end wins | PR #7 / post4 | PORT unless v0.31.1 tests prove equivalent | Upstream edge resolution changed materially; verify first. Menhir requires no restatement copy to overwrite a supersession found by another copy. |
| Recomputed edge attributes win during copy reconciliation | PR #9 / post5 | PORT unless v0.31.1 tests prove equivalent | No clear upstream contract found. Needed for paraphrase/exact-fact mixed duplicate batches. |
| `Graphiti(preserved_edge_attributes=...)` | PR #11 / post6 | PORT / ADAPT | Upstream does not expose this API. Required so Menhir-owned time-contract/stamp fields survive duplicate attribute recomputation and reach expiry policy. |
| Neo4j stale save can never revive a stored expired edge; earliest stored expiry/end wins | PR #13 / post7 | PORT | No upstream equivalent found. Required for concurrent-writer correctness under Menhir anchored-time expiry. |

## Upstream changes to preserve

The reconstruction must retain v0.31.1 behavior, including:
- v0.31 extraction/dedup/temporal fixes from upstream PR #1964;
- C2 as the default combined extraction prompt;
- exact-name and identity-property candidate handling;
- endpoint-pair-aware edge resolution / contradiction behavior;
- LLMRuntime and configurable prompt support;
- current GraphitiClients/request-scoped driver behavior;
- v0.31.1 package/version fixes.

Fork code must compose with these rather than restore 0.30.2 implementations over them.

## Port order

1. **Fork identity + test scaffolding**
   - rename distribution back to `archolith-graphiti-core`;
   - establish v0.31.1-based compatibility workflow;
   - bring over only tests needed to express fork contracts.

2. **Core Menhir extension seams**
   - extraction routing hook;
   - candidate filter;
   - identity gate;
   - node pre-resolution;
   - request-size guard / adaptive dedup splitting.
   - Adapt around upstream `LLMRuntime`, configurable prompts, and current clients.

3. **Prompt policy**
   - merge-lineage exclusion;
   - anti-conflation policy if still necessary under v0.31.1;
   - redesign cacheable combined C2 prompt while preserving upstream C2 content/semantics.

4. **Anchored-time edge semantics**
   - edge-expiry hook;
   - edge-copy reconciliation;
   - recomputed-attributes reconciliation;
   - preserved edge attributes.

5. **Neo4j persistence race guard**
   - port PR #13's lock-before-read stale-save protection to v0.31.1's current Neo4j edge-save queries.

6. **Release + consumer bump**
   - full fork compatibility suite;
   - live disposable Neo4j expiry/concurrency suite;
   - Menhir Graphiti fork contract tests;
   - Menhir anchored-time P3/P3c unit + live tests;
   - ingest/dedup/recall smoke;
   - package `0.31.1.post1`;
   - only then update Menhir's exact pin/hash.

## Gates

A patch is not considered successfully ported merely because it applies.

### Behavior gates
- no runtime monkeypatching is reintroduced;
- upstream v0.31.1 generic behavior remains available when Menhir hooks are unset;
- fork-specific constructor args/hooks are neutral by default;
- Menhir merge-lineage data never reaches LLM prompt content;
- duplicate/rephrased edge resolution preserves Menhir-owned attributes when configured;
- a natural world end is not treated as supersession when the hook says WORLD_END;
- contradictory evidence still expires/truncates an edge;
- multiple copies of one stored edge cannot erase an earlier supersession;
- concurrent/stale Neo4j saves cannot revive an expired edge or move its end later.

### Regression gates
- upstream v0.31.1 test subset covering extraction, node resolution, edge resolution, temporal normalization, configurable prompts, and LLMRuntime;
- fork compatibility tests;
- ruff + pyright on all touched fork modules;
- disposable Neo4j live tests for save/expiry concurrency;
- Menhir's Graphiti fork E2E contract and anchored-time suites.

### Performance/cost gate
The C2 prompt-cache rewrite must be measured instead of assumed. Record uncached/cached input-token behavior on repeated distinct combined-extraction calls. Do not regress C2 extraction behavior merely to preserve the old post2 cache layout.

## Non-goals for this branch

- Do not update Menhir's pin yet.
- Do not publish a fork package until all gates pass.
- Do not merge moving upstream `main`; v0.31.1 is the fixed base for this upgrade.
- Do not port historical fork code that v0.31.1 already satisfies.
- Do not change Menhir policy semantics while doing the dependency rebase.
