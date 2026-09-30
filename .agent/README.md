# graphiti — Agent Docs

Read everything in this directory before starting work. Upstream `AGENTS.md` and `CLAUDE.md` at the repo root remain
authoritative for Graphiti source and testing conventions; these docs add fork context on top of them.

## Read Order

1. `AGENTS.md` and `CLAUDE.md` (repo root, upstream-authored) — source layout and testing conventions.
2. `architecture.md` — system design, seams, and fork/upstream topology.
3. `data_models.md` — core graph data models.
4. `workflows/code_conventions.md` — style, tooling, and test commands.
5. `CHANGELOG.md` — running log, most recent first.

## Role

This directory documents the Archolith soft fork of Graphiti (maintained for Menhir) at baseline upstream
`getzep/graphiti` tag `v0.30.2`, exact commit `eaa4128681bc53487138a4bbc22d58336ebe70d2`, on branch `menhir/main`.

## Fork Boundary

- `origin` is `Archolith/graphiti` (public fork); `upstream` is `getzep/graphiti`.
- Menhir consumes this fork by exact commit SHA only — never a moving branch or tag.
- Native extraction, resolution and request-guard hooks are implemented; Menhir consumes them through policy adapters.
- Every fork customization commit must be labeled exactly one of: `upstream bug fix` | `Menhir policy divergence` |
  `provider compatibility` | `temporary workaround`.
- Never blindly merge upstream `main` into the maintenance line; assess, cherry-pick or rebase in a separate worktree,
  and gate before accepting.

## Branch / Worktree Rule

The canonical clone stays on `main` (the upstream mirror). All fork work lands on the single long-lived branch
`menhir/main`; releases are cut from it with `v*` tags. The former `menhir/0.29.3` and `menhir/0.30.2` lines were
consolidated into `menhir/main` on 2026-09-30. Do fork work only in isolated worktrees branched from `menhir/main`.

## Structural Ingest Deferral

Agent Smith created recoverable scaffold files here, but project identity / structural ingest is deferred: remote Menhir
ingest is a known unavailable capability. Do not retry it and do not invent `.agent/project-id`.

## Files

| File | Purpose |
|------|---------|
| `architecture.md` | System design, data flow, seams, fork topology |
| `data_models.md` | Core graph data models and canonical definition locations |
| `CHANGELOG.md` | Running log of changes, most recent first |
| `workflows/code_conventions.md` | Language-specific style and formatting rules |

## Maintenance Rules

- Update `data_models.md` when any entity, DTO, or enum changes.
- Update `architecture.md` when adding services, integrations, or structural changes.
- Update the relevant workflow file when operational behavior changes.
- Add a `CHANGELOG.md` entry at the end of every session with meaningful changes.
  Format: `## YYYY-MM-DD — <short description>` with bullet points per file changed.
- Push to git regularly — at minimum at the end of each working session.
- Use conventional commit messages: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`.
- Only commit files worked on this session. Run `git diff --name-only` and `git status` before staging. Add files
  explicitly by path — never `git add .` or `git add -A`.
