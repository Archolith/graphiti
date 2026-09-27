# Changelog

This file covers the Archolith maintenance fork published as
[`archolith-graphiti-core`](https://pypi.org/project/archolith-graphiti-core/).
The package still imports as `graphiti_core`. For the upstream project's release
history, see [getzep/graphiti](https://github.com/getzep/graphiti/releases).

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
