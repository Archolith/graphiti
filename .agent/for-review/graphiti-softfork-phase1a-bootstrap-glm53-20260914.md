# Wrapup — graphiti-softfork-phase1a-bootstrap-glm53-20260914

- **Agent:** OpenCode
- **Model:** zai-coding-plan/glm-5.3-flash
- **Status:** READY FOR REVIEW
- **Plan:** menhir-graphiti-soft-fork-migration Phase B / Phase 1A bootstrap docs
- **Worktree:** `C:\Users\thron\IdeaProjects\.agent\worktrees\graphiti-menhir-0293`
- **Branch:** `menhir/0.29.3` (baseline: upstream `getzep/graphiti` tag `v0.29.3`, exact commit `021d3a57d511f21b10adaf7fa923bd5c1fce5e9d`)
- **Commits:** `7fb3ad0959f75ce03a4104d185736d574d037caf` — orchestrator-accepted and committed Phase 1A product after
  independent review. This wrapup metadata adjustment will be committed separately by the orchestrator (that closeout
  commit does not exist yet).

## Summary

Completed Phase 1A fork bootstrap documentation on the exact v0.29.3 maintenance worktree. Added an Archolith
soft-fork maintenance section to the upstream README (upstream content preserved), replaced all `.agent/` scaffold
TODOs with concrete facts from this tree, and wrote the dated bootstrap changelog entry. No source, tests, CI,
packaging, or dependency changes.

## Files Changed

| File | Change |
|------|--------|
| `README.md` | Added "Archolith soft-fork maintenance" section near the top; upstream content preserved |
| `.agent/README.md` | Rewritten: read order, role, fork boundary, branch/worktree rule, structural-ingest deferral |
| `.agent/architecture.md` | Rewritten: modules, data flow, provider seams, fork topology, exact-SHA consumption, bootstrap vs migration |
| `.agent/data_models.md` | Rewritten: core Node/Edge/Episode/community models and canonical definition locations |
| `.agent/workflows/code_conventions.md` | Rewritten: style/tooling/test commands, telemetry-off testing, fork rules |
| `.agent/CHANGELOG.md` | Added 2026-09-14 bootstrap entry and review-corrections entry |
| `.agent/for-review/graphiti-softfork-phase1a-bootstrap-glm53-20260914.md` | This wrapup: Phase 1A report plus Codex review-correction record |

## Verification

| Check | Result |
|-------|--------|
| Tests | NOT RUN by worker (task forbids running tests; orchestrator verifies centrally) |
| Lint/typecheck | NOT RUN by worker (same reason) |
| `artifact_validate` | NOT RUN — tool not available in this harness session |
| Scaffold self-check (no TODO/YYYY-MM-DD placeholders in touched `.agent` files) | PASS — manual grep-style review |
| README baseline SHA/branch exactness | PASS — matches immutable anchors (`021d3a57d511f21b10adaf7fa923bd5c1fce5e9d`, `menhir/0.29.3`) |
| Scope fence (only named files touched) | PASS — edits limited to the seven named files |

## Claim Cross-Check

| Claim | Verified? |
|-------|-----------|
| Files-changed list matches actual edits | yes |
| Commit anchor accurate | yes — Phase 1A product committed by orchestrator at `7fb3ad0959f75ce03a4104d185736d574d037caf`; hash verified read-only via `git rev-parse 7fb3ad0` |
| Baseline SHA/branch documented exactly | yes |
| No claim that semantic migration or tests are complete | yes |
| Data-model fields verified against source | yes — every listed field re-checked against `graphiti_core/nodes.py` and `graphiti_core/edges.py` after Codex review found defects; the earlier "no invented fields" claim was removed and the inventory was corrected |

## Review Corrections (Codex independent Phase 1A review, 2026-09-14)

- `.agent/data_models.md`: corrected model inventory to match source at v0.29.3 — `EpisodeType` has four values
  (`message`, `json`, `text`, `fact_triple`); `Node` base fields are `uuid`, `name`, `group_id`, `labels`, `created_at`;
  `EpisodicNode` adds `source`, `source_description`, `content`, `valid_at`, `entity_edges`, `episode_metadata` (no
  `invalid_at`); `EntityNode` adds `name_embedding`, `summary`, `attributes`; `CommunityNode` adds `name_embedding`,
  `summary`; `Edge` base fields are `uuid`, `group_id`, `source_node_uuid`, `target_node_uuid`, `created_at`;
  `EpisodicEdge`/`CommunityEdge` add no fields; `EntityEdge` uses `name` for the relation name and adds `fact`,
  `fact_embedding`, `episodes`, `expired_at`, `valid_at`, `invalid_at`, `reference_time`, `attributes`.
- `.agent/architecture.md`: removed the implication that Menhir currently avoids monkeypatching; now states this branch
  is the unmodified v0.29.3 baseline, Menhir still installs 17 runtime Graphiti patches, and later phases will move
  behavior into fork source or explicit hooks and remove runtime symbol rebinding. Clarified there is no repository-wide
  Neo4j password default (server example `password=password`; MCP/Docker commonly `demodemo`).
- `.agent/CHANGELOG.md`: added a dated corrections entry; original bootstrap entry wording retained.

## Assumptions

- The wrapup could not be validated with `artifact_validate` because that MCP tool is not available in this session; the
  document follows the required template sections and fields.
- "Near the top" for the README section was interpreted as immediately after the upstream hero/badges/TIP block and
  before the framework description.
- Facts in `.agent/` docs were sourced from the v0.29.3 tree itself (source layout, `pyproject.toml`, class locations)
  and existing root `AGENTS.md`. Model field inventories were re-verified field-by-field against
  `graphiti_core/nodes.py` and `graphiti_core/edges.py` after the Codex review. The "17 runtime patches" and password
  configuration figures came from the Codex review instructions and were not independently re-derived in this session.

## Known Limitations

- Remote Menhir structural ingest is a known unavailable capability; project identity ingest is deferred. `.agent/project-id`
  was intentionally not created.

## Risks / Gaps

- Documentation claims (e.g. env variable defaults in `architecture.md`) were written from source inspection and not
  runtime-verified.
- This wrapup metadata adjustment is not yet committed; the orchestrator will commit it separately from the product
  commit `7fb3ad0959f75ce03a4104d185736d574d037caf`.

## Follow-Up Tasks

- Orchestrator: commit this wrapup metadata adjustment separately (product commit already exists at `7fb3ad0`).
- Future phases: begin semantic migration with labeled fork commits; revisit structural ingest when the capability
  becomes available.
