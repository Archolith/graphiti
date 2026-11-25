"""Fork seams are rejected, not dropped, when concurrent ingestion is enabled (menhir fork)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder import EmbedderClient
from graphiti_core.graphiti import Graphiti
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.ingestion import ConcurrentIngestionConfig
from graphiti_core.llm_client import LLMClient
from graphiti_core.tracer import NoOpTracer

GROUP = 'fork_guard_group'
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
ROUTED = object()


def _make_graphiti() -> Graphiti:
    driver = Mock()
    driver._database = GROUP
    graphiti = Graphiti.__new__(Graphiti)
    graphiti.driver = driver
    graphiti.tracer = NoOpTracer()
    graphiti.clients = GraphitiClients.model_construct(  # no validation, for test doubles
        driver=driver,
        llm_client=Mock(spec=LLMClient),
        embedder=Mock(spec=EmbedderClient),
        cross_encoder=Mock(spec=CrossEncoderClient),
        tracer=graphiti.tracer,
    )
    graphiti.concurrent_ingestion = ConcurrentIngestionConfig(window=4)
    ingestor = Mock()
    ingestor.add_episode = AsyncMock(return_value=ROUTED)
    ingestor.add_episode_bulk = AsyncMock(return_value=ROUTED)
    graphiti._concurrent_ingestor = ingestor
    for name in (
        'single_episode_extraction_hook',
        'identity_gate_hook',
        'candidate_filter_hook',
        'node_pre_resolution_hook',
        'edge_expiry_hook',
        'edge_invalidation_hook',
    ):
        setattr(graphiti, name, None)
    graphiti.preserved_edge_attributes = frozenset()
    return graphiti


_INSTANCE_SEAMS = {
    'single_episode_extraction_hook': object(),
    'identity_gate_hook': object(),
    'candidate_filter_hook': object(),
    'node_pre_resolution_hook': object(),
    'edge_expiry_hook': object(),
    'edge_invalidation_hook': object(),
    'preserved_edge_attributes': frozenset({'keep_me'}),
}


async def _add_episode(graphiti: Graphiti, **kwargs):
    return await graphiti.add_episode(
        name='episode',
        episode_body='content',
        source_description='test',
        reference_time=T0,
        group_id=GROUP,
        **kwargs,
    )


@pytest.mark.parametrize('seam', sorted(_INSTANCE_SEAMS))
async def test_instance_seams_are_rejected(seam):
    graphiti = _make_graphiti()
    setattr(graphiti, seam, _INSTANCE_SEAMS[seam])

    with pytest.raises(ValueError, match='not supported with concurrent ingestion'):
        await _add_episode(graphiti)
    with pytest.raises(ValueError, match='not supported with concurrent ingestion'):
        await graphiti.add_episode_bulk([], group_id=GROUP)

    graphiti._concurrent_ingestor.add_episode.assert_not_awaited()
    graphiti._concurrent_ingestor.add_episode_bulk.assert_not_awaited()


@pytest.mark.parametrize(
    'kwargs',
    [{'new_episode_uuid': 'reserved-uuid'}, {'publish_fence': Mock()}],
    ids=['new_episode_uuid', 'publish_fence'],
)
async def test_add_episode_call_seams_are_rejected(kwargs):
    graphiti = _make_graphiti()

    with pytest.raises(ValueError, match='not supported with concurrent ingestion'):
        await _add_episode(graphiti, **kwargs)

    graphiti._concurrent_ingestor.add_episode.assert_not_awaited()


@pytest.mark.parametrize('seam', sorted(_INSTANCE_SEAMS))
async def test_class_defaults_do_not_mask_an_instance_seam(seam):
    graphiti = _make_graphiti()
    for name in (*_INSTANCE_SEAMS, '_preserved_edge_attributes'):
        graphiti.__dict__.pop(name, None)  # fall back to the class defaults
    setattr(graphiti, seam, _INSTANCE_SEAMS[seam])

    with pytest.raises(ValueError, match='not supported with concurrent ingestion'):
        await _add_episode(graphiti)


async def test_no_seam_routes_to_the_concurrent_ingestor():
    graphiti = _make_graphiti()

    assert await _add_episode(graphiti) is ROUTED
    assert await graphiti.add_episode_bulk([], group_id=GROUP) is ROUTED

    graphiti._concurrent_ingestor.add_episode.assert_awaited_once()
    graphiti._concurrent_ingestor.add_episode_bulk.assert_awaited_once()


@pytest.mark.parametrize('seam', sorted(_INSTANCE_SEAMS))
async def test_public_concurrent_ingestor_rejects_instance_seams(seam):
    from graphiti_core.ingestion import ConcurrentIngestor

    graphiti = _make_graphiti()
    setattr(graphiti, seam, _INSTANCE_SEAMS[seam])
    ingestor = ConcurrentIngestor(graphiti, ConcurrentIngestionConfig(window=4))
    common = dict(
        group_id=GROUP,
        entity_types=None,
        excluded_entity_types=None,
        edge_types=None,
        edge_type_map=None,
        custom_extraction_instructions=None,
        saga=None,
    )

    with pytest.raises(ValueError, match='not supported with concurrent ingestion'):
        await ingestor.add_episode(
            name='episode',
            episode_body='content',
            source_description='test',
            reference_time=T0,
            source=Mock(),
            uuid=None,
            previous_episode_uuids=None,
            saga_previous_episode_uuid=None,
            **common,
        )
    with pytest.raises(ValueError, match='not supported with concurrent ingestion'):
        await ingestor.add_episode_bulk(
            bulk_episodes=[], strict_edge_types=False, use_combined_extraction=True, **common
        )
