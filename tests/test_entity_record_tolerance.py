"""Fork-native entity-record tolerance tests.

Replaces the fork half of Menhir installer #6
(`_patch_graphiti_entity_record_group_id`): defensive copying of entity
records, null group_id repair via a neutral process-level resolver hook,
legacy `Z[UTC]` created_at suffix repair, and bounded log-once diagnostics.

Menhir namespace policy is deliberately NOT in the fork; the resolver hook is
neutral and unregistered by default.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

import graphiti_core.search.search_utils as search_utils
from graphiti_core import nodes as nodes_mod
from graphiti_core.driver.driver import GraphProvider
from graphiti_core.helpers import get_default_group_id
from graphiti_core.nodes import (
    get_entity_node_from_record,
    set_entity_record_group_id_resolver,
)


@pytest.fixture(autouse=True)
def _reset_global_state():
    set_entity_record_group_id_resolver(None)
    nodes_mod._logged_null_group_keys.clear()
    nodes_mod._logged_repaired_timestamp_keys.clear()
    yield
    set_entity_record_group_id_resolver(None)
    nodes_mod._logged_null_group_keys.clear()
    nodes_mod._logged_repaired_timestamp_keys.clear()


def _valid_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        'uuid': 'entity-uuid-1',
        'name': 'Entity One',
        'group_id': 'group-a',
        'name_embedding': [0.1, 0.2],
        'created_at': datetime(2026, 1, 1, tzinfo=timezone.utc),
        'summary': 'a summary',
        'attributes': {'uuid': 'entity-uuid-1', 'custom': 'value'},
        'labels': ['Entity'],
    }
    record.update(overrides)
    return record


def _resolve(record: dict[str, Any], provider: GraphProvider) -> Any:
    # Needed because non-KUZU paths require a dict attributes value.
    if provider != GraphProvider.KUZU:
        record.setdefault('attributes', {})
    return get_entity_node_from_record(record, provider)


# --- valid records ---


def test_valid_neo4j_record_produces_same_node():
    record = _valid_record()
    node = _resolve(record, GraphProvider.NEO4J)

    assert node.uuid == 'entity-uuid-1'
    assert node.name == 'Entity One'
    assert node.group_id == 'group-a'
    assert node.summary == 'a summary'
    assert node.labels == ['Entity']
    assert node.attributes == {'custom': 'value'}
    assert node.created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


# --- defensive copying ---


def test_caller_record_attributes_and_labels_not_mutated():
    attributes = {'uuid': 'entity-uuid-1', 'custom': 'value'}
    labels = ['Entity', 'Entity_groupa']
    record = _valid_record(attributes=attributes, labels=labels)

    node = _resolve(record, GraphProvider.NEO4J)

    assert record['attributes'] == {'uuid': 'entity-uuid-1', 'custom': 'value'}
    assert attributes == {'uuid': 'entity-uuid-1', 'custom': 'value'}
    assert labels == ['Entity', 'Entity_groupa']
    # The internal Entity_ prefix label is stripped on the node copy only.
    assert node.labels == ['Entity']


def test_labels_tuple_not_mutated_and_accepted():
    labels = ('Entity',)
    record = _valid_record(labels=labels)
    node = _resolve(record, GraphProvider.NEO4J)
    assert labels == ('Entity',)
    assert node.labels == ['Entity']


def test_missing_labels_default_to_empty_list():
    record = _valid_record()
    del record['labels']
    node = _resolve(record, GraphProvider.NEO4J)
    assert node.labels == []


# --- null group id without resolver ---


def test_null_group_without_resolver_uses_provider_defaults():
    record_neo4j = _valid_record(group_id=None)
    node = _resolve(record_neo4j, GraphProvider.NEO4J)
    assert node.group_id == get_default_group_id(GraphProvider.NEO4J)
    assert node.group_id == ''

    record_falkor = _valid_record(group_id=None)
    node = _resolve(record_falkor, GraphProvider.FALKORDB)
    assert node.group_id == get_default_group_id(GraphProvider.FALKORDB)
    assert node.group_id == '_'


# --- resolver hook ---


def test_resolver_receives_defensive_copies_and_infers_group():
    received: list[tuple[Any, GraphProvider]] = []

    def resolver(record: Any, provider: GraphProvider) -> str | None:
        received.append((record, provider))
        # The resolver's authority is return-only: top-level assignment must
        # fail on the read-only mapping.
        with pytest.raises(TypeError):
            record['name'] = 'mutated'
        # Nested mutation (if attempted) may only touch the isolated
        # snapshot, never caller input or the returned node.
        record['attributes']['sneaky'] = 'x'
        record['labels'].append('sneaky')
        return 'inferred-group'

    set_entity_record_group_id_resolver(resolver)

    record = _valid_record(group_id=None)
    node = _resolve(record, GraphProvider.NEO4J)

    assert node.group_id == 'inferred-group'
    assert node.name == 'Entity One'
    assert node.attributes == {'custom': 'value'}
    assert node.labels == ['Entity']
    assert len(received) == 1
    assert received[0][1] == GraphProvider.NEO4J
    assert record['name'] == 'Entity One'
    assert record['attributes'] == {'uuid': 'entity-uuid-1', 'custom': 'value'}
    assert record['labels'] == ['Entity']
    # The resolver's view still carries the copied nested structures.
    assert isinstance(received[0][0]['attributes'], dict)
    assert isinstance(received[0][0]['labels'], list)
    # The resolver saw its own isolated snapshot copies. Graphiti strips
    # reserved keys from the working attributes copy before the resolver is
    # invoked, so the snapshot carries only the post-strip attributes.
    assert received[0][0]['attributes'] == {'custom': 'value', 'sneaky': 'x'}
    assert received[0][0]['labels'] == ['Entity', 'sneaky']


def test_resolver_called_only_for_null_group():
    calls: list[int] = []

    def resolver(record: Any, provider: GraphProvider) -> str | None:
        calls.append(1)
        return 'resolved'

    set_entity_record_group_id_resolver(resolver)

    record = _valid_record()  # group_id is non-None
    node = _resolve(record, GraphProvider.NEO4J)
    assert node.group_id == 'group-a'
    assert calls == []

    record = _valid_record(group_id=None)
    node = _resolve(record, GraphProvider.NEO4J)
    assert node.group_id == 'resolved'
    assert len(calls) == 1


def test_resolver_reset_restores_default_behavior():
    set_entity_record_group_id_resolver(lambda record, provider: 'inferred-group')
    record = _valid_record(group_id=None)
    node = _resolve(record, GraphProvider.NEO4J)
    assert node.group_id == 'inferred-group'

    set_entity_record_group_id_resolver(None)
    record = _valid_record(group_id=None)
    node = _resolve(record, GraphProvider.NEO4J)
    assert node.group_id == ''


def test_resolver_none_return_falls_back_to_provider_default():
    set_entity_record_group_id_resolver(lambda record, provider: None)
    record = _valid_record(group_id=None)
    node = _resolve(record, GraphProvider.FALKORDB)
    assert node.group_id == '_'


def test_resolver_empty_string_return_is_authoritative():
    set_entity_record_group_id_resolver(lambda record, provider: '')
    record = _valid_record(group_id=None)
    node = _resolve(record, GraphProvider.FALKORDB)
    assert node.group_id == ''


def test_resolver_non_string_return_raises_type_error():
    set_entity_record_group_id_resolver(lambda record, provider: 42)  # type: ignore[arg-type,return-value]
    record = _valid_record(group_id=None)
    with pytest.raises(TypeError):
        _resolve(record, GraphProvider.NEO4J)


def test_resolver_exception_propagates():
    def bad_resolver(record: Any, provider: GraphProvider) -> str | None:
        raise RuntimeError('policy bug')

    set_entity_record_group_id_resolver(bad_resolver)
    record = _valid_record(group_id=None)
    with pytest.raises(RuntimeError, match='policy bug'):
        _resolve(record, GraphProvider.NEO4J)


def test_set_entity_record_group_id_resolver_rejects_non_callable():
    with pytest.raises(TypeError):
        set_entity_record_group_id_resolver('not-a-resolver')  # type: ignore[arg-type]


# --- Z[UTC] timestamp repair ---


def test_exact_zutc_suffix_repaired_to_aware_datetime():
    record = _valid_record(created_at='2026-01-01T00:00:00Z[UTC]')
    node = _resolve(record, GraphProvider.NEO4J)
    assert node.created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert node.created_at.tzinfo is not None


def test_other_timestamps_retain_existing_handling():
    # datetime objects pass through
    record = _valid_record(created_at=datetime(2025, 6, 1, tzinfo=timezone.utc))
    node = _resolve(record, GraphProvider.NEO4J)
    assert node.created_at == datetime(2025, 6, 1, tzinfo=timezone.utc)

    # non-Z[UTC] strings parse via fromisoformat
    record = _valid_record(created_at='2025-06-01T00:00:00+00:00')
    node = _resolve(record, GraphProvider.NEO4J)
    assert node.created_at == datetime(2025, 6, 1, tzinfo=timezone.utc)

    # a plain terminal Z is NOT repaired but continues to parse successfully
    # (datetime.fromisoformat supports it) as an aware UTC datetime, with no
    # repaired-timestamp diagnostic
    record = _valid_record(created_at='2025-06-01T00:00:00Z')
    node = _resolve(record, GraphProvider.NEO4J)
    assert node.created_at == datetime(2025, 6, 1, tzinfo=timezone.utc)
    assert node.created_at.tzinfo is not None
    assert 'entity-uuid-1' not in nodes_mod._logged_repaired_timestamp_keys


# --- diagnostics ---


def test_diagnostics_log_identity_not_attributes(caplog):
    with caplog.at_level(logging.WARNING, logger='graphiti_core.nodes'):
        record = _valid_record(group_id=None, created_at='2026-01-01T00:00:00Z[UTC]')
        _resolve(record, GraphProvider.NEO4J)

    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'entity-uuid-1' in text
    assert 'custom' not in text
    assert 'value' not in text
    assert 'Z[UTC]' in text


def test_diagnostics_dedupe_repeat_keys(caplog):
    with caplog.at_level(logging.WARNING, logger='graphiti_core.nodes'):
        for _ in range(3):
            _resolve(_valid_record(group_id=None), GraphProvider.NEO4J)

    null_logs = [r for r in caplog.records if 'null group_id' in r.getMessage()]
    assert len(null_logs) == 1


def test_diagnostics_bounded_with_eviction(caplog):
    # 600 unique keys legitimately produce 600 first-seen logs while storage
    # stays bounded at exactly 512 via eviction of an arbitrary key
    # (set.pop(), matching current Menhir semantics).
    with caplog.at_level(logging.WARNING, logger='graphiti_core.nodes'):
        for i in range(600):
            record = _valid_record(group_id=None, uuid=f'u-{i}', name=f'n-{i}')
            _resolve(record, GraphProvider.NEO4J)

    assert len(nodes_mod._logged_null_group_keys) == 512
    null_logs = [r for r in caplog.records if 'null group_id' in r.getMessage()]
    assert len(null_logs) == 600

    # set.pop() eviction is arbitrary (matching current Menhir semantics), so
    # derive the retained/evicted probe keys from actual state instead of
    # assuming which key was evicted.
    universe = {f'u-{i}' for i in range(600)}
    retained_key = next(iter(nodes_mod._logged_null_group_keys))
    assert retained_key in nodes_mod._logged_null_group_keys
    evicted_keys = universe - nodes_mod._logged_null_group_keys
    assert evicted_keys
    evicted_key = min(evicted_keys)

    with caplog.at_level(logging.WARNING, logger='graphiti_core.nodes'):
        # Repeating a retained key produces no new log...
        caplog.clear()
        _resolve(
            _valid_record(group_id=None, uuid=retained_key, name='n-retained'), GraphProvider.NEO4J
        )
        retained_repeat_logs = [r for r in caplog.records if 'null group_id' in r.getMessage()]
        assert retained_repeat_logs == []
        # ...while repeating an evicted key is first-seen again and logs once.
        caplog.clear()
        _resolve(
            _valid_record(group_id=None, uuid=evicted_key, name='n-evicted'), GraphProvider.NEO4J
        )
        repeat_logs = [r for r in caplog.records if 'null group_id' in r.getMessage()]
        assert len(repeat_logs) == 1
        assert evicted_key in repeat_logs[0].getMessage()
    assert len(nodes_mod._logged_null_group_keys) == 512


# --- search_utils native behavior (no symbol rebinding) ---


def test_search_utils_call_sites_get_native_behavior():
    # search_utils imports the function object directly; the native behavior
    # must be visible through that same object without any rebinding.
    assert search_utils.get_entity_node_from_record is get_entity_node_from_record

    calls: list[int] = []

    def resolver(record: Any, provider: GraphProvider) -> str | None:
        calls.append(1)
        return 'via-search-utils'

    set_entity_record_group_id_resolver(resolver)
    record = _valid_record(group_id=None)
    record.setdefault('attributes', {})
    node = search_utils.get_entity_node_from_record(record, GraphProvider.NEO4J)
    assert node.group_id == 'via-search-utils'
    assert len(calls) == 1


# --- KUZU ---


def test_kuzu_string_attributes_supported():
    import json

    record = _valid_record(
        attributes=json.dumps({'custom': 'kuzu-value'}),
        labels=['Entity'],
    )
    node = _resolve(record, GraphProvider.KUZU)
    assert node.attributes == {'custom': 'kuzu-value'}
    assert record['attributes'] == json.dumps({'custom': 'kuzu-value'})


# --- fork hygiene ---


def test_no_menhir_reference_in_fork_source():
    import inspect

    source = inspect.getsource(nodes_mod)
    assert 'menhir' not in source.lower()
