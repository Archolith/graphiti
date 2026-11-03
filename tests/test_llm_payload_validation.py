from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.utils.datetime_utils import utc_now
from graphiti_core.utils.maintenance import node_operations
from graphiti_core.utils.maintenance.combined_extraction import extract_nodes_and_edges


def _make_episode() -> EpisodicNode:
    return EpisodicNode(
        name='test_episode',
        group_id='group',
        source=EpisodeType.text,
        source_description='test',
        content='Acme Robotics announced the Atlas launch.',
        valid_at=utc_now(),
    )


def _make_clients():
    llm_client = MagicMock()
    llm_generate = AsyncMock()
    llm_client.generate_response = llm_generate
    clients = GraphitiClients.model_construct(
        driver=MagicMock(),
        embedder=MagicMock(),
        cross_encoder=MagicMock(),
        llm_client=llm_client,
    )
    return clients, llm_generate


@pytest.mark.asyncio
async def test_combined_extraction_non_object_payload_raises_validation_error():
    clients, llm_generate = _make_clients()
    llm_generate.return_value = [{'name': 'Acme Robotics', 'entity_type_id': 0}]

    with pytest.raises(ValidationError):
        await extract_nodes_and_edges(clients, _make_episode(), previous_episodes=[])


@pytest.mark.asyncio
async def test_extract_nodes_single_non_object_payload_raises_validation_error(monkeypatch):
    monkeypatch.setattr(
        node_operations,
        '_call_extraction_llm',
        AsyncMock(return_value=[{'name': 'Acme Robotics', 'entity_type_id': 0}]),
    )

    with pytest.raises(ValidationError):
        await node_operations._extract_nodes_single(MagicMock(), _make_episode(), {})


@pytest.mark.asyncio
async def test_extract_nodes_single_dict_payload_still_parses(monkeypatch):
    monkeypatch.setattr(
        node_operations,
        '_call_extraction_llm',
        AsyncMock(
            return_value={'extracted_entities': [{'name': 'Acme Robotics', 'entity_type_id': 0}]}
        ),
    )

    result = await node_operations._extract_nodes_single(MagicMock(), _make_episode(), {})

    assert [entity.name for entity in result] == ['Acme Robotics']
