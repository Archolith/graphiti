"""Validation of Graphiti.preserved_edge_attributes (Archolith/menhir#256)."""

import pytest

from graphiti_core.graphiti import Graphiti


def _graphiti() -> Graphiti:
    return object.__new__(Graphiti)


@pytest.mark.parametrize('value', [{'time_expiry': 1}, {'time_expiry': None, 'source': None}])
def test_a_mapping_is_rejected(value) -> None:
    with pytest.raises(TypeError):
        _graphiti().preserved_edge_attributes = value


@pytest.mark.parametrize(
    'value', [['fact'], ['time_expiry', 'invalid_at'], ('uuid',), ['reference_time']]
)
def test_core_edge_fields_are_rejected(value) -> None:
    with pytest.raises(ValueError, match='core edge fields'):
        _graphiti().preserved_edge_attributes = value


@pytest.mark.parametrize('value', [[' uuid'], ['fact '], ['time_expiry	']])
def test_names_with_surrounding_whitespace_are_rejected(value) -> None:
    # ' uuid' would otherwise slip past the core-field check.
    with pytest.raises(TypeError, match='whitespace'):
        _graphiti().preserved_edge_attributes = value


def test_assignment_after_construction_is_validated_and_normalized() -> None:
    graphiti = _graphiti()
    with pytest.raises(TypeError):
        graphiti.preserved_edge_attributes = 'time_expiry'
    assert graphiti.preserved_edge_attributes == frozenset()

    graphiti.preserved_edge_attributes = ['time_expiry', 'source', 'time_expiry']
    assert graphiti.preserved_edge_attributes == frozenset({'time_expiry', 'source'})

    graphiti.preserved_edge_attributes = None
    assert graphiti.preserved_edge_attributes == frozenset()


def test_menhir_names_are_accepted() -> None:
    # Menhir passes its anchored-time contract keys, the world-end stamp and the guard's source.
    graphiti = _graphiti()
    graphiti.preserved_edge_attributes = (
        'time_basis',
        'time_window_start',
        'time_expiry',
        'time_world_end',
        'source',
    )
    assert 'source' in graphiti.preserved_edge_attributes
