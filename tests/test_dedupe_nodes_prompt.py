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

from copy import deepcopy
from typing import Any

from graphiti_core.prompts.dedupe_nodes import node, node_list, nodes, versions

ANTI_CONFLATION_NODE_MARKER = 'NEW ENTITY: "the suburbs"'
ANTI_CONFLATION_NODES_MARKER = 'ENTITY: "the suburbs"'
ANTI_CONFLATION_RESULT = (
    'Result: duplicate_candidate_id = -1 (relative/descriptive locations such as the suburbs, '
    'downtown, or countryside are never the same object as a specific named city merely because '
    'they are related or appear in movement context)'
)


def _node_context() -> dict[str, Any]:
    return {
        'previous_episodes': [],
        'episode_content': 'Moved to the suburbs',
        'extracted_node': {'name': 'the suburbs', 'entity_types': ['Location']},
        'entity_type_description': '',
        'existing_nodes': [],
    }


def _nodes_context() -> dict[str, Any]:
    return {
        'previous_episodes': [],
        'episode_content': 'Moved to the suburbs',
        'extracted_nodes': [{'name': 'the suburbs', 'entity_types': ['Location']}],
        'existing_nodes': [],
    }


def test_node_prompt_renders_anti_conflation_example_exactly_once() -> None:
    content = node(_node_context())[1].content
    assert content.count(ANTI_CONFLATION_NODE_MARKER) == 1
    assert content.count(ANTI_CONFLATION_RESULT) == 1


def test_nodes_prompt_renders_anti_conflation_example_exactly_once() -> None:
    content = nodes(_nodes_context())[1].content
    assert content.count(ANTI_CONFLATION_NODES_MARKER) == 1
    assert content.count(ANTI_CONFLATION_RESULT) == 1


def test_versions_node_entry_renders_anti_conflation_example_exactly_once() -> None:
    content = versions['node'](_node_context())[1].content
    assert content.count(ANTI_CONFLATION_NODE_MARKER) == 1
    assert content.count(ANTI_CONFLATION_RESULT) == 1


def test_versions_nodes_entry_renders_anti_conflation_example_exactly_once() -> None:
    content = versions['nodes'](_nodes_context())[1].content
    assert content.count(ANTI_CONFLATION_NODES_MARKER) == 1
    assert content.count(ANTI_CONFLATION_RESULT) == 1


def test_versions_map_unchanged_shape() -> None:
    assert set(versions.keys()) == {'node', 'node_list', 'nodes'}
    assert versions['node'] is node
    assert versions['nodes'] is nodes
    assert versions['node_list'] is node_list


def test_node_list_prompt_has_no_anti_conflation_leakage() -> None:
    content = node_list({'nodes': []})[1].content
    assert ANTI_CONFLATION_NODE_MARKER not in content
    assert ANTI_CONFLATION_NODES_MARKER not in content
    assert ANTI_CONFLATION_RESULT not in content
    assert 'the suburbs' not in content
    assert 'Chicago' not in content


def test_rendered_json_braces_are_native_single_braces() -> None:
    node_content = node(_node_context())[1].content
    nodes_content = nodes(_nodes_context())[1].content
    chicago_line = (
        'EXISTING ENTITIES: [{"candidate_id": 0, "name": "Chicago", '
        '"entity_types": ["Location"], "summary": "A city where someone lives"}]'
    )
    assert chicago_line in node_content
    assert chicago_line in nodes_content
    assert '{{' not in node_content
    assert '}}' not in node_content
    assert '{{' not in nodes_content
    assert '}}' not in nodes_content


def test_node_prompt_preserves_existing_examples_and_response_contract() -> None:
    content = node(_node_context())[1].content
    assert 'NEW ENTITY: "Sam" (Person)' in content
    assert 'NEW ENTITY: "NYC"' in content
    assert 'NEW ENTITY: "Java" (programming language)' in content
    assert 'NEW ENTITY: "Marco\'s car"' in content
    assert (
        'Return `duplicate_candidate_id = -1` when there is no match or you are unsure.' in content
    )
    assert 'duplicate_candidate_id = -1 (same name but distinct real-world things)' in content


def test_nodes_prompt_preserves_existing_examples_and_response_contract() -> None:
    content = nodes(_nodes_context())[1].content
    assert 'ENTITY: "Sam" (Person)' in content
    assert 'ENTITY: "NYC"' in content
    assert 'ENTITY: "Java" (programming language)' in content
    assert 'ENTITY: "Marco\'s car"' in content
    assert '`duplicate_candidate_id`: the `candidate_id` of the EXISTING ENTITY' in content
    assert 'Your response MUST include EXACTLY 1 resolutions with IDs 0 through 0.' in content


def test_prompts_do_not_mutate_context() -> None:
    node_context = _node_context()
    node_context_before = deepcopy(node_context)
    node(node_context)

    nodes_context = _nodes_context()
    nodes_context_before = deepcopy(nodes_context)
    nodes(nodes_context)

    list_context = {'nodes': [{'uuid': 'a1', 'name': 'x'}]}
    list_context_before = deepcopy(list_context)
    node_list(list_context)

    assert node_context == node_context_before
    assert nodes_context == nodes_context_before
    assert list_context == list_context_before


def test_dedupe_and_summary_prompts_exclude_merge_lineage():
    from graphiti_core.prompts.extract_nodes import extract_summaries_batch

    lineage = {
        'merge_audit': ['{"absorbed_uuid": "a"}'],
        'merged_from': ['a'],
        'last_merge_op_id': 'op-1',
    }
    candidate = {
        **lineage,
        'edge_count': 4,
        'candidate_id': 0,
        'name': 'CYBERSYN',
        'entity_types': ['Entity'],
        'summary': 'schema',
    }
    dedupe = nodes(
        {
            'previous_episodes': [],
            'episode_content': 'Step 3: inspected CYBERSYN',
            'extracted_nodes': [
                {
                    'id': 0,
                    'name': 'CYBERSYN schema',
                    'entity_type': ['Entity'],
                    'entity_type_description': 'Default Entity Type',
                }
            ],
            'existing_nodes': [candidate],
        }
    )
    summaries = extract_summaries_batch(
        {
            'previous_episodes': [],
            'episode_content': 'Step 3: inspected CYBERSYN',
            'entities': [
                {
                    'name': 'CYBERSYN',
                    'summary': 'schema',
                    'entity_types': ['Entity'],
                    'attributes': lineage,
                }
            ],
        }
    )

    for prompt in (dedupe, summaries):
        text = ''.join(message.content for message in prompt)
        assert not any(key in text for key in lineage)
        assert 'CYBERSYN' in text
    assert '"edge_count": 4' in ''.join(message.content for message in dedupe)
