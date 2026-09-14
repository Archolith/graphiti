"""Focused tests for fork-native provider response normalization.

Covers the native Pydantic before-validators that replace Menhir installers
#9 (`ExtractedEntity`) and #10 (`NodeResolutions`).
"""

from typing import Any

import pytest
from pydantic import ValidationError

from graphiti_core.prompts.dedupe_nodes import NodeResolutions
from graphiti_core.prompts.extract_nodes import ExtractedEntities, ExtractedEntity


def test_extracted_entity_canonical_passthrough() -> None:
    entity = ExtractedEntity.model_validate(
        {'name': 'Acme Corp', 'entity_type_id': 3, 'episode_indices': [1, 2]}
    )
    assert entity.name == 'Acme Corp'
    assert entity.entity_type_id == 3
    assert entity.episode_indices == [1, 2]


def test_extracted_entity_name_from_entity_name_alias() -> None:
    entity = ExtractedEntity.model_validate({'entity_name': 'Acme Corp', 'entity_type_id': 2})
    assert entity.name == 'Acme Corp'
    assert entity.entity_type_id == 2


def test_extracted_entity_name_from_entity_alias() -> None:
    entity = ExtractedEntity.model_validate({'entity': 'Acme Corp', 'type_id': 4})
    assert entity.name == 'Acme Corp'
    assert entity.entity_type_id == 4


@pytest.mark.parametrize('typo_key', ['name-', 'name_', 'Name ', ' name'])
def test_extracted_entity_name_typo_keys(typo_key: str) -> None:
    payload: dict[str, Any] = {typo_key: 'Widget', 'entity_type_id': 1}
    entity = ExtractedEntity.model_validate(payload)
    assert entity.name == 'Widget'
    assert entity.entity_type_id == 1


def test_extracted_entity_singleton_mapping() -> None:
    entity = ExtractedEntity.model_validate({'Acme Corp': 3})
    assert entity.name == 'Acme Corp'
    assert entity.entity_type_id == 3


def test_extracted_entity_type_id_alias() -> None:
    entity = ExtractedEntity.model_validate({'name': 'X', 'type_id': 7})
    assert entity.entity_type_id == 7


def test_extracted_entity_integer_type_alias() -> None:
    entity = ExtractedEntity.model_validate({'name': 'X', 'type': 5})
    assert entity.entity_type_id == 5


def test_extracted_entity_type_name_discarded_defaults_zero() -> None:
    entity = ExtractedEntity.model_validate({'name': 'X', 'type_name': 'Organization'})
    assert entity.entity_type_id == 0


def test_extracted_entity_entity_type_integer_accepted() -> None:
    entity = ExtractedEntity.model_validate({'name': 'X', 'entity_type': 6})
    assert entity.entity_type_id == 6


def test_extracted_entity_entity_type_non_integer_defaults_zero() -> None:
    entity = ExtractedEntity.model_validate({'name': 'X', 'entity_type': 'Organization'})
    assert entity.entity_type_id == 0


def test_extracted_entity_remaining_entity_field_integer_coerced() -> None:
    entity = ExtractedEntity.model_validate({'name': 'X', 'entity': '4'})
    assert entity.entity_type_id == 4


def test_extracted_entity_remaining_entity_field_non_integer_defaults_zero() -> None:
    entity = ExtractedEntity.model_validate({'name': 'X', 'entity': 'Organization'})
    assert entity.entity_type_id == 0


def test_extracted_entity_invalid_canonical_name_skips_alias_recovery() -> None:
    with pytest.raises(ValidationError):
        ExtractedEntity.model_validate({'name': None, 'entity_name': 'rescuer', 'entity_type_id': 1})


def test_extracted_entity_type_name_wins_over_entity_type() -> None:
    entity = ExtractedEntity.model_validate(
        {'name': 'X', 'type_name': 'Organization', 'entity_type': 6}
    )
    assert entity.entity_type_id == 0


def test_extracted_entity_missing_name_still_raises() -> None:
    with pytest.raises(ValidationError):
        ExtractedEntity.model_validate({'entity_type_id': 3, 'type_id': 4})


def test_extracted_entity_episode_indices_default_is_upstream_zero() -> None:
    entity = ExtractedEntity.model_validate({'name': 'X', 'entity_type_id': 1})
    assert entity.episode_indices == [0]


def test_extracted_entity_episode_indices_explicit_preserved() -> None:
    entity = ExtractedEntity.model_validate(
        {'name': 'X', 'entity_type_id': 1, 'episode_indices': [2, 4]}
    )
    assert entity.episode_indices == [2, 4]


def test_extracted_entity_episode_indices_no_shared_default() -> None:
    a = ExtractedEntity.model_validate({'name': 'A', 'entity_type_id': 1})
    b = ExtractedEntity.model_validate({'name': 'B', 'entity_type_id': 1})
    a.episode_indices.append(9)
    assert b.episode_indices == [0]


def test_extracted_entities_container_still_validates() -> None:
    container = ExtractedEntities.model_validate(
        {'extracted_entities': [{'name': 'X', 'entity_type_id': 1}, {'Entity B': 2}]}
    )
    assert [e.name for e in container.extracted_entities] == ['X', 'Entity B']


def test_node_resolutions_mixed_rows_normalized() -> None:
    payload: dict[str, Any] = {
        'entity_resolutions': [
            {'id': 1, 'name': 'A', 'duplicate_candidate_id': 0},
            'bare string',
            None,
            {'name': 'no id here'},
            {'id': '7'},
            {'id': 2, 'name': None, 'duplicate_candidate_id': None},
            {'id': 3, 'name': 'C', 'duplicate_candidate_id': '5'},
            {'id': 4, 'name': 'D', 'duplicate_candidate_id': 'not-a-number'},
            {'id': True, 'name': 'Bool id'},
            {'id': 5, 'name': 'E', 'duplicate_candidate_id': True},
        ]
    }
    result = NodeResolutions.model_validate(payload)
    assert [
        (r.id, r.name, r.duplicate_candidate_id) for r in result.entity_resolutions
    ] == [
        (1, 'A', 0),
        (7, '', -1),
        (2, '', -1),
        (3, 'C', 5),
        (4, 'D', -1),
        (5, 'E', -1),
    ]


def test_node_resolutions_missing_top_level_yields_empty() -> None:
    assert NodeResolutions.model_validate({}).entity_resolutions == []


def test_node_resolutions_null_top_level_yields_empty() -> None:
    assert NodeResolutions.model_validate({'entity_resolutions': None}).entity_resolutions == []


def test_node_resolutions_non_sequence_top_level_fails_safe_to_empty() -> None:
    payload: dict[str, Any] = {'entity_resolutions': {'id': 1, 'name': 'X'}}
    result = NodeResolutions.model_validate(payload)
    assert result.entity_resolutions == []


def test_node_resolutions_valid_order_and_payload_preserved() -> None:
    payload: dict[str, Any] = {
        'entity_resolutions': [
            {'id': 9, 'name': 'Z', 'duplicate_candidate_id': -1},
            {'id': 2, 'name': 'B', 'duplicate_candidate_id': 4},
        ]
    }
    result = NodeResolutions.model_validate(payload)
    assert [(r.id, r.name, r.duplicate_candidate_id) for r in result.entity_resolutions] == [
        (9, 'Z', -1),
        (2, 'B', 4),
    ]


def test_node_resolutions_no_shared_default() -> None:
    a = NodeResolutions.model_validate({})
    b = NodeResolutions.model_validate({})
    assert a.entity_resolutions == []
    assert b.entity_resolutions == []
    assert a.entity_resolutions is not b.entity_resolutions
