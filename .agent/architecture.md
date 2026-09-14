# graphiti — Architecture

## Overview

Graphiti is a framework for building and querying temporal context graphs for AI agents. It incrementally integrates
episodes (messages, text, JSON) into a graph of entities, relations, and communities with temporal validity windows,
then supports hybrid retrieval (semantic + keyword + graph traversal) without full recomputation.

**Fork context:** this tree is the Archolith soft fork of upstream `getzep/graphiti`, baseline tag `v0.29.3`, exact
commit `021d3a57d511f21b10adaf7fa923bd5c1fce5e9d`, branch `menhir/0.29.3`. `origin` is `Archolith/graphiti`; `upstream`
is `getzep/graphiti`. No semantic customization has migrated yet — current state is pure upstream bootstrap.

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
(`CrossEncoderClient`). Consumers swap implementations at `Graphiti` construction. This maintenance branch currently
contains the **unmodified v0.29.3 source baseline**; current Menhir still installs **17 runtime Graphiti patches**
against that baseline. A goal of later migration phases is to move suitable behavior into fork source or explicit hooks
and remove runtime symbol rebinding — that goal is not yet met on this branch.

## Fork / Upstream Topology

- Canonical clone: `Archolith/graphiti` (`origin`), stays on `main`.
- This maintenance worktree: branch `menhir/0.29.3`, based on upstream `v0.29.3`
  (`021d3a57d511f21b10adaf7fa923bd5c1fce5e9d`).
- Upstream remote: `getzep/graphiti` (`upstream`).
- Consumers pin exact fork commit SHAs; branches and tags are never used as consumption pins.
- Upstream changes are applied via assessed cherry-pick/rebase in a separate worktree, gated, then the pinned SHA is
  updated — never by blind merges of upstream `main`.

## Current Bootstrap State vs Future Migration

Current: pure upstream `v0.29.3` tree plus documentation scaffold; no semantic customization migrated; Menhir remote
structural ingest is deferred (known unavailable capability; do not invent `.agent/project-id`).
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
