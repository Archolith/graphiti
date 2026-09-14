# Changelog — graphiti

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
