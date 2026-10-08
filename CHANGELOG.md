# Changelog

This file covers the Archolith maintenance fork published as
[`archolith-graphiti-core`](https://pypi.org/project/archolith-graphiti-core/).
The package still imports as `graphiti_core`. For the upstream project's release
history, see [getzep/graphiti](https://github.com/getzep/graphiti/releases).

## 0.30.2.post3 — 2026-10-08

Maintenance release on the same upstream Graphiti 0.30.2 base.
[Source tag](https://github.com/Archolith/graphiti/tree/v0.30.2.post3) ·
[PyPI files](https://pypi.org/project/archolith-graphiti-core/0.30.2.post3/)

### Changed

- New optional `Graphiti(edge_expiry_hook=...)`. Without a hook, or when it answers
  `EXPIRE`, edge expiry is unchanged. A hook that answers `WORLD_END` keeps an edge
  whose own `invalid_at` is the fact's world-time end (a finished trip, a past state)
  from being expired for that end alone; a contradiction that starts inside the
  fact's window still supersedes it
  ([#5](https://github.com/Archolith/graphiti/pull/5)).

## 0.30.2.post2 — 2026-10-01

Maintenance release on the same upstream Graphiti 0.30.2 base.
[Source tag](https://github.com/Archolith/graphiti/tree/v0.30.2.post2) ·
[PyPI files](https://pypi.org/project/archolith-graphiti-core/0.30.2.post2/)

### Changed

- The combined node/edge extraction prompt now places all static instruction text
  (rules, examples, entity and fact type catalogs) in the system message and only
  per-call content in the user message. The instruction text is byte-identical to
  the previous layout; only the split point moved, so providers that cache at
  message boundaries can reuse the static prefix
  ([#3](https://github.com/Archolith/graphiti/pull/3)).
- Menhir merge-lineage attributes (`merge_audit`, `merged_from`,
  `last_merge_op_id`) are kept out of deduplication-candidate, batch-summary and
  typed attribute-extraction prompts; stored node attributes are unchanged
  ([#2](https://github.com/Archolith/graphiti/pull/2)).

## 0.30.2.post1 — 2026-09-27

First public release of the Archolith fork, based on upstream Graphiti 0.30.2.
[Source tag](https://github.com/Archolith/graphiti/tree/v0.30.2.post1) ·
[PyPI files](https://pypi.org/project/archolith-graphiti-core/0.30.2.post1/)

### Added

- Native extension hooks for extraction, deduplication candidate filtering,
  identity gating, node pre-resolution, and OpenAI-compatible request guarding.
  Menhir supplies policy through these hooks instead of replacing Graphiti
  symbols at runtime.
- Adaptive deduplication request splitting when a request exceeds the configured
  context ceiling; request-scoped lifecycle and failure metadata for the LLM
  guard.

### Changed

- Single-episode ingestion uses the combined node-and-edge extraction path.
  Request-scoped clients and edge evidence remain attached throughout extraction
  and deduplication.
- The fork is distributed under its own PyPI name with an exact version. Its
  wheel build explicitly includes `graphiti_core`, and the publishing workflow
  verifies the package identity and native hook files before upload.

### Fixed

- Preserve untyped and previously stored node attributes during updates;
  tolerate malformed entity records during extraction.
- Avoid conflating related locations during deduplication and preserve
  concurrent group/database routing. The upstream 0.30.2 search, Saga cleanup,
  and database-routing fixes are retained.

Do not install this distribution alongside upstream `graphiti-core`: both
provide the `graphiti_core` import package.
