"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from abc import ABC, abstractmethod
from typing import Any, Protocol, TypedDict

from pydantic import BaseModel, Field

from .models import ChatPrompt, Message, PromptFunction, PromptVersion
from .prompt_helpers import to_prompt_json


class EdgeDuplicate(BaseModel):
    duplicate_facts: list[int] = Field(
        ...,
        description='List of idx values of duplicate facts (only from EXISTING FACTS range). Empty list if none.',
    )
    contradicted_facts: list[int] = Field(
        ...,
        description='List of idx values of contradicted facts (from full idx range). Empty list if none.',
    )


class EdgeDuplicateBatchItem(BaseModel):
    request_id: int = Field(
        ...,
        description='Integer request_id matching one item from EDGE REQUESTS.',
    )
    duplicate_facts: list[int] = Field(
        ...,
        description='List of idx values of duplicate facts for this request (only from its existing_edges range). Empty list if none.',
    )
    contradicted_facts: list[int] = Field(
        ...,
        description='List of idx values of contradicted facts for this request (from its full idx range). Empty list if none.',
    )


class EdgeDuplicateBatch(BaseModel):
    edge_resolutions: list[EdgeDuplicateBatchItem] = Field(
        ...,
        description='Exactly one resolution per request_id in EDGE REQUESTS.',
    )


class Prompt(Protocol):
    resolve_edge: PromptVersion
    resolve_edges_batch: PromptVersion


class Versions(TypedDict):
    resolve_edge: PromptFunction
    resolve_edges_batch: PromptFunction


# GPT-5.6+ models (including GPT-6 Luna) reuse cached prompt prefixes only at message boundaries,
# and OpenAI requires >= 1,024 tokens in the cached prefix. Keep all static rules, constraints,
# and disambiguation examples in the system message so only the per-edge dynamic candidate lists
# and new fact stay in the user message.
_RESOLVE_EDGE_SYSTEM_PROMPT = """You are a fact deduplication assistant. NEVER mark facts as contradicted unless the new fact explicitly makes the old fact false.

NEVER mark facts as duplicates if they have key differences, particularly around numeric values, dates, or key qualifiers.

Only mark a fact as contradicted when the NEW FACT and the existing fact cannot both be true.
Do NOT treat additional activity, progress, status, or project-update facts as contradictions of role, ownership, responsibility, membership, or assignment facts. A person doing work on a project is compatible with that person remaining responsible for, owning, leading, or being the DRI for the project.

IMPORTANT constraints:
- duplicate_facts: ONLY idx values from EXISTING FACTS (NEVER include FACT INVALIDATION CANDIDATES)
- contradicted_facts: idx values from EITHER list (EXISTING FACTS or FACT INVALIDATION CANDIDATES)
- The idx values are continuous across both lists (FACT INVALIDATION CANDIDATES start where EXISTING FACTS end)
- Never invent idx values that are not present in the provided lists.

Input structure in the user message:
You will receive TWO lists of facts with CONTINUOUS idx numbering across both lists, followed by the NEW FACT.
EXISTING FACTS are indexed first, followed by FACT INVALIDATION CANDIDATES.

1. DUPLICATE DETECTION:
   - If the NEW FACT represents identical factual information as any fact in EXISTING FACTS (including clear paraphrases with the exact same meaning, dates, and quantities), return those idx values in duplicate_facts.
   - Never put an idx from FACT INVALIDATION CANDIDATES into duplicate_facts, even if the wording looks identical.
   - If no duplicates, return an empty list for duplicate_facts.

2. CONTRADICTION DETECTION:
   - Determine which facts the NEW FACT directly contradicts from either list.
   - A contradiction requires explicit evidence that the old fact stopped being true or was replaced, such as clear termination, replacement, transfer, or mutually exclusive current-state language.
   - Do NOT infer contradiction from recency alone, episode dates, meeting dates, weekly snapshots, section headings such as "updates" or "status changes", or a new event involving the same entities.
   - Do NOT mark a role/ownership/responsibility/assignment fact as contradicted merely because the NEW FACT describes work performed, updates shipped, documents created, progress made, blockers, next steps, or collaboration involving the same person and project.
   - Do NOT mark additive facts as contradictions when both can remain true simultaneously (for example, speaking multiple languages or working on multiple projects).
   - A fact from EXISTING FACTS can be both a duplicate AND contradicted only when the same assertion is restated with an explicit incompatible change.
   - Return all contradicted idx values in contradicted_facts.
   - If no contradictions, return an empty list for contradicted_facts.

<EXAMPLE>
EXISTING FACT: idx=0, "Alice joined Acme Corp in 2020"
NEW FACT: "Alice joined Acme Corp in 2020"
Result: duplicate_facts=[0], contradicted_facts=[] (identical factual information)

EXISTING FACT: idx=1, "Alice works at Acme Corp as a software engineer"
NEW FACT: "Alice works at Acme Corp as a senior engineer"
Result: duplicate_facts=[], contradicted_facts=[1] (same relationship but updated title — contradiction, NOT a duplicate)

EXISTING FACT: idx=2, "Bob ran 5 miles on Tuesday"
NEW FACT: "Bob ran 3 miles on Wednesday"
Result: duplicate_facts=[], contradicted_facts=[] (different events on different days — neither duplicate nor contradiction)

EXISTING FACT: idx=3, "Maya is the DRI for the billing migration"
NEW FACT: "Maya merged the retry fix for the billing migration"
Result: duplicate_facts=[], contradicted_facts=[] (project work does not contradict ownership or responsibility)

EXISTING FACT: idx=4, "The payments service uses Redis for session storage"
NEW FACT: "The payments service replaced Redis with PostgreSQL for session storage"
Result: duplicate_facts=[], contradicted_facts=[4] (explicit architectural replacement supersedes the old choice)

EXISTING FACT: idx=5, "Sam owns 2 bicycles"
NEW FACT: "Sam owns 3 bicycles"
Result: duplicate_facts=[], contradicted_facts=[5] (updated current count — NOT a duplicate because the numeric value changed)

EXISTING FACT: idx=6, "Priya speaks English"
NEW FACT: "Priya speaks Japanese"
Result: duplicate_facts=[], contradicted_facts=[] (additive non-exclusive facts can both be true)

EXISTING FACTS: idx=0, "Acme API runs on v1"
FACT INVALIDATION CANDIDATES: idx=1, "Acme API defaults to v1 auth"
NEW FACT: "Acme API migrated to v2 auth and retired v1"
Result: duplicate_facts=[], contradicted_facts=[0, 1] (idx=1 from FACT INVALIDATION CANDIDATES may only appear in contradicted_facts, never duplicate_facts)
</EXAMPLE>"""


def resolve_edge(context: dict[str, Any]) -> list[Message]:
    return [
        Message(
            role='system',
            content=_RESOLVE_EDGE_SYSTEM_PROMPT,
        ),
        Message(
            role='user',
            content=f"""
<EXISTING FACTS>
{context['existing_edges']}
</EXISTING FACTS>

<FACT INVALIDATION CANDIDATES>
{context['edge_invalidation_candidates']}
</FACT INVALIDATION CANDIDATES>

<NEW FACT>
{context['new_edge']}
</NEW FACT>
""",
        ),
    ]


# Static-first cacheable system prompt for batched edge deduplication (Menhir #91 Path B).
# Its dedupe and contradiction rules and its examples are resolve_edge's, so a batched decision
# follows the same policy as a per-edge one; test_batch_prompt_rules_match_resolve_edge checks it.
# Clears OpenAI's 1,024-token message-boundary prefix cache floor while resolving N independent
# edge requests in a single LLM round-trip.
_RESOLVE_EDGES_BATCH_SYSTEM_PROMPT = """You are a fact deduplication assistant. NEVER mark facts as contradicted unless the new fact explicitly makes the old fact false.

You will receive a JSON array in EDGE REQUESTS containing multiple INDEPENDENT fact-resolution requests.
Each request object has:
- `request_id`: integer identifier for that request
- `existing_edges`: list of `{"idx": int, "fact": str}` duplicate candidates (indexed first)
- `edge_invalidation_candidates`: list of `{"idx": int, "fact": str}` contradiction-only candidates (continuous idx numbering starting right after `existing_edges`)
- `new_edge`: the new fact string to evaluate against THAT request's candidates only

Evaluate EVERY request independently. NEVER mix or borrow `idx` values across different `request_id` entries.

NEVER mark facts as duplicates if they have key differences, particularly around numeric values, dates, or key qualifiers.

Only mark a fact as contradicted when `new_edge` and an existing fact in the SAME request cannot both be true.
Do NOT treat additional activity, progress, status, or project-update facts as contradictions of role, ownership, responsibility, membership, or assignment facts. A person doing work on a project is compatible with that person remaining responsible for, owning, leading, or being the DRI for the project.

IMPORTANT constraints (applied independently per `request_id`):
- `duplicate_facts`: ONLY `idx` values from that request's `existing_edges` (NEVER include `edge_invalidation_candidates`)
- `contradicted_facts`: `idx` values from EITHER `existing_edges` or `edge_invalidation_candidates` of that request
- The `idx` values are continuous within each request (`edge_invalidation_candidates` start where `existing_edges` end)
- Never invent `idx` values that do not appear in that request.
- Your response `edge_resolutions` MUST contain EXACTLY one entry for every `request_id` in EDGE REQUESTS — no missing, skipped, or duplicate `request_id`s.

For each request:
1. DUPLICATE DETECTION:
   - If `new_edge` represents identical factual information as any fact in that request's `existing_edges` (including clear paraphrases with the exact same meaning, dates, and quantities), return those `idx` values in `duplicate_facts`.
   - Never put an `idx` from `edge_invalidation_candidates` into `duplicate_facts`, even if the wording looks identical.
   - If no duplicates exist in `existing_edges`, return `duplicate_facts: []`.

2. CONTRADICTION DETECTION:
   - Determine which facts in that request's `existing_edges` or `edge_invalidation_candidates` are directly contradicted by `new_edge`.
   - A contradiction requires explicit evidence that the old fact stopped being true or was replaced, such as clear termination, replacement, transfer, or mutually exclusive current-state language.
   - Do NOT infer contradiction from recency alone, episode dates, meeting dates, weekly snapshots, section headings such as "updates" or "status changes", or a new event involving the same entities.
   - Do NOT mark a role/ownership/responsibility/assignment fact as contradicted merely because `new_edge` describes work performed, updates shipped, documents created, progress made, blockers, next steps, or collaboration involving the same person and project.
   - Do NOT mark additive facts as contradictions when both can remain true simultaneously (for example, speaking multiple languages or working on multiple projects).
   - A fact from `existing_edges` can be both a duplicate AND contradicted only when the same assertion is restated with an explicit incompatible change.
   - If no contradictions exist, return `contradicted_facts: []`.

<EXAMPLE>
Input EDGE REQUESTS:
[
  {
    "request_id": 0,
    "existing_edges": [{"idx": 0, "fact": "Alice joined Acme Corp in 2020"}],
    "edge_invalidation_candidates": [],
    "new_edge": "Alice joined Acme Corp in 2020"
  },
  {
    "request_id": 1,
    "existing_edges": [{"idx": 0, "fact": "Alice works at Acme Corp as a software engineer"}],
    "edge_invalidation_candidates": [],
    "new_edge": "Alice works at Acme Corp as a senior engineer"
  },
  {
    "request_id": 2,
    "existing_edges": [{"idx": 0, "fact": "Bob ran 5 miles on Tuesday"}],
    "edge_invalidation_candidates": [{"idx": 1, "fact": "Bob ran 3 miles on Wednesday"}],
    "new_edge": "Bob ran 3 miles on Wednesday"
  },
  {
    "request_id": 3,
    "existing_edges": [{"idx": 0, "fact": "Maya is the DRI for the billing migration"}],
    "edge_invalidation_candidates": [],
    "new_edge": "Maya merged the retry fix for the billing migration"
  },
  {
    "request_id": 4,
    "existing_edges": [{"idx": 0, "fact": "The payments service uses Redis for session storage"}],
    "edge_invalidation_candidates": [],
    "new_edge": "The payments service replaced Redis with PostgreSQL for session storage"
  },
  {
    "request_id": 5,
    "existing_edges": [{"idx": 0, "fact": "Sam owns 2 bicycles"}],
    "edge_invalidation_candidates": [],
    "new_edge": "Sam owns 3 bicycles"
  },
  {
    "request_id": 6,
    "existing_edges": [{"idx": 0, "fact": "Priya speaks English"}],
    "edge_invalidation_candidates": [],
    "new_edge": "Priya speaks Japanese"
  },
  {
    "request_id": 7,
    "existing_edges": [{"idx": 0, "fact": "Acme API runs on v1"}],
    "edge_invalidation_candidates": [{"idx": 1, "fact": "Acme API defaults to v1 auth"}],
    "new_edge": "Acme API migrated to v2 auth and retired v1"
  }
]

Result edge_resolutions:
[
  {"request_id": 0, "duplicate_facts": [0], "contradicted_facts": []},
  {"request_id": 1, "duplicate_facts": [], "contradicted_facts": [0]},
  {"request_id": 2, "duplicate_facts": [], "contradicted_facts": []},
  {"request_id": 3, "duplicate_facts": [], "contradicted_facts": []},
  {"request_id": 4, "duplicate_facts": [], "contradicted_facts": [0]},
  {"request_id": 5, "duplicate_facts": [], "contradicted_facts": [0]},
  {"request_id": 6, "duplicate_facts": [], "contradicted_facts": []},
  {"request_id": 7, "duplicate_facts": [], "contradicted_facts": [0, 1]}
]
(Notes: request 0: identical factual information; request 1: same relationship but updated title, a contradiction and NOT a duplicate; request 2: idx 0 is a different event on a different day, neither duplicate nor contradiction; the identical text appears only in edge_invalidation_candidates idx 1, which is NEVER placed in duplicate_facts; request 3: project work does not contradict ownership or responsibility; request 4: explicit architectural replacement supersedes the old choice; request 5: updated current count, NOT a duplicate because the numeric value changed; request 6: additive non-exclusive facts can both be true; request 7: idx 1 from edge_invalidation_candidates may only appear in contradicted_facts, never duplicate_facts.)
</EXAMPLE>"""


def resolve_edges_batch(context: dict[str, Any]) -> list[Message]:
    edge_requests = context['edge_requests']
    count = len(edge_requests)
    return [
        Message(
            role='system',
            content=_RESOLVE_EDGES_BATCH_SYSTEM_PROMPT,
        ),
        Message(
            role='user',
            content=f"""
<EDGE REQUESTS>
{to_prompt_json(edge_requests)}
</EDGE REQUESTS>

EDGE REQUESTS contains {count} independent requests with request_id 0 through {count - 1}.
Your response MUST include EXACTLY {count} resolutions in edge_resolutions with request_id 0 through {count - 1}. Do not skip, duplicate, or merge request_ids.
""",
        ),
    ]


versions: Versions = {
    'resolve_edge': resolve_edge,
    'resolve_edges_batch': resolve_edges_batch,
}


class DedupeEdgesPrompts(ABC):
    @abstractmethod
    def resolve_edge(self, context: dict[str, Any]) -> ChatPrompt: ...

    def resolve_edges_batch(self, context: dict[str, Any]) -> ChatPrompt:
        # Not abstract: batching is never used with a custom prompt library, so older
        # subclasses need not implement it.
        return ChatPrompt.from_messages(resolve_edges_batch(context))


class DefaultDedupeEdgesPrompts(DedupeEdgesPrompts):
    def resolve_edge(self, context: dict[str, Any]) -> ChatPrompt:
        return ChatPrompt.from_messages(resolve_edge(context))

    def resolve_edges_batch(self, context: dict[str, Any]) -> ChatPrompt:
        return ChatPrompt.from_messages(resolve_edges_batch(context))
