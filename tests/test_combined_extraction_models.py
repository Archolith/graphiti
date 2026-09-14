"""Focused tests for the fork-native CombinedExtraction sanitization validator.

Covers the generic half of Menhir installer #2
(``_patch_graphiti_combined_extraction_models``): native before-validator
sanitization of malformed rows in
``graphiti_core/prompts/extract_nodes_and_edges.py``. No endpoint synthesis,
Menhir policy, receipt, hook, or suppression behavior belongs in this phase.
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from graphiti_core.prompts.extract_nodes_and_edges import CombinedExtraction

VALID_ENTITY = {'name': 'Acme Corp', 'entity_type_id': 3}
VALID_EDGE = {
    'source_entity_name': 'Acme Corp',
    'target_entity_name': 'Bob',
    'relation_type': 'EMPLOYS',
    'fact': 'Acme Corp employs Bob.',
    'episode_indices': [1],
}


def _payload(entities: Any, edges: Any) -> dict[str, Any]:
    return {'extracted_entities': entities, 'edges': edges}


def test_schema_marks_both_arrays_required_with_descriptions() -> None:
    schema = CombinedExtraction.model_json_schema()
    assert schema['required'] == ['extracted_entities', 'edges']
    assert schema['properties']['extracted_entities']['description'] == 'List of extracted entities'
    assert schema['properties']['edges']['description'] == 'List of extracted relationship facts'


def test_missing_arrays_sanitize_to_empty_lists() -> None:
    result = CombinedExtraction.model_validate({})
    assert result.extracted_entities == []
    assert result.edges == []


def test_non_list_arrays_sanitize_to_empty_lists() -> None:
    result = CombinedExtraction.model_validate(_payload('not a list', 42))
    assert result.extracted_entities == []
    assert result.edges == []


def test_null_arrays_sanitize_to_empty_lists() -> None:
    result = CombinedExtraction.model_validate(_payload(None, None))
    assert result.extracted_entities == []
    assert result.edges == []


def test_non_dict_top_level_still_fails_normally() -> None:
    with pytest.raises(ValidationError):
        CombinedExtraction.model_validate([VALID_ENTITY])
    with pytest.raises(ValidationError):
        CombinedExtraction.model_validate('garbage')


def test_entity_canonical_name_wins_over_aliases() -> None:
    entity = {
        'name': 'Canonical',
        'entity_name': 'Alias One',
        'entity': 'Alias Two',
        'entity_type_id': 1,
    }
    result = CombinedExtraction.model_validate(_payload([entity], []))
    assert result.extracted_entities[0].name == 'Canonical'
    assert len(result.extracted_entities) == 1


def test_entity_entity_name_alias_used_when_canonical_invalid() -> None:
    entity = {'name': '   ', 'entity_name': 'Alias One', 'entity': 'Alias Two', 'entity_type_id': 2}
    result = CombinedExtraction.model_validate(_payload([entity], []))
    assert result.extracted_entities[0].name == 'Alias One'


def test_entity_entity_alias_used_when_canonical_and_entity_name_invalid() -> None:
    entity = {'name': 7, 'entity_name': None, 'entity': 'Alias Two', 'entity_type_id': 3}
    result = CombinedExtraction.model_validate(_payload([entity], []))
    assert result.extracted_entities[0].name == 'Alias Two'


def test_entity_name_is_trimmed() -> None:
    entity = {'name': '  Acme Corp  ', 'entity_type_id': 1}
    result = CombinedExtraction.model_validate(_payload([entity], []))
    assert result.extracted_entities[0].name == 'Acme Corp'


def test_entity_type_id_coercion_and_fallback() -> None:
    entities = [
        {'name': 'A', 'entity_type_id': '5'},
        {'name': 'B', 'entity_type_id': None},
        {'name': 'C', 'entity_type_id': 'oops'},
        {'name': 'D'},
        {'name': 'E', 'entity_type_id': True},
    ]
    result = CombinedExtraction.model_validate(_payload(entities, []))
    assert [e.entity_type_id for e in result.extracted_entities] == [5, -1, -1, -1, 1]


def test_entity_rows_without_any_valid_name_are_dropped() -> None:
    entities = [
        {'name': '', 'entity_type_id': 1},
        {'entity_name': '   ', 'entity_type_id': 2},
        {'entity': None, 'entity_type_id': 3},
        {'entity_type_id': 4},
    ]
    result = CombinedExtraction.model_validate(_payload(entities, []))
    assert result.extracted_entities == []


def test_entity_extra_fields_are_dropped() -> None:
    entity = {'name': 'Acme', 'entity_type_id': 1, 'episode_indices': [2], 'junk': 'x'}
    result = CombinedExtraction.model_validate(_payload([entity], []))
    assert result.extracted_entities[0].model_dump() == {'name': 'Acme', 'entity_type_id': 1}


def test_edge_non_dict_and_missing_required_field_rows_dropped() -> None:
    edges = [
        'not a dict',
        {},
        {**VALID_EDGE, 'source_entity_name': ''},
        {**VALID_EDGE, 'target_entity_name': '   '},
        {**VALID_EDGE, 'relation_type': None},
        {**VALID_EDGE, 'fact': 5},
    ]
    result = CombinedExtraction.model_validate(_payload([], edges))
    assert result.edges == []


def test_edge_required_strings_preserved_exactly() -> None:
    edge = {
        'source_entity_name': ' Padded Source ',
        'target_entity_name': 'Target\tName',
        'relation_type': ' KNOWS ',
        'fact': '  Fact with leading and trailing spaces.  ',
        'episode_indices': [0],
    }
    result = CombinedExtraction.model_validate(_payload([], [edge]))
    retained = result.edges[0]
    assert retained.source_entity_name == ' Padded Source '
    assert retained.target_entity_name == 'Target\tName'
    assert retained.relation_type == ' KNOWS '
    assert retained.fact == '  Fact with leading and trailing spaces.  '


def test_edge_episode_indices_filtering_and_default() -> None:
    missing_indices_edge = dict(VALID_EDGE)
    del missing_indices_edge['episode_indices']
    edges = [
        {**VALID_EDGE, 'episode_indices': [1, True, '2', 2.0, False, 3]},
        {**VALID_EDGE, 'episode_indices': [True, 'x']},
        missing_indices_edge,
        {**VALID_EDGE, 'episode_indices': None},
        {**VALID_EDGE, 'episode_indices': 'nope'},
    ]
    result = CombinedExtraction.model_validate(_payload([], edges))
    assert [e.episode_indices for e in result.edges] == [[1, 3], [0], [0], [0], [0]]


def test_edge_extra_fields_are_dropped() -> None:
    edge = {**VALID_EDGE, 'weight': 3, 'extra': {'a': 1}}
    result = CombinedExtraction.model_validate(_payload([], [edge]))
    assert result.edges[0].model_dump() == {**VALID_EDGE}


def test_input_dict_is_not_mutated() -> None:
    payload = _payload([dict(VALID_ENTITY)], [dict(VALID_EDGE, junk='x')])
    snapshot: dict[str, Any] = {
        'extracted_entities': [dict(payload['extracted_entities'][0])],
        'edges': [dict(payload['edges'][0])],
    }
    CombinedExtraction.model_validate(payload)
    assert payload == snapshot


def test_mixed_rows_preserve_valid_order() -> None:
    entities = [
        {'name': '  A  ', 'entity_type_id': 1, 'junk': True},
        'garbage',
        {'name': '', 'entity_type_id': 2},
        {'name': 'B', 'entity_type_id': 'x'},
    ]
    edges = [
        'garbage',
        {**VALID_EDGE, 'fact': 'First fact.', 'episode_indices': [4, False]},
        {**VALID_EDGE, 'fact': ''},
        {**VALID_EDGE, 'fact': 'Second fact.', 'episode_indices': None},
    ]
    result = CombinedExtraction.model_validate(_payload(entities, edges))
    assert [e.name for e in result.extracted_entities] == ['A', 'B']
    assert [e.entity_type_id for e in result.extracted_entities] == [1, -1]
    assert [(e.fact, e.episode_indices) for e in result.edges] == [
        ('First fact.', [4]),
        ('Second fact.', [0]),
    ]


def test_no_endpoint_synthesis_for_edges_referencing_unknown_entities() -> None:
    edge = {**VALID_EDGE, 'target_entity_name': 'Ghost Entity'}
    result = CombinedExtraction.model_validate(_payload([VALID_ENTITY], [edge]))
    assert [e.name for e in result.extracted_entities] == ['Acme Corp']
    assert result.edges[0].target_entity_name == 'Ghost Entity'


def test_no_menhir_import_or_reference_in_module_source() -> None:
    import graphiti_core.prompts.extract_nodes_and_edges as module

    source = Path(module.__file__).read_text(encoding='utf-8')
    assert 'Menhir' not in source
    assert 'menhir' not in source.lower()
    assert '_patch_graphiti' not in source
