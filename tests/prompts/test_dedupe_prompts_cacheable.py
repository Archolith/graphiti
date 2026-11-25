"""Tests for cacheable static-first system/user split of dedupe prompts (Menhir #91).

GPT-5.6+ models (including GPT-6 Luna) reuse cached prompt prefixes only at
message boundaries, and OpenAI requires at least 1,024 tokens in the cached
prefix. These tests verify that all static instructions and disambiguation
examples live in the system message, only per-call dynamic data lives in the
user message, positional references match the new order, and the system prefix
clears the 1,024-token OpenAI cache floor.
"""

from __future__ import annotations

import pytest

from graphiti_core.prompts import dedupe_edges, dedupe_nodes

EDGE_CONTEXT_A = {
    'existing_edges': '[{"idx": 0, "fact": "Alice lives in Austin"}]',
    'edge_invalidation_candidates': '[{"idx": 1, "fact": "Alice lives in Boston"}]',
    'new_edge': 'Alice moved to Seattle',
}

EDGE_CONTEXT_B = {
    'existing_edges': '[]',
    'edge_invalidation_candidates': '[{"idx": 0, "fact": "Service uses MySQL"}]',
    'new_edge': 'Service migrated to PostgreSQL',
}

NODES_CONTEXT_A = {
    'previous_episodes': ['Earlier chat about travel'],
    'episode_content': 'User: I drove to Austin yesterday.',
    'extracted_nodes': [{'id': 0, 'name': 'Austin'}],
    'existing_nodes': [{'candidate_id': 0, 'name': 'Austin, TX', 'entity_types': ['Location']}],
}

NODES_CONTEXT_B = {
    'previous_episodes': ['Different prior message', 'Another turn'],
    'episode_content': 'User: Deploying the billing service to staging and prod.',
    'extracted_nodes': [
        {'id': 0, 'name': 'billing service'},
        {'id': 1, 'name': 'staging'},
        {'id': 2, 'name': 'prod'},
    ],
    'existing_nodes': [],
}

NODE_CONTEXT_A = {
    'previous_episodes': ['Earlier chat'],
    'episode_content': 'User: I use Postgres.',
    'extracted_node': {'name': 'Postgres'},
    'entity_type_description': 'Database engine',
    'existing_nodes': [{'candidate_id': 0, 'name': 'PostgreSQL', 'entity_types': ['Entity']}],
}

NODE_CONTEXT_B = {
    'previous_episodes': [],
    'episode_content': 'User: Met Alex Chen.',
    'extracted_node': {'name': 'Alex Chen'},
    'entity_type_description': 'Person',
    'existing_nodes': [{'candidate_id': 0, 'name': 'Alex Rivera', 'entity_types': ['Person']}],
}


def test_resolve_edge_static_instructions_live_in_system_message():
    [system, user] = dedupe_edges.resolve_edge(EDGE_CONTEXT_A)

    assert system.role == 'system'
    assert user.role == 'user'
    assert '1. DUPLICATE DETECTION:' in system.content
    assert '2. CONTRADICTION DETECTION:' in system.content
    assert 'DRI for the project' in system.content
    assert '<EXAMPLE>' in system.content
    assert '<EXISTING FACTS>' not in system.content
    assert '<FACT INVALIDATION CANDIDATES>' not in system.content
    assert '<NEW FACT>' not in system.content
    assert 'Alice lives in Austin' not in system.content

    assert '<EXISTING FACTS>' in user.content
    assert '<FACT INVALIDATION CANDIDATES>' in user.content
    assert '<NEW FACT>' in user.content
    assert 'Alice moved to Seattle' in user.content
    assert '1. DUPLICATE DETECTION:' not in user.content
    assert '<EXAMPLE>' not in user.content


def test_resolve_edge_system_message_is_identical_across_contexts():
    [sys_a, _] = dedupe_edges.resolve_edge(EDGE_CONTEXT_A)
    [sys_b, _] = dedupe_edges.resolve_edge(EDGE_CONTEXT_B)

    assert sys_a.content == sys_b.content


def test_dedupe_nodes_static_instructions_live_in_system_message():
    [system, user] = dedupe_nodes.nodes(NODES_CONTEXT_A)

    assert system.role == 'system'
    assert user.role == 'user'
    assert 'the suburbs' in system.content
    assert 'NEVER mark entities as duplicates if:' in system.content
    assert '<EXAMPLE>' in system.content
    assert 'above ENTITIES' not in system.content
    assert '<ENTITIES>' not in system.content
    assert '<EXISTING ENTITIES>' not in system.content
    assert '<CURRENT MESSAGE>' not in system.content
    assert 'Austin' not in system.content

    assert '<PREVIOUS MESSAGES>' in user.content
    assert '<CURRENT MESSAGE>' in user.content
    assert '<ENTITIES>' in user.content
    assert '<EXISTING ENTITIES>' in user.content
    assert 'ENTITIES contains 1 entities with IDs 0 through 0.' in user.content
    assert '<EXAMPLE>' not in user.content


def test_dedupe_nodes_system_message_is_identical_across_contexts():
    [sys_a, user_a] = dedupe_nodes.nodes(NODES_CONTEXT_A)
    [sys_b, user_b] = dedupe_nodes.nodes(NODES_CONTEXT_B)

    assert sys_a.content == sys_b.content
    assert 'ENTITIES contains 1 entities with IDs 0 through 0.' in user_a.content
    assert 'ENTITIES contains 3 entities with IDs 0 through 2.' in user_b.content


def test_dedupe_node_single_static_instructions_live_in_system_message():
    [sys_a, user_a] = dedupe_nodes.node(NODE_CONTEXT_A)
    [sys_b, _] = dedupe_nodes.node(NODE_CONTEXT_B)

    assert sys_a.content == sys_b.content
    assert '<NEW ENTITY>' not in sys_a.content
    assert '<EXISTING ENTITIES>' not in sys_a.content
    assert '<NEW ENTITY>' in user_a.content
    assert '<EXISTING ENTITIES>' in user_a.content
    assert 'the suburbs' in sys_a.content


def test_default_prompt_wrappers_preserve_cacheable_split():
    edge_chat = dedupe_edges.DefaultDedupeEdgesPrompts().resolve_edge(EDGE_CONTEXT_A)
    nodes_chat = dedupe_nodes.DefaultDedupeNodesPrompts().nodes(NODES_CONTEXT_A)
    node_chat = dedupe_nodes.DefaultDedupeNodesPrompts().node(NODE_CONTEXT_A)

    assert edge_chat.system is not None and edge_chat.system.role == 'system'
    assert '<EXISTING FACTS>' not in edge_chat.system.content
    assert nodes_chat.system is not None and nodes_chat.system.role == 'system'
    assert '<EXISTING ENTITIES>' not in nodes_chat.system.content
    assert node_chat.system is not None and node_chat.system.role == 'system'
    assert '<EXISTING ENTITIES>' not in node_chat.system.content


def test_dedupe_system_prompts_clear_openai_cache_threshold():
    tiktoken = pytest.importorskip('tiktoken')
    enc = tiktoken.get_encoding('o200k_base')

    for name, messages in (
        ('dedupe_edges.resolve_edge', dedupe_edges.resolve_edge(EDGE_CONTEXT_A)),
        ('dedupe_nodes.nodes', dedupe_nodes.nodes(NODES_CONTEXT_A)),
        ('dedupe_nodes.node', dedupe_nodes.node(NODE_CONTEXT_A)),
    ):
        tokens = len(enc.encode(messages[0].content))
        assert tokens >= 1024, f'{name} system prompt is {tokens} tokens (< 1024 OpenAI cache floor)'
