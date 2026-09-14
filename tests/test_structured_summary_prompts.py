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

from typing import Any

from graphiti_core.prompts.summarize_nodes import (
    summarize_context,
    summarize_pair,
    summary_description,
    versions,
)


def test_summarize_context_message_roles_and_order() -> None:
    messages = summarize_context({})
    assert len(messages) == 2
    assert messages[0].role == 'system'
    assert messages[1].role == 'user'


def test_summarize_pair_message_roles_and_order() -> None:
    messages = summarize_pair({})
    assert len(messages) == 2
    assert messages[0].role == 'system'
    assert messages[1].role == 'user'


def test_summarize_context_exact_system_string() -> None:
    messages = summarize_context({})
    assert messages[0].content == (
        'You are a concise knowledge-graph assistant. '
        'Output structured key:value facts only. No prose, no explanation.'
    )


def test_summarize_pair_exact_system_string() -> None:
    messages = summarize_pair({})
    assert messages[0].content == (
        'You are a concise knowledge-graph assistant. '
        'Merge two structured summaries into one. Output key:value pairs only.'
    )


def test_summarize_context_exact_user_policy_phrases() -> None:
    prompt = summarize_context({})[1].content
    assert 'Summarize the ENTITY using ONLY facts from the MESSAGES.' in prompt
    assert "Format: key:value pairs separated by ' | '. Max 150 characters total." in prompt
    assert 'Focus on: what it is, its role/status, key attributes. Omit filler words.' in prompt


def test_summarize_context_exact_good_bad_examples() -> None:
    prompt = summarize_context({})[1].content
    assert (
        'Good example: "project:cth.mcp.memory | stack:Neo4j+SQLite | status:M4 active '
        '| role:memory graph"' in prompt
    )
    assert (
        'Bad example: "The cth.mcp.memory system is a project that uses Neo4j. '
        'It is currently in M4 phase."' in prompt
    )


def test_summarize_context_interpolates_tagged_sections() -> None:
    context: dict[str, Any] = {
        'previous_episodes': 'PREV_EPISODE_TEXT',
        'episode_content': 'EPISODE_CONTENT_TEXT',
        'node_name': 'NODE_NAME_TEXT',
        'node_summary': 'NODE_SUMMARY_TEXT',
    }
    prompt = summarize_context(context)[1].content
    assert '<MESSAGES>' in prompt and '</MESSAGES>' in prompt
    assert '<ENTITY>' in prompt and '</ENTITY>' in prompt
    assert '<ENTITY CONTEXT>' in prompt and '</ENTITY CONTEXT>' in prompt
    messages_block = prompt.split('<MESSAGES>')[1].split('</MESSAGES>')[0]
    assert 'PREV_EPISODE_TEXT' in messages_block
    assert 'EPISODE_CONTENT_TEXT' in messages_block
    entity_block = prompt.split('<ENTITY>')[1].split('</ENTITY>')[0]
    assert 'NODE_NAME_TEXT' in entity_block
    entity_context_block = prompt.split('<ENTITY CONTEXT>')[1].split('</ENTITY CONTEXT>')[0]
    assert 'NODE_SUMMARY_TEXT' in entity_context_block


def test_summarize_context_tolerates_missing_keys() -> None:
    messages = summarize_context({})
    assert isinstance(messages[1].content, str)


def test_summarize_context_tolerates_empty_values() -> None:
    messages = summarize_context(
        {'previous_episodes': '', 'episode_content': '', 'node_name': '', 'node_summary': ''}
    )
    assert isinstance(messages[1].content, str)


def test_summarize_context_has_no_attributes_block() -> None:
    prompt = summarize_context({'attributes': {'status': 'active'}})[1].content
    assert 'ATTRIBUTES' not in prompt
    assert "{'status': 'active'}" not in prompt


def test_summarize_pair_exact_user_policy_phrases() -> None:
    prompt = summarize_pair({})[1].content
    assert 'Merge these two summaries into one structured key:value summary.' in prompt
    assert "Format: key:value pairs separated by ' | '. Max 150 characters total." in prompt
    assert 'Keep the most current/specific values. Drop duplicates.' in prompt
    assert 'Summaries:' in prompt


def test_summarize_pair_interpolates_summaries_raw() -> None:
    context: dict[str, Any] = {'node_summaries': 'SUMMARY_A | SUMMARY_B'}
    prompt = summarize_pair(context)[1].content
    assert 'SUMMARY_A | SUMMARY_B' in prompt


def test_summarize_pair_tolerates_missing_keys() -> None:
    messages = summarize_pair({})
    assert isinstance(messages[1].content, str)


def test_versions_map_directly_to_native_functions() -> None:
    assert versions['summarize_context'] is summarize_context
    assert versions['summarize_pair'] is summarize_pair
    assert versions['summary_description'] is summary_description


def test_summary_description_still_uses_json_serialization() -> None:
    prompt = summary_description({'summary': {'a': 1}})[1].content
    assert '"a": 1' in prompt
