"""Focused tests for prompt JSON serialization in prompt_helpers."""

import json
from datetime import datetime

import pytest

from graphiti_core.prompts.prompt_helpers import to_prompt_json, without_merge_lineage


def test_baseline_json_behavior():
    data = {'a': 1, 'b': 'two', 'c': [1, 2, 3], 'd': None, 'e': True}
    assert to_prompt_json(data) == json.dumps(data)


def test_ensure_ascii_false_is_default():
    assert to_prompt_json({'name': '한국어'}) == '{"name": "한국어"}'


def test_ensure_ascii_true():
    assert to_prompt_json({'name': '한국어'}, ensure_ascii=True) == json.dumps(
        {'name': '한국어'}, ensure_ascii=True
    )


def test_indent():
    data = {'a': [1, 2]}
    assert to_prompt_json(data, indent=2) == json.dumps(data, indent=2)


def test_key_suffix_removed_even_when_value_is_not_a_long_vector():
    data = {'name_embedding': [1, 2, 3], 'name': 'kept'}
    assert to_prompt_json(data) == json.dumps({'name': 'kept'})


def test_structural_removal_at_length_65():
    vector = [0.5] * 65
    data = {'facts': 'x', 'vec': vector}
    result = json.loads(to_prompt_json(data))
    assert result == {'facts': 'x'}


def test_preservation_at_length_64():
    vector = [0.5] * 64
    data = {'vec': vector}
    assert json.loads(to_prompt_json(data)) == {'vec': vector}


def test_sampled_head_compatibility_boundary():
    vector = [1] * 8 + ['not-a-number'] + [2] * 56
    assert len(vector) == 65
    data = {'vec': vector}
    assert json.loads(to_prompt_json(data)) == {}


def test_long_bool_list_preserved():
    data = {'flags': [True, False] * 40}
    assert json.loads(to_prompt_json(data)) == {'flags': [True, False] * 40}


def test_long_string_list_preserved():
    data = {'words': ['a'] * 65}
    assert json.loads(to_prompt_json(data)) == {'words': ['a'] * 65}


def test_recursive_nested_structures_and_tuples():
    vector = [1.0] * 65
    data = {
        'outer': {
            'vec': vector,
            'keep': (1, 'two', 3.0),
            'list': [{'inner_vec': [2] * 65}, {'inner': 'ok'}],
        }
    }
    result = json.loads(to_prompt_json(data))
    assert result == {
        'outer': {'keep': [1, 'two', 3.0], 'list': [{}, {'inner': 'ok'}]}
    }


def test_input_not_mutated():
    vector = [1.0] * 65
    data = {'name_embedding': [1, 2], 'vec': vector, 'nested': {'more_embedding': 'x'}}
    original = json.dumps({'name_embedding': [1, 2], 'vec': vector, 'nested': {'more_embedding': 'x'}})
    to_prompt_json(data)
    assert json.dumps(data) == original
    assert isinstance(data['nested']['more_embedding'], str)


def test_isoformat_fallback():
    dt = datetime(2024, 5, 1, 12, 30, 45)
    data = {'when': dt}
    assert json.loads(to_prompt_json(data)) == {'when': '2024-05-01T12:30:45'}


def test_iso_format_fallback():
    class TemporalLike:
        def iso_format(self):
            return '2024-05-01T00:00:00Z'

    assert json.loads(to_prompt_json({'at': TemporalLike()})) == {'at': '2024-05-01T00:00:00Z'}


def test_to_native_fallback():
    class NativeLike:
        def to_native(self):
            return 42

    assert json.loads(to_prompt_json({'value': NativeLike()})) == {'value': 42}


def test_recursive_conversion_of_non_primitive_result():
    class Inner:
        def to_native(self):
            return 42

    class Wrapper:
        def isoformat(self):
            return Inner()

    assert json.loads(to_prompt_json({'w': Wrapper()})) == {'w': 42}


def test_conversion_returning_self_falls_back_to_str():
    class Selfish:
        def isoformat(self):
            return self

        def __str__(self):
            return 'selfish-str'

    assert json.loads(to_prompt_json({'s': Selfish()})) == {'s': 'selfish-str'}


def test_unsupported_object_falls_back_to_str():
    class Plain:
        def __str__(self):
            return 'plain-str'

    assert json.loads(to_prompt_json({'p': Plain()})) == {'p': 'plain-str'}


def test_isoformat_takes_precedence_over_later_methods():
    class Precedence:
        def isoformat(self):
            return 'from-isoformat'

        def iso_format(self):
            raise AssertionError('iso_format must not be called')

        def to_native(self):
            raise AssertionError('to_native must not be called')

    assert json.loads(to_prompt_json({'v': Precedence()})) == {'v': 'from-isoformat'}


def test_conversion_method_exception_propagates():
    class Exploding:
        def isoformat(self):
            raise ValueError('malformed conversion')

    with pytest.raises(ValueError, match='malformed conversion'):
        to_prompt_json({'v': Exploding()})


def test_merge_lineage_keys_removed_at_any_depth():
    data = {
        'name': 'Alice',
        'merge_audit': ['{"absorbed_uuid": "a"}'],
        'merged_from': ['a'],
        'last_merge_op_id': 'op-1',
        'attributes': {'merge_audit': ['x'], 'role': 'engineer'},
        'candidates': [{'candidate_id': 0, 'merged_from': ['b'], 'summary': 's'}],
    }
    original = json.loads(json.dumps(data))

    result = json.loads(to_prompt_json(data))

    assert result == {
        'name': 'Alice',
        'attributes': {'role': 'engineer'},
        'candidates': [{'candidate_id': 0, 'summary': 's'}],
    }
    assert data == original  # input not mutated


def test_without_merge_lineage_copies_and_keeps_other_keys():
    attributes = {'merge_audit': ['x'], 'merged_from': ['a'], 'last_merge_op_id': 'op', 'role': 'r'}

    assert without_merge_lineage(attributes) == {'role': 'r'}
    assert 'merge_audit' in attributes
