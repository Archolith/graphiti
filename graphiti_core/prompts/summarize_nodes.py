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

from typing import Any, Protocol, TypedDict

from pydantic import BaseModel, Field

from graphiti_core.utils.text_utils import MAX_SUMMARY_CHARS

from .models import Message, PromptFunction, PromptVersion
from .prompt_helpers import to_prompt_json


class Summary(BaseModel):
    summary: str = Field(
        ...,
        description=(
            f'Summary containing the important information about the entity. '
            f'Under {MAX_SUMMARY_CHARS} characters'
        ),
    )


class SummaryDescription(BaseModel):
    description: str = Field(..., description='One sentence description of the provided summary')


class Prompt(Protocol):
    summarize_pair: PromptVersion
    summarize_context: PromptVersion
    summary_description: PromptVersion


class Versions(TypedDict):
    summarize_pair: PromptFunction
    summarize_context: PromptFunction
    summary_description: PromptFunction


def summarize_pair(context: dict[str, Any]) -> list[Message]:
    return [
        Message(
            role='system',
            content='You are a concise knowledge-graph assistant. Merge two structured summaries into one. Output key:value pairs only.',
        ),
        Message(
            role='user',
            content=f"""
        Merge these two summaries into one structured key:value summary.
        Format: key:value pairs separated by ' | '. Max 150 characters total.
        Keep the most current/specific values. Drop duplicates.

        Summaries:
        {context.get('node_summaries', '')}
        """,
        ),
    ]


def summarize_context(context: dict[str, Any]) -> list[Message]:
    return [
        Message(
            role='system',
            content='You are a concise knowledge-graph assistant. Output structured key:value facts only. No prose, no explanation.',
        ),
        Message(
            role='user',
            content=f"""
        Summarize the ENTITY using ONLY facts from the MESSAGES.
        Format: key:value pairs separated by ' | '. Max 150 characters total.
        Focus on: what it is, its role/status, key attributes. Omit filler words.

        Good example: "project:cth.mcp.memory | stack:Neo4j+SQLite | status:M4 active | role:memory graph"
        Bad example: "The cth.mcp.memory system is a project that uses Neo4j. It is currently in M4 phase."

        <MESSAGES>
        {context.get('previous_episodes', '')}
        {context.get('episode_content', '')}
        </MESSAGES>

        <ENTITY>{context.get('node_name', '')}</ENTITY>
        <ENTITY CONTEXT>{context.get('node_summary', '')}</ENTITY CONTEXT>
        """,
        ),
    ]


def summary_description(context: dict[str, Any]) -> list[Message]:
    return [
        Message(
            role='system',
            content='You are a helpful assistant that describes provided contents in a single sentence.',
        ),
        Message(
            role='user',
            content=f"""
        Create a short one sentence description of the summary that explains what kind of information is summarized.
        Summaries must be under {MAX_SUMMARY_CHARS} characters.

        Summary:
        {to_prompt_json(context['summary'])}
        """,
        ),
    ]


versions: Versions = {
    'summarize_pair': summarize_pair,
    'summarize_context': summarize_context,
    'summary_description': summary_description,
}
