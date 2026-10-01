"""Tests for the cacheable system/user split of the combined extraction prompt.

GPT-5.6+ models only reuse cached prefixes at message boundaries, so all static
instruction text must live in the system message and only per-call content in
the user message. These tests pin the split and the byte-level preservation of
the instruction text.
"""

from graphiti_core.prompts.extract_nodes_and_edges import extract_message

CONTEXT_A = {
    'entity_types': [{'entity_type_id': 1, 'entity_type_name': 'Person'}],
    'edge_types': [{'fact_type_name': 'WORKS_AT', 'description': 'employment'}],
    'previous_episodes': [{'role': 'user', 'content': 'earlier chat'}],
    'episode_content': 'James: I bought a new guitar today!',
    'custom_extraction_instructions': 'Prefer musical instrument entities.',
}

CONTEXT_B = {
    'entity_types': [{'entity_type_id': 1, 'entity_type_name': 'Person'}],
    'edge_types': [{'fact_type_name': 'WORKS_AT', 'description': 'employment'}],
    'previous_episodes': [{'role': 'assistant', 'content': 'different prior context'}],
    'episode_content': 'Sarah: Planning a trip to Tokyo next week.',
    'custom_extraction_instructions': '',
}

# sha256 of the pre-split prompt (menhir/main bc0ba9c, single system + single user message) for
# CONTEXT_A. The split must reproduce it exactly: persona == old system message, and
# '\n' + <rest of new system> + <new user> == old user message. Any intentional prompt edit must
# update these pins.
_OLD_SYSTEM_SHA256 = 'a20d106ed76c8414ab95a029f89c568b062658bb4d2cbf7faae018ad98278b26'
_OLD_USER_SHA256 = '85e783e20636484bcbbcc825d7b23e092c4798c562a361b609569d8e6a216fbf'


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def test_static_instructions_live_in_system_message():
    [system, user] = extract_message(CONTEXT_A)

    assert 'ENTITY RULES:' in system.content
    assert '</NEGATIVE EXAMPLES>' in system.content
    assert '<ENTITY TYPES>' in system.content
    assert '<FACT TYPES>' in system.content
    assert '<CURRENT MESSAGES>' not in system.content
    assert '<PREVIOUS MESSAGES>' not in system.content

    assert '<PREVIOUS MESSAGES>' in user.content
    assert '<CURRENT MESSAGES>' in user.content
    assert 'ENTITY RULES:' not in user.content


def test_system_message_is_independent_of_per_call_content():
    [system_a, _] = extract_message(CONTEXT_A)
    [system_b, _] = extract_message(CONTEXT_B)

    assert system_a.content == system_b.content


def test_split_reproduces_the_previous_prompt_byte_for_byte():
    [system, user] = extract_message(CONTEXT_A)
    persona, static_block = system.content.split('\n\n', 1)

    assert _sha256(persona) == _OLD_SYSTEM_SHA256
    assert _sha256('\n' + static_block + user.content) == _OLD_USER_SHA256
