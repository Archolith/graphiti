# graphiti — Architecture

## Overview

Graphiti is a framework for building and querying temporal context graphs for AI agents. It incrementally integrates
episodes (messages, text, JSON) into a graph of entities, relations, and communities with temporal validity windows,
then supports hybrid retrieval (semantic + keyword + graph traversal) without full recomputation.

**Fork context:** this tree is the Archolith soft fork of upstream `getzep/graphiti`, baseline tag `v0.29.3`, exact
commit `021d3a57d511f21b10adaf7fa923bd5c1fce5e9d`, branch `menhir/0.29.3`. `origin` is `Archolith/graphiti`; `upstream`
is `getzep/graphiti`. Semantic customization migration has begun (see `.agent/CHANGELOG.md`); Menhir's runtime
patches remain installed until Phase F.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.10+ |
| Core models | Pydantic v2 |
| Graph databases | Neo4j, FalkorDB, Neptune Analytics (Kuzu deprecated) |
| LLM clients | OpenAI, Azure OpenAI, Anthropic, Groq, Gemini, GLiNER2 |
| Embedders | OpenAI, Azure OpenAI, Voyage, Gemini |
| Cross-encoders (reranking) | BGE (local), OpenAI, Gemini |
| Telemetry | PostHog (opt-out), OpenTelemetry tracing |

## Data Flow

1. `Graphiti.add_episode` receives raw data (episode) via `graphiti.py`.
2. LLM clients (`llm_client/`) with prompt templates (`prompts/`) extract entities, relations, and edge temporal info.
3. Embedders (`embedder/`) embed node names/summaries for hybrid search.
4. Entities are deduplicated/resolved against existing nodes; edges get validity windows; community structure is
   maintained (`utils/`, `graph_queries.py`).
5. Nodes/edges are persisted through the driver layer (`driver/`).
6. Retrieval (`search/`) combines semantic, BM25/fulltext, and graph-distance searches, optionally reranked by a
   cross-encoder (`cross_encoder/`).

## Key Components

| Module | Role |
|--------|------|
| `graphiti.py` | `Graphiti` orchestration class: add/search/delete, driver + client wiring |
| `nodes.py`, `edges.py` | Core Pydantic node/edge models and save/get helpers |
| `models/` | Extended/derived model definitions (`nodes/`, `edges/` subpackages) |
| `driver/` | `GraphDriver` interface + Neo4j, FalkorDB, Neptune, Kuzu (deprecated) implementations; `operations/` holds per-entity operation ABCs |
| `llm_client/` | Provider LLM clients behind a common interface |
| `embedder/` | Provider embedder clients |
| `cross_encoder/` | Rerankers for search result reranking |
| `search/` | Search recipes/filters and hybrid search config |
| `namespaces/` | Namespace management (bulk ops scoped per node/edge type) |
| `migrations/` | Database migration support |
| `telemetry/` | OpenTelemetry tracing; PostHog event capture |
| `prompts/` | LLM prompt templates |
| `utils/` | Maintenance, dedup, community, and mass operations |

Provider seams: graph database (`GraphDriver`), LLM (`LLMClient`), embedder (`EmbedderClient`), reranker
(`CrossEncoderClient`). Consumers swap implementations at `Graphiti` construction. This maintenance branch started from
the unmodified v0.29.3 source baseline; Menhir still installs runtime Graphiti patches against the fork, and migration
phases are progressively moving that behavior into fork source (see Prompt JSON Serialization below,
Structured Summary Policy below, and `.agent/CHANGELOG.md`). Full removal of runtime symbol rebinding is a later
migration phase and is not yet met on this
branch.

## Prompt JSON Serialization

`graphiti_core/prompts/prompt_helpers.py::to_prompt_json` implements a native prompt-serialization
boundary: before `json.dumps`, the payload is recursively copied and normalized so that (1) dict
entries whose string key ends with `_embedding` are dropped, (2) dict entries whose value structurally
looks like an embedding vector are dropped — a list/tuple of length strictly greater than 64 whose
FIRST EIGHT items are int/float (bool excluded); this intentional sampled-head rule preserves short
numeric lists, bool lists, and string lists — and (3) non-JSON-native values (temporal-like provider
objects) are converted via callable `isoformat`, then `iso_format`, then `to_native`, with a final
`str` fallback; a conversion returning the same object short-circuits to `str`. The caller's input is
never mutated, and `ensure_ascii`/`indent` pass through to `json.dumps` unchanged. This prevents
embedding vectors from bloating prompts and prevents temporal serialization failures during episode
extraction. All prompt modules (and `search/search_helpers.py`) inherit this behavior through the
shared helper import — no runtime rebinding or patching of prompt modules is involved. This replaces
Menhir runtime patch behavior (installer #3, `_patch_graphiti_prompt_json`) at the source level.

## Structured Summary Policy

`graphiti_core/prompts/summarize_nodes.py` implements the Menhir structured-summary policy natively in
`summarize_context` and `summarize_pair` (installer #4, `_patch_graphiti_summarize`). Both produce exactly
two messages (system + user) and request output as `key:value` pairs separated by `' | '` with a 150-character
total ceiling, replacing the upstream prose-summary instruction style. `summarize_context` restricts the model
to the provided MESSAGES, focuses on what the entity is, its role/status, and key attributes, and requires a
GOOD/BAD example contrast; `summarize_pair` merges two summaries keeping the most current/specific values and
dropping duplicates. Context values are interpolated with raw f-strings of `context.get(...)` defaults, so
missing keys are tolerated without raising; `to_prompt_json` is deliberately not used in these two functions
(`summary_description` still uses it). The `versions` mapping points directly at the native functions with no
later rebinding, wrappers, or aliases.

Deliberate Menhir policy divergence: `summarize_context` no longer asks the model to extract entity attributes
and does not include `context['attributes']` in the prompt (upstream v0.29.3 instructs attribute extraction and
renders an ATTRIBUTES block). This mirrors the active Menhir patch and narrows the function to summary-only
output; it is an intentional tradeoff of this fork, not an omission.

## Single-Episode Extraction Routing (native, fork)

`Graphiti.add_episode` routes single-episode extraction through the existing combined extractor
(`graphiti_core/utils/maintenance/combined_extraction.py::extract_nodes_and_edges`) by default — one LLM call
produces both entity nodes and relationship facts, replacing the upstream two-call `extract_nodes` then
`extract_edges` sequence. This implements the fork half of Menhir installer #1 (`_patch_graphiti_combined_extraction`)
natively: the installer previously rebound module symbols (`extract_nodes`/`extract_edges`) and carried edges via a
`ContextVar` cache; the fork now routes through ordinary call arguments. This default route is a deliberate
divergence from upstream v0.29.3, whose hook-absent behavior is always the separate path.
`Graphiti._extract_single_episode` owns route selection, and
`Graphiti._extract_and_resolve_edges` accepts `precomputed_edges` so combined/hook edges skip `extract_edges` and
flow directly into `resolve_extracted_edges`. Edges are carried per-call only — no module-global state, no
`ContextVar`, nothing to reset between requests.

Route policy: `COMBINED` for ordinary schemas; `SEPARATE` fallback when custom edge schemas (`edge_types`) are
supplied. The fallback is a deliberate compatibility boundary, not a functional gap: combined-route edges also pass
through `resolve_extracted_edges` (which performs the downstream typed-attribute work), so the SEPARATE route
exists to keep custom-schema episodes on the exact pre-C1g upstream/installer path rather than to supply missing
machinery. Bulk routing (`extract_nodes_and_edges_bulk`) is unchanged.

Neutral extension hook: a `SingleEpisodeExtractionHook` (see `graphiti_core/extraction_routing.py`) may be passed
to `Graphiti(...)` at construction. It is invoked once per `add_episode` call with a frozen
`SingleEpisodeExtractionContext` — borrowed request-local inputs (clients, episode, previous episodes, type maps,
custom instructions); the container is frozen but its contents are shared with the call and hooks must not mutate
them — and returns:

- `None` — default route selection;
- `ExtractionRoute` (`COMBINED`/`SEPARATE`) — force a built-in route; Graphiti still performs extraction;
- `SingleEpisodeExtractionResult` — the hook performed extraction; Graphiti skips its own extraction calls and
  feeds the supplied nodes/edges/index-map into native resolution, attribute extraction, and persistence.

Any other return value raises `TypeError`; hook exceptions (including cancellation) propagate and abort the call,
leaving no state to reset. The hook parameter defaults to absent; with no hook installed, behavior is the fork's
default routing above (combined by default — itself a divergence from upstream). The hook is
policy-free: it carries no Menhir receipt, marker, repair, or telemetry semantics — Phase F will wire Menhir
policy on top of this hook and remove installers #1/#2.

## Fork / Upstream Topology

- Canonical clone: `Archolith/graphiti` (`origin`), stays on `main`.
- This maintenance worktree: branch `menhir/0.29.3`, based on upstream `v0.29.3`
  (`021d3a57d511f21b10adaf7fa923bd5c1fce5e9d`).
- Upstream remote: `getzep/graphiti` (`upstream`).
- Consumers pin exact fork commit SHAs; branches and tags are never used as consumption pins.
- Upstream changes are applied via assessed cherry-pick/rebase in a separate worktree, gated, then the pinned SHA is
  updated — never by blind merges of upstream `main`.

## Current Bootstrap State vs Future Migration

Current: upstream `v0.29.3` tree with Phase C migrations landed in fork source (model None-hardening, response
normalization, native prompt JSON serialization, native structured summary prompts, combined-extraction model
sanitization, native single-episode combined-extraction routing with a neutral extraction hook — see
`.agent/CHANGELOG.md`);
Menhir runtime patches remain installed
until Phase F. Menhir remote structural ingest is deferred (known unavailable capability; do not invent
`.agent/project-id`).
Future: Menhir policy divergences and provider compatibility fixes migrate as labeled commits under the four allowed
labels (`upstream bug fix` | `Menhir policy divergence` | `provider compatibility` | `temporary workaround`).

## Configuration / Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` | Neo4j connection in examples/tooling | server example uses `bolt://localhost:7687`, `neo4j`, `password` |
| `FALKORDB_HOST`, `FALKORDB_PORT` | FalkorDB connection | `localhost`, `6379` |
| `OPENAI_API_KEY` | OpenAI LLM/embedder auth | required by default clients |
| `GRAPHITI_TELEMETRY_ENABLED` | Disable PostHog telemetry | `true` |

There is no single repository-wide Neo4j password default: the core library receives configured drivers and has no
password default of its own. The server example uses `password=password`, while the MCP/Docker configuration commonly
uses `demodemo`.

## External Dependencies

- Graph databases: Neo4j 5.26+, FalkorDB, AWS Neptune Analytics; Kuzu is deprecated upstream and slated for removal.
- LLM/embedding APIs: OpenAI (default), Azure OpenAI, Anthropic, Groq, Google Gemini, Voyage.
- Search backends for hybrid fulltext: driver-provided (Neo4j, FalkorDB) or OpenSearch (Neptune/neo4j-opensearch extras).
