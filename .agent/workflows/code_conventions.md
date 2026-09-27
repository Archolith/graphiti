# Code Conventions — graphiti

## Style

- Python 3.10+ (4-space indentation, single quotes, 100-character lines — enforced via Ruff config in `pyproject.toml`).
- Run `make format` (Ruff import sorting + canonical formatting) before committing.
- Run `make lint` (Ruff + Pyright over `graphiti_core`) before submitting.

## Naming

- Modules, files, functions: `snake_case`.
- Pydantic models: `PascalCase` with explicit type hints.
- Tests: files `test_<feature>.py`; integration test files use the `_int` suffix (e.g. `test_node_int.py`).

## File Organization

- Core library: `graphiti_core/` (domain modules plus `driver/`, `llm_client/`, `embedder/`, `cross_encoder/`,
  `search/`, `namespaces/`, `migrations/`, `telemetry/`, `prompts/`, `utils/`).
- Side-effectful code stays in drivers/adapters (`graphiti_core/driver`, `graphiti_core/cross_encoder`,
  `graphiti_core/utils`); keep helpers elsewhere pure.
- Service adapters: `server/graph_service/`; MCP integration: `mcp_server/` (own test suite under `mcp_server/tests/`).
- Specs: `spec/`; type signatures: `signatures/`.

## Testing

- Pytest with `asyncio_mode = auto` (async tests run without decorators).
- Unit only: `make test` (excludes `@pytest.mark.integration`).
- Targeted: `uv run pytest tests/path/test_file.py`.
- Integration tests are marked `@pytest.mark.integration` and gated out of default runs; backing services come from
  `docker-compose.test.yml`.
- Set `GRAPHITI_TELEMETRY_ENABLED=false` when running anything to avoid PostHog network calls.
- Authoritative fully-green no-DB unit gate (CI): `DISABLE_NEO4J=1 DISABLE_FALKORDB=1 DISABLE_KUZU=1 DISABLE_NEPTUNE=1`
  plus the `--ignore` list from `.github/workflows/unit_tests.yml`.

## Fork Rules

- Branch: `menhir/0.30.2` in this isolated maintenance worktree; canonical clone stays on `main`.
- Baseline: upstream `getzep/graphiti` tag `v0.30.2`, exact commit `eaa4128681bc53487138a4bbc22d58336ebe70d2`.
- Never blindly merge upstream `main` into the maintenance line; cherry-pick/rebase deliberately in a separate
  worktree and pass gates before updating Menhir's pinned exact SHA.
- Label every fork customization commit exactly one of: `upstream bug fix` | `Menhir policy divergence` |
  `provider compatibility` | `temporary workaround`.
