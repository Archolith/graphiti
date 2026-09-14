"""Fork-native None-hardening regression tests.

Replaces Menhir installers #5 (`_patch_graphiti_none_replace`), #7
(`_patch_graphiti_node_summary_none`), and #8 (`_patch_graphiti_edge_none_fields`)
with native Pydantic validators and direct method hardening.

Degenerate (explicit-None) payloads are fed through ``model_validate`` with
``dict[str, Any]`` inputs so the intentionally invalid values stay type-correct.
"""

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from graphiti_core.edges import EntityEdge
from graphiti_core.embedder import EmbedderClient
from graphiti_core.nodes import CommunityNode, EntityNode

_NOW = datetime.now(timezone.utc)


class _StubEmbedder(EmbedderClient):
    def __init__(self) -> None:
        # Captured inputs are always checked to be list[str] before appending;
        # typed object so the honest capture type matches the abstract signature.
        self.received: list[object] = []

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        assert isinstance(input_data, list) and all(isinstance(s, str) for s in input_data)
        self.received.append(input_data)
        return [0.1, 0.2, 0.3]


def _valid_edge_payload() -> dict[str, Any]:
    return {
        'group_id': 'g',
        'name': 'WORKS_ON',
        'fact': 'Alice works on menhir',
        'source_node_uuid': 'src',
        'target_node_uuid': 'dst',
        'created_at': _NOW,
    }


def _make_edge(**overrides: Any) -> EntityEdge:
    payload = _valid_edge_payload()
    payload.update(overrides)
    return EntityEdge(**payload)


# --- EntityNode.summary (#7) ---


def test_entity_node_explicit_none_summary_becomes_empty_string():
    node = EntityNode.model_validate({'name': 'x', 'group_id': 'g', 'summary': None})
    assert node.summary == ''


def test_entity_node_missing_summary_uses_default():
    node = EntityNode(name='x', group_id='g')
    assert node.summary == ''


def test_entity_node_real_summary_preserved():
    node = EntityNode(name='x', group_id='g', summary='a real summary')
    assert node.summary == 'a real summary'


# --- EntityEdge None coercion (#8) ---


def test_degenerate_edge_constructs_with_none_fields():
    payload: dict[str, Any] = {
        'uuid': None,
        'group_id': None,
        'name': None,
        'fact': None,
        'source_node_uuid': None,
        'target_node_uuid': None,
        'episodes': None,
        'created_at': _NOW,
    }
    edge = EntityEdge.model_validate(payload)
    assert edge.group_id == ''
    assert edge.name == ''
    assert edge.fact == ''
    assert edge.source_node_uuid == ''
    assert edge.target_node_uuid == ''
    # uuid default factory runs: fresh non-empty string, never '' or None.
    assert isinstance(edge.uuid, str) and edge.uuid
    # episodes default factory runs: fresh empty list, never None.
    assert edge.episodes == []


def test_edge_generated_uuids_are_distinct():
    a = EntityEdge.model_validate({**_valid_edge_payload(), 'uuid': None})
    b = EntityEdge.model_validate({**_valid_edge_payload(), 'uuid': None})
    assert a.uuid and b.uuid and a.uuid != b.uuid


def test_edge_episode_defaults_are_not_shared():
    a = _make_edge()
    b = _make_edge()
    assert a.episodes == [] and b.episodes == []
    a.episodes.append('ep-1')
    assert b.episodes == []
    assert EntityEdge.model_fields['episodes'].default_factory is not None


def test_edge_normal_values_preserved():
    edge = _make_edge(uuid='fixed-uuid-123', episodes=['ep-9'])
    assert edge.uuid == 'fixed-uuid-123'
    assert edge.name == 'WORKS_ON'
    assert edge.fact == 'Alice works on menhir'
    assert edge.group_id == 'g'
    assert edge.source_node_uuid == 'src'
    assert edge.target_node_uuid == 'dst'
    assert edge.episodes == ['ep-9']


def test_edge_partial_none_coercion():
    payload = _valid_edge_payload()
    payload['name'] = None
    payload['fact'] = None
    edge = EntityEdge.model_validate(payload)
    assert edge.name == ''
    assert edge.fact == ''
    assert edge.group_id == 'g'


def test_edge_missing_required_field_still_raises():
    # None is tolerated, but an actually omitted required str field stays required.
    payload = _valid_edge_payload()
    del payload['group_id']
    with pytest.raises(ValidationError):
        EntityEdge.model_validate(payload)


# --- Embedding safety (#5) ---


@pytest.mark.asyncio
async def test_entity_edge_embedding_coerces_none_fact():
    embedder = _StubEmbedder()
    edge = _make_edge(fact='initial')
    object.__setattr__(edge, 'fact', None)  # simulate the LLM returning null
    await edge.generate_embedding(embedder)

    assert edge.fact == ''
    assert embedder.received == [['']]


@pytest.mark.asyncio
async def test_entity_node_embedding_coerces_none_name():
    embedder = _StubEmbedder()
    node = EntityNode(name='initial', group_id='g')
    object.__setattr__(node, 'name', None)
    await node.generate_name_embedding(embedder)

    assert node.name == ''
    assert embedder.received == [['']]


@pytest.mark.asyncio
async def test_community_node_embedding_coerces_none_name():
    embedder = _StubEmbedder()
    node = CommunityNode(name='initial', group_id='g')
    object.__setattr__(node, 'name', None)
    await node.generate_name_embedding(embedder)

    assert node.name == ''
    assert embedder.received == [['']]


@pytest.mark.asyncio
async def test_embedding_normal_values_unchanged():
    embedder = _StubEmbedder()
    edge = _make_edge(fact='multi\nline fact')
    await edge.generate_embedding(embedder)
    assert edge.fact == 'multi\nline fact'
    assert embedder.received == [['multi line fact']]

    node = EntityNode(name='Alice', group_id='g')
    await node.generate_name_embedding(embedder)
    assert node.name == 'Alice'
    assert embedder.received == [['multi line fact'], ['Alice']]
