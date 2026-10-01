# graphiti — Architecture

## Overview

Graphiti is a framework for building and querying temporal context graphs for AI agents. It incrementally integrates
episodes (messages, text, JSON) into a graph of entities, relations, and communities with temporal validity windows,
then supports hybrid retrieval (semantic + keyword + graph traversal) without full recomputation.

**Fork context:** this tree is the Archolith soft fork of upstream `getzep/graphiti`, baseline tag `v0.30.2`, exact
commit `eaa4128681bc53487138a4bbc22d58336ebe70d2`, branch `menhir/main`. `origin` is `Archolith/graphiti`; `upstream`
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

The same boundary drops Menhir merge lineage: dict entries keyed `merge_audit`, `merged_from` or
`last_merge_op_id` are omitted at any depth. Entity attributes reach the dedup context
(`**candidate.attributes`) and the batch-summary context (`'attributes': node.attributes`) through
`to_prompt_json`; the typed attribute-extraction context renders its node dict without it, so
`node_operations._extract_entity_attributes` passes `without_merge_lineage(node.attributes)` there.
Only the prompt omits lineage: the node's stored attributes, and the attributes merged back after
extraction, keep it. This replaces Menhir's request-level filter (`_strip_merge_lineage`, Menhir #205).

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

## Dedup Prompt Anti-Conflation Policy (fork half of installer #11)

`graphiti_core/prompts/dedupe_nodes.py` implements Menhir's anti-conflation node-dedup prompt policy
natively in both active per-entity dedup prompts, `node` and `nodes` (including the corresponding
`versions['node']`/`versions['nodes']` entries, which point directly at the functions — no wrappers or
runtime rebinding). Both prompts' `<EXAMPLE>` blocks carry an additional counterexample: a NEW
ENTITY of `"the suburbs"` against an existing `Chicago` (Location, summary "A city where someone
lives") resolving to `duplicate_candidate_id = -1`, with an explanation that relative/descriptive
locations (the suburbs, downtown, countryside) are never the same object as a specific named city
merely because they are related or appear in movement context. The example is rendered exactly once
per prompt with the same native JSON brace style as the surrounding examples (no escaped-brace
artifacts from the original Menhir runtime patch, `_patch_graphiti_dedup_prompt`). Existing examples
and the response contract are unchanged, and `node_list` (the separate UUID-grouping prompt) is
deliberately outside this policy and untouched. Identity-gate logic remains installer #12 and is not
part of this phase.

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

Combined-extraction prompt caching: `graphiti_core/prompts/extract_nodes_and_edges.py::extract_message` splits its
output across messages — the system message carries the static persona, all instruction blocks (ENTITY RULES through
</NEGATIVE EXAMPLES>), and the <ENTITY TYPES>/<FACT TYPES> catalogs (byte-identical to the pre-split text); the user
message carries only per-call content (<PREVIOUS MESSAGES>, <CURRENT MESSAGES>, custom instructions). This keeps the
system prefix stable across calls for a deployment so GPT-5.6+ models — which only reuse cached prefixes at message
boundaries — hit the prompt cache (measured 98% cached input tokens vs 0% with everything in the user message).

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

## Identity Gate for LLM-Proposed Node Merges (native, fork)

`node_operations._resolve_with_llm` is the seam where the dedupe LLM selects (or rejects) an existing
candidate for each extracted node. The fork exposes a neutral typed hook at exactly that boundary:
an `IdentityGateHook` (see `graphiti_core/identity_gate.py`) may be passed to `Graphiti(...)` at
construction and is invoked exactly once per *valid* LLM-proposed merge — after normalized
`NodeResolutions` are available and before `_promote_resolved_node` or any resolved-state,
uuid-map, or duplicate-pair mutation. It is never invoked for negative (`duplicate_candidate_id < 0`)
decisions, invalid candidate ids, invalid or duplicate relative ids, deterministic exact/similarity
resolution, or paths with no LLM-proposed merge. This replaces only the Graphiti-side *mechanism* of
Menhir installer #12 (`_patch_graphiti_dedup_identity_gate`, which wrapped
`node_operations._resolve_with_llm` and temporarily replaced `llm_client.generate_response`); the
Menhir policy remains outside the fork and is wired during Phase F.

Hook contract: the hook receives a frozen `IdentityGateContext` — borrowed request-local evidence
(the extracted `EntityNode`, the selected candidate `EntityNode`, the LLM's `candidate_id`, the
episode when resolution is episode-bound, the prior episodes, and the request-local
extracted/precomputed `EntityEdge` evidence available at that boundary); the container is frozen but
its contents are shared with the call and hooks must not mutate them — and must return an
`IdentityGateDecision`: `ALLOW` preserves Graphiti's ordinary promotion of the candidate; `VETO`
gives the extracted node Graphiti's ordinary no-duplicate behavior (kept as a new node). Any other
return value raises `TypeError`; hook exceptions (including cancellation) propagate and abort the
call, leaving no state to reset.

Edge evidence travels through ordinary per-call arguments — `identity_gate_edges` on
`resolve_extracted_nodes` / `_resolve_with_llm`, and an episode-indexed `extracted_edges` list on
`dedupe_nodes_bulk` — never a module global, `ContextVar`, or cache. Availability by flow: single
`add_episode` passes the combined/hook `precomputed_edges` (`None` on the separate route, where
edges do not exist yet at node-resolution time); the bulk path passes each episode's already
extracted edges at both `dedupe_nodes_bulk` and `_resolve_nodes_and_edges_bulk` (the latter's
deduped `edges_by_episode`); `add_triplet` passes the hook with no episode and no edges.
The hook parameter defaults to absent; with no hook installed, resolution behavior is byte-for-byte
the pre-D2 fork behavior (direct promotion of valid merges). Compatibility rule: identity-gate
kwargs are forwarded from `Graphiti`/bulk call paths only when a hook is configured — with no hook,
every call path (`add_episode`, `_extract_and_resolve_nodes`, `_extract_and_dedupe_nodes_bulk`,
`_resolve_nodes_and_edges_bulk`, `add_triplet`, `dedupe_nodes_bulk`'s first pass) invokes
`resolve_extracted_nodes`/`dedupe_nodes_bulk` with the exact legacy positional/keyword shape,
omitting both new kwargs entirely; edge evidence alone never triggers keyword forwarding. The hook is
policy-free: it carries no
identity heuristic (exact/substring/acronym/Jaccard), no edge-fact mention policy, no Menhir
warning text, no receipt or telemetry semantics — Phase F will wire Menhir's positive-identity
policy on top of this hook and remove installer #12.

## Candidate Filter for Node-Dedupe Candidate Pools (native, fork)

`node_operations.resolve_extracted_nodes` builds, per extracted node, a dedupe
candidate pool by running semantic search (`_collect_candidate_nodes`) and merging
those results with `existing_nodes_override` (`_merge_candidate_nodes`, order-preserving,
uuid-deduplicated). The fork exposes a neutral typed hook at exactly that boundary: a
`CandidateFilterHook` (see `graphiti_core/candidate_filter.py`) may be passed to
`Graphiti(...)` at construction and is invoked exactly once per unique merged candidate
per extracted node — after the merge/dedup above and before deterministic
exact/fuzzy resolution or LLM candidate indexing (including the building of the
LLM candidate list for unresolved nodes). It is consulted for every resolution
path because all of them consume the merged pool.

Hook contract: the hook receives a frozen `CandidateFilterContext` — borrowed
request-local inputs (the current extracted `EntityNode` and the unique merged
candidate `EntityNode`); the container is frozen but the node objects are shared with
the in-flight call and are read-only by contract (no runtime copy or immutability
enforcement) — and must return a `CandidateFilterDecision`: `INCLUDE` keeps the
candidate in that extracted node's pool; `EXCLUDE` removes it, so it can never be
resolved to (deterministically or via the LLM) for that node. The same candidate is
evaluated once for each different extracted node. Candidate order is preserved. When
all candidates for an extracted node are excluded, Graphiti's ordinary no-candidate
behavior applies: the node is kept as new, and neither the dedupe LLM nor an installed
identity-gate hook runs for it. Any other return value raises `TypeError`; hook
exceptions (including cancellation) propagate and abort the call, leaving no state to
reset — there is no module-global state, `ContextVar`, or cache, so there is no
cross-call leakage.

Compatibility rule: `resolve_extracted_nodes` gains an optional
`candidate_filter_hook` parameter and `dedupe_nodes_bulk` a matching pass-through.
The hook is forwarded from `Graphiti` at every `resolve_extracted_nodes` call path —
single `add_episode`, `_extract_and_resolve_nodes`, `_extract_and_dedupe_nodes_bulk`/
`dedupe_nodes_bulk`, `_resolve_nodes_and_edges_bulk`, and `add_triplet` — only when
configured; with no hook configured, every call path keeps the exact legacy
positional/keyword shape (the kwarg is omitted entirely, never passed as `None`).
The candidate-filter and identity-gate hooks compose independently: each is
forwarded exactly when its own hook is present, and identity edge evidence
(`identity_gate_edges`) is forwarded only when the identity hook is present. The
pre-resolution hook additionally carries its own `node_pre_resolution_edges` channel
(never reusing `identity_gate_edges`). The candidate-filter hook
is policy-free: it carries no `structure_role`/`is_view`/`view_kind`/`view_class`
predicate, no canonical-self/receipt/telemetry semantics, no logging counts, and no
source policy — this replaces only the Graphiti-side *mechanism* of Menhir installer
#14 (`_patch_graphiti_structural_candidate_isolation`, which wrapped
`node_operations._collect_candidate_nodes`); Menhir's structural/View predicate
remains outside the fork and is wired on top of this hook during Phase F.

## Untyped Attribute Preservation in Attribute Extraction (fork half of installer #15)

`node_operations._extract_entity_attributes` is the single boundary where node attributes
are replaced during extraction. When no typed attribute schema applies — `entity_type is None`
or the schema has no `model_fields` — the function now returns a defensive shallow copy of
`node.attributes` (absent/falsy attributes are treated as empty) instead of `{}`. No LLM call
is made in this path. `extract_attributes_from_nodes` assigns that returned dict back to
`node.attributes`, so externally owned pre-existing properties survive replacement-save
persistence, and the returned mapping never aliases the original node attribute dict.
Typed-schema behavior is unchanged: episode context build, LLM call, capped overlay merge
(`apply_capped_attributes`), shape validation, and return. This is generic and policy-free
(no Menhir predicates or names in runtime code); it replaces the Graphiti-side *mechanism* of
Menhir installer #15 (`_patch_graphiti_untyped_attribute_preservation`, which wrapped
`_extract_entity_attributes`). Menhir-side patch removal is deferred to Phase F.

## Adaptive Dedupe Request Bisection and Node Pre-Resolution (native, fork half of installer #16)

Two independent, policy-free mechanisms now live in `node_operations` and
`graphiti_core/node_pre_resolution.py`, replacing the Graphiti-side mechanics of Menhir
installer #16 (`_patch_graphiti_adaptive_dedupe`):

Adaptive request bisection. `graphiti_core/errors.py` gains a public
`GraphitiRequestTooLargeError` (a `GraphitiError` subclass). Phase D5 only *catches* it —
making real OpenAI-compatible clients raise it is owned by Phase E. When
`resolve_extracted_nodes`' single combined `_resolve_with_llm` call raises
`GraphitiRequestTooLargeError`, the dedicated recursive helper
`_resolve_unresolved_indices` bisects the unresolved index batch at its stable midpoint
and resolves left then right recursively; each retry's dedupe candidate index is rebuilt
only from the candidate pools of that subset (via `_merge_candidate_nodes`, preserving
order and uuid-dedup semantics); a singleton batch that still raises propagates the
exception unchanged. Semantic search, candidate filtering, and deterministic
exact/similarity resolution are never rerun across splits — they complete exactly once
before LLM escalation. Resolved nodes, uuid mappings, and duplicate pairs merge into the
shared batch state (no duplicate promotions), and `identity_gate_hook` /
`identity_gate_edges` are forwarded unchanged to every initial and retry LLM call, so
identity-gate decisions and edge evidence survive every split. Without the exception,
the ordinary one-request path is behavior-identical to the pre-D5 fork.

Node pre-resolution seam. `NodePreResolutionHook` (see
`graphiti_core/node_pre_resolution.py`) may be passed to `Graphiti(...)` at construction
(class-level default included, so `__init__`-bypassing instances observe "no hook") and
runs exactly once per extracted node *before* any semantic candidate search. It
receives a frozen borrowed `NodePreResolutionContext` — the extracted `EntityNode`, the
`GraphitiClients` bundle (driver/LLM/embedder access), the episode when bound, prior
episodes, the entity-type map, and the request-local edge evidence when available — and
must return a frozen `PreResolutionResult` carrying an explicit
`PreResolutionDecision` plus a payload: `DEFER` requires `resolved_node=None` and leaves
ordinary resolution untouched; `RESOLVE` requires `resolved_node` to be an `EntityNode`
and marks the node pre-resolved, excluding it from candidate search, the candidate
filter, deterministic similarity, and the dedupe LLM, committed to the final resolved
state and uuid map exactly once, with a duplicate pair recorded exactly when the
resolved UUID differs from the extracted UUID. Validation is strict: any other return
type (including a bare `EntityNode`, which is NOT an implicit `RESOLVE`), any unknown
decision, and any invalid decision/payload combination raise `TypeError`; hook
exceptions propagate with no state to reset. Edge evidence for this hook travels a
dedicated channel: `node_pre_resolution_edges` on `resolve_extracted_nodes`
(episode-indexed `node_pre_resolution_edges` on `dedupe_nodes_bulk`), forwarded only
when the pre-resolution hook is configured and never sourced from
`identity_gate_edges`; the D2 `identity_gate_edges` channel remains independent.
Ordinary nodes are searched once and realigned to original extracted-node order.

Both mechanisms compose with the D2 identity gate and D3 candidate filter: the filter
still runs once per unique merged candidate for non-pre-resolved nodes only, before
deterministic/LLM resolution; pre-resolved nodes invoke neither the candidate filter nor
the identity gate nor the dedupe LLM. The new `node_pre_resolution_hook` kwarg is
forwarded from `Graphiti` at every `resolve_extracted_nodes` path (`add_episode`,
`_extract_and_resolve_nodes`, `_extract_and_dedupe_nodes_bulk`/`dedupe_nodes_bulk`,
`_resolve_nodes_and_edges_bulk`, `add_triplet`) and through `dedupe_nodes_bulk`'s first
pass only when configured — with no hook, every call path keeps the exact legacy
positional/keyword shape (kwarg omitted, never `None`), and identity edge evidence is
still forwarded only with the identity hook. Both mechanisms are generic and carry no
Menhir canonical-self identity, UUID/namespace derivation, receipts, mode selection,
telemetry/prompt measurement, or candidate predicates: Menhir's canonical-self
pre-resolution policy and installer-#16 patch removal are wired/removed in Phase F; the
exception-raising side is Phase E.

## OpenAI-Compatible Request Guard and Context-Length Normalization (fork half of installer #17)

`graphiti_core/llm_client/request_guard.py` (new in Phase E) and the
`OpenAIGenericClient` implement the fork-native *mechanism* of Menhir installer #17
(`_patch_graphiti_openai_generic_client`): generic request sizing, an optional
pre-send ceiling, and narrow provider context-length classification. The module is
policy-free — no Menhir imports, environment-variable names, scheduler URLs, or
lifecycle policy live in the fork.

Request sizing and ceiling. Every assembled provider request is sized immediately
before send with a documented conservative estimate — char count divided by
`CHARS_PER_TOKEN = 3` (ceiling division, minimum 1), deliberately pessimistic for
code-heavy Graphiti prompts; it is an estimate for a guard, not a tokenizer. The
effective ceiling is resolved per request through a typed
`RequestCeilingResolver` seam (`async resolve_request_ceiling(context) ->
int | None`): `None` means no ceiling is configured, `0` is a deliberate opt-out
(the check stays disabled and derivation policy outside the fork must not re-enable
it), and a positive int is enforced by `enforce_request_ceiling` BEFORE the provider
call — an oversize request raises the public `GraphitiRequestTooLargeError`
(introduced in D5) with structural diagnostics only (message count, total chars,
largest messages as index/role/char-count tuples; never prompt content). Where the
ceiling comes from — endpoint/context-window probing, caching, TTLs, per-endpoint
values — is entirely consumer policy and lives outside the fork.

Context-length normalization. `is_context_length_error` classifies provider
rejections narrowly: a structured `code` attribute equal to
`context_length_exceeded`, OR a dict `body` whose `error.code` equals it — the two
are inspected independently, so a nested `context_length_exceeded` classifies even
when the top-level code is generic (e.g. `invalid_request_error`) — plus the
recognized textual fallbacks `context_length_exceeded` / `maximum context length`.
A provider context-length rejection raises
`GraphitiRequestTooLargeError` (chained `from` the provider error) inside
`_generate_response`; it is not retryable by the tenacity wrapper
(`is_server_or_retry_error` does not match it) and D5's bisection catches it. So a
deterministic oversized payload never enters an unchanged retry loop. Unrelated bad
requests and local parse failures do not match the classifier and keep their normal
behavior.

Typed, request-scoped extension seams (all optional, per Phase F's Menhir halves).
An immutable `RequestGuard` may be passed to `OpenAIGenericClient(...)` at
construction bundling: a `ceiling_resolver` (scheduler/context-window probing or
request-ceiling resolution), a `lifecycle_hook` (`on_request_started` immediately
before send — after ceiling enforcement, so rejected requests never report as
started — and `on_request_completed(context, response)` on success), a
`failure_listener` (`on_request_failed(context, error, phase, response)` with phase
`'ceiling_rejected'` or `'provider_error'`), and a `correlation_provider`
(`resolve_correlation(context) -> str | None`, invoked once per request before any
other hook). Hooks receive a frozen, explicitly bounded `LLMResponseMetadata` —
provider status, response id, raw length, a 240-character raw-response preview, and
duration when built on completion — so a consumer can reproduce its own diagnostic
records (parse failures, empty responses, provider errors) without the fork
implementing logging policy; the full raw response is never retained by the fork.
Every provider/send/parse failure — including translated OpenAI rate limits and
context-length normalization — is observed exactly once through the failure seam;
pre-send ceiling rejections and provider-call failures (rate limit, transport) carry
`response=None`. `group_id` is a partition/namespace and `prompt_name` an operation
name — they are NOT intrinsically a unique per-episode key; consumers needing
per-request correlation supply an opaque id via the `correlation_provider`, a
deliberately narrow seam: it receives the immutable measured context and may return
only a string (or None); the fork itself creates the final context via
`dataclasses.replace(correlation_id=...)`, so consumer code cannot alter the
fork-measured structural sizing facts (model, endpoint, counts, sizes, estimated
tokens, largest messages) before ceiling enforcement. The
fork keeps no module-global mutable request state, no `ContextVar`, and performs no
symbol rebinding. Hook and provider exceptions propagate and abort the request with
no state to reset.

Compatibility. With no guard (or an all-default guard) the client behaves exactly
as before: same `json_schema`/`json_object` handling, code-fence tolerance,
empty-response behavior, tracing, multilingual instructions, attribute-extraction
framing, and retry semantics for genuinely retryable failures (tenacity on
`RateLimitError`/`EmptyResponseError`/`JSONDecodeError`; a subclass retry wrapper
exists only to thread request-scoped `group_id`/`prompt_name` through, mirroring
the base tenacity configuration). Generic response normalization added: responses
with no `choices` raise a clear `EmptyResponseError` instead of an `IndexError`,
and prose-wrapped JSON is normalized by a left-to-right
`json.JSONDecoder.raw_decode` scan that returns the FIRST decodable object/array —
robust against later brace fragments, multiple payload-looking spans, and trailing
malformed braces; unparseable output still raises the original `JSONDecodeError`
so retry classification is unchanged. gpt-5*/o1/o3/o4 models send
`max_completion_tokens` instead of the rejected legacy `max_tokens` parameter. The
Menhir halves of #17 — the ceiling derivation policy, failure diagnostics, and
lifecycle telemetry — are wired through these seams in Phase F, which also removes
the installer; the Menhir-side concise-prompt/truncation-escalation retry loop
remains Menhir-owned retry policy and is not ported.

## Fork / Upstream Topology

- Canonical clone: `Archolith/graphiti` (`origin`), stays on `main`.
- Maintenance branch: `menhir/main`, the single long-lived fork line (currently upstream `v0.30.2` plus fork
  changes; it began at upstream `v0.29.3`, `021d3a57d511f21b10adaf7fa923bd5c1fce5e9d`). Releases are `v*` tags on it.
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

## Upstream 0.30.2 reconciliation

The maintenance line merges the stable v0.30.2 release, retaining the fork hooks.
Single-episode extraction (hook, combined and separate routes), edge resolution and
bulk deduplication use the upstream request-scoped GraphitiClients bundle. Neither
concurrent tenant changes nor extraction hooks mutate the shared instance driver.
Upstream search reranking, Neo4j routing, saga refetch and untyped attribute fixes
are preserved. The Menhir compatibility workflow tests the merged boundaries.
