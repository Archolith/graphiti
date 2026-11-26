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

from pydantic import BaseModel, Field, model_validator

from .models import ChatPrompt, Message, PromptFunction, PromptVersion
from .prompt_helpers import to_prompt_json


class NodeDuplicate(BaseModel):
    id: int = Field(..., description='integer id of the entity')
    name: str = Field(
        ...,
        description='Name of the entity. Should be the most complete and descriptive name of the entity. Do not include any JSON formatting in the Entity name such as {}.',
    )
    duplicate_candidate_id: int = Field(
        ...,
        description='candidate_id of the matching EXISTING ENTITY, or -1 if no duplicate exists.',
    )


def _coerce_int(value: Any) -> int | None:
    """Coerce an integer-coercible value to int; bools and failures return None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class NodeResolutions(BaseModel):
    entity_resolutions: list[NodeDuplicate] = Field(
        default_factory=list, description='List of resolved nodes'
    )

    @model_validator(mode='before')
    @classmethod
    def _normalize_entity_resolutions(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        raw = d.get('entity_resolutions')
        if raw is None or not isinstance(raw, list):
            d['entity_resolutions'] = []
            return d
        normalized: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            coerced_id = _coerce_int(item.get('id'))
            if coerced_id is None:
                continue
            item['id'] = coerced_id
            if item.get('name') is None:
                item['name'] = ''
            coerced_dup = _coerce_int(item.get('duplicate_candidate_id'))
            item['duplicate_candidate_id'] = -1 if coerced_dup is None else coerced_dup
            normalized.append(item)
        d['entity_resolutions'] = normalized
        return d


class Prompt(Protocol):
    node: PromptVersion
    node_list: PromptVersion
    nodes: PromptVersion


class Versions(TypedDict):
    node: PromptFunction
    node_list: PromptFunction
    nodes: PromptFunction


# GPT-5.6+ models (including GPT-6 Luna) reuse cached prompt prefixes only at message boundaries,
# and OpenAI requires >= 1,024 tokens in the cached prefix. Keep all static rules, output rules,
# and disambiguation examples in the system message so only per-call context (messages, entities,
# existing candidates, and batch counts) stays in the user message.
_DEDUPE_NODES_CORE_RULES = """Entities should only be considered duplicates if they refer to the *same real-world object or concept*.
Semantic Equivalence: if an abbreviation, shorthand, or descriptive label in EXISTING ENTITIES clearly refers to the same named entity in context, treat them as duplicates.

NEVER mark entities as duplicates if:
- They are related but distinct.
- They have similar names or purposes but refer to separate instances, owners, or concepts.
- One is a part, module, or component inside a broader service or container.
- They denote distinct versions or deployment environments (e.g., staging vs production, PostgreSQL 14 vs PostgreSQL 16).
- They denote different people sharing a first name, or a generic role not clearly bound to a named person in context.
- One is a relative location ("the suburbs", "downtown") and the other is a specific named city.

Output rules:
- Only return a `candidate_id` that appears in EXISTING ENTITIES; return `-1` when there is no match, EXISTING ENTITIES is empty, or you are unsure.
- Preserve the original name unless a duplicate has a more complete canonical name; never fabricate details or include `{}`."""

def _format_dedupe_node_examples(item_label: str) -> str:
    return f"""<EXAMPLE>
{item_label}: "Sam" (Person)
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Sam", "entity_types": ["Person"], "summary": "Sam enjoys hiking and photography"}}]
Result: duplicate_candidate_id = 0 (same person referenced in conversation)

{item_label}: "NYC"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "New York City", "entity_types": ["Location"]}}, {{"candidate_id": 1, "name": "New York Knicks", "entity_types": ["Organization"]}}]
Result: duplicate_candidate_id = 0 (same location, abbreviated name)

{item_label}: "Java" (programming language)
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Java", "entity_types": ["Location"], "summary": "An island in Indonesia"}}]
Result: duplicate_candidate_id = -1 (same name but distinct real-world things)

{item_label}: "Marco's car"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Marco's vehicle", "entity_types": ["Entity"], "summary": "Marco drives a red sedan."}}]
Result: duplicate_candidate_id = 0 (synonym — "car" and "vehicle" refer to the same thing, same possessor)

{item_label}: "the suburbs"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Chicago", "entity_types": ["Location"], "summary": "A city where someone lives"}}]
Result: duplicate_candidate_id = -1 (relative/descriptive locations such as the suburbs, downtown, or countryside are never the same object as a specific named city merely because they are related or appear in movement context)

{item_label}: "Alice's laptop"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Bob's laptop", "entity_types": ["Entity"], "summary": "Bob's work laptop"}}]
Result: duplicate_candidate_id = -1 (different owners — distinct physical objects)

{item_label}: "auth middleware"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "backend API", "entity_types": ["Entity"], "summary": "Main HTTP service handling client requests"}}]
Result: duplicate_candidate_id = -1 (component is distinct from its parent service)

{item_label}: "staging cluster"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "production cluster", "entity_types": ["Entity"], "summary": "Customer-facing Kubernetes cluster"}}]
Result: duplicate_candidate_id = -1 (separate environments must never be merged)

{item_label}: "Postgres"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "PostgreSQL", "entity_types": ["Entity"], "summary": "Relational database used for order records"}}]
Result: duplicate_candidate_id = 0 (shorthand for the same database; keep fuller name "PostgreSQL")

{item_label}: "Alex Chen"
EXISTING ENTITIES: [{{"candidate_id": 0, "name": "Alex Rivera", "entity_types": ["Person"], "summary": "Product manager on the mobile team"}}]
Result: duplicate_candidate_id = -1 (different surnames — distinct people)
</EXAMPLE>"""


_DEDUPE_NODE_SYSTEM_PROMPT = f"""You are an entity deduplication assistant. NEVER fabricate entity names or mark distinct entities as duplicates.

You will receive PREVIOUS MESSAGES, CURRENT MESSAGE, NEW ENTITY, ENTITY TYPE DESCRIPTION, and EXISTING ENTITIES in the user message.
Determine whether NEW ENTITY is a duplicate of any candidate in EXISTING ENTITIES.

{_DEDUPE_NODES_CORE_RULES}

Task:
1. Compare NEW ENTITY against each EXISTING ENTITY (identified by `candidate_id`), using PREVIOUS MESSAGES, CURRENT MESSAGE, and ENTITY TYPE DESCRIPTION for disambiguation.
2. If NEW ENTITY refers to the same real-world object or concept as an existing candidate, return that candidate's `candidate_id` as `duplicate_candidate_id`.
3. Return `duplicate_candidate_id = -1` when there is no match or you are unsure.

{_format_dedupe_node_examples('NEW ENTITY')}"""

_DEDUPE_NODES_SYSTEM_PROMPT = f"""You are an entity deduplication assistant. NEVER fabricate entity names or mark distinct entities as duplicates.

You will receive PREVIOUS MESSAGES, CURRENT MESSAGE, ENTITIES, and EXISTING ENTITIES in the user message.
Each entity in ENTITIES was extracted from CURRENT MESSAGE.
For each entity in ENTITIES, determine if it is a duplicate of any EXISTING ENTITY.

{_DEDUPE_NODES_CORE_RULES}

Task:
For every entity in ENTITIES (matching the exact count and integer ID range specified in the user message — do not skip or add IDs), provide:
- `id`: integer id from ENTITIES
- `name`: the best full name for the entity (preserve the original name unless a duplicate has a more complete name)
- `duplicate_candidate_id`: the `candidate_id` of the EXISTING ENTITY that is the best duplicate match, or -1 if there is no duplicate

{_format_dedupe_node_examples('ENTITY')}"""


def node(context: dict[str, Any]) -> list[Message]:
    return [
        Message(
            role='system',
            content=_DEDUPE_NODE_SYSTEM_PROMPT,
        ),
        Message(
            role='user',
            content=f"""
<PREVIOUS MESSAGES>
{to_prompt_json(context['previous_episodes'])}
</PREVIOUS MESSAGES>

<CURRENT MESSAGE>
{context['episode_content']}
</CURRENT MESSAGE>

<NEW ENTITY>
{to_prompt_json(context['extracted_node'])}
</NEW ENTITY>

<ENTITY TYPE DESCRIPTION>
{to_prompt_json(context['entity_type_description'])}
</ENTITY TYPE DESCRIPTION>

<EXISTING ENTITIES>
{to_prompt_json(context['existing_nodes'])}
</EXISTING ENTITIES>
""",
        ),
    ]


def nodes(context: dict[str, Any]) -> list[Message]:
    entity_count = len(context['extracted_nodes'])
    return [
        Message(
            role='system',
            content=_DEDUPE_NODES_SYSTEM_PROMPT,
        ),
        Message(
            role='user',
            content=f"""
<PREVIOUS MESSAGES>
{to_prompt_json(context['previous_episodes'])}
</PREVIOUS MESSAGES>

<CURRENT MESSAGE>
{context['episode_content']}
</CURRENT MESSAGE>

<ENTITIES>
{to_prompt_json(context['extracted_nodes'])}
</ENTITIES>

<EXISTING ENTITIES>
{to_prompt_json(context['existing_nodes'])}
</EXISTING ENTITIES>

ENTITIES contains {entity_count} entities with IDs 0 through {entity_count - 1}.
Your response MUST include EXACTLY {entity_count} resolutions with IDs 0 through {entity_count - 1}. Do not skip or add IDs.
""",
        ),
    ]


def node_list(context: dict[str, Any]) -> list[Message]:
    return [
        Message(
            role='system',
            content='You are an entity deduplication assistant that groups duplicate nodes by UUID.',
        ),
        Message(
            role='user',
            content=f"""
Given the following context, deduplicate a list of nodes:

<NODES>
{to_prompt_json(context['nodes'])}
</NODES>

Task:
1. Group nodes together such that all duplicate nodes are in the same list of uuids.
2. All duplicate uuids should be grouped together in the same list.
3. Also return a new summary that synthesizes the summaries into a new short summary.

Guidelines:
1. Each uuid from the list of nodes should appear EXACTLY once in your response.
2. If a node has no duplicates, it should appear in the response in a list of only one uuid.

<EXAMPLE>
Input nodes:
[
  {{"uuid": "a1", "name": "NYC", "summary": "New York City"}},
  {{"uuid": "b2", "name": "New York City", "summary": "The city of New York"}},
  {{"uuid": "c3", "name": "Los Angeles", "summary": "City in California"}}
]

Result:
[
  {{"uuids": ["a1", "b2"], "summary": "New York City, also known as NYC"}},
  {{"uuids": ["c3"], "summary": "City in California"}}
]
</EXAMPLE>
""",
        ),
    ]


versions: Versions = {'node': node, 'node_list': node_list, 'nodes': nodes}


class DedupeNodesPrompts(ABC):
    @abstractmethod
    def node(self, context: dict[str, Any]) -> ChatPrompt: ...

    @abstractmethod
    def node_list(self, context: dict[str, Any]) -> ChatPrompt: ...

    @abstractmethod
    def nodes(self, context: dict[str, Any]) -> ChatPrompt: ...


class DefaultDedupeNodesPrompts(DedupeNodesPrompts):
    def node(self, context: dict[str, Any]) -> ChatPrompt:
        return ChatPrompt.from_messages(node(context))

    def node_list(self, context: dict[str, Any]) -> ChatPrompt:
        return ChatPrompt.from_messages(node_list(context))

    def nodes(self, context: dict[str, Any]) -> ChatPrompt:
        return ChatPrompt.from_messages(nodes(context))
