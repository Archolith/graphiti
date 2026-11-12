"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Tests for the publish fence: a hook that runs inside the single publish transaction.

Unit tests drive ``add_nodes_and_edges_bulk_tx`` with a fake transaction. Integration tests
(``@pytest.mark.integration``) drive the public ``add_episode`` against a real Neo4j with the
extraction, resolution and embedding clients stubbed; no LLM is ever called.
"""

import os
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from neo4j.exceptions import Neo4jError, TransientError

import graphiti_core.graphiti as graphiti_module
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.driver import GraphProvider
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.graphiti import Graphiti
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.publish_fence import PublishFence, publish_fence_kwargs
from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk_tx

NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Unit tests (no database)
# ---------------------------------------------------------------------------


class _FakeTx:
    """Records every statement run against it, in order."""

    def __init__(self, log: list[str]):
        self.log = log

    async def run(self, query: str, **kwargs: Any) -> None:
        self.log.append('save')


class _RecordingFence:
    def __init__(self, log: list[str], fail_before: bool = False, fail_after: bool = False):
        self.log = log
        self.fail_before = fail_before
        self.fail_after = fail_after
        self.before_calls: list[dict[str, Any]] = []
        self.after_calls: list[dict[str, Any]] = []

    async def before_write(self, tx, *, episode_uuid, previous_episode_uuids):
        self.log.append('before')
        self.before_calls.append(
            {'tx': tx, 'episode_uuid': episode_uuid, 'previous': list(previous_episode_uuids)}
        )
        if self.fail_before:
            raise RuntimeError('before failed')

    async def after_write(
        self,
        tx,
        *,
        episode_uuid,
        entity_node_uuids,
        entity_edge_uuids,
        previous_episode_uuids,
    ):
        self.log.append('after')
        self.after_calls.append(
            {
                'tx': tx,
                'episode_uuid': episode_uuid,
                'nodes': list(entity_node_uuids),
                'edges': list(entity_edge_uuids),
                'previous': list(previous_episode_uuids),
            }
        )
        if self.fail_after:
            raise RuntimeError('after failed')


def _episode(group: str = 'g', name: str = 'ep', valid_at: datetime = NOW) -> EpisodicNode:
    return EpisodicNode(
        name=name,
        group_id=group,
        labels=[],
        source=EpisodeType.message,
        source_description='test',
        content='Alice likes Bob',
        created_at=NOW,
        valid_at=valid_at,
        entity_edges=[],
    )


def _node(name: str, group: str = 'g') -> EntityNode:
    return EntityNode(name=name, group_id=group, labels=['Entity'], created_at=NOW, summary='')


def _edge(source: EntityNode, target: EntityNode, group: str = 'g') -> EntityEdge:
    return EntityEdge(
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        group_id=group,
        name='LIKES',
        fact='Alice likes Bob',
        episodes=[],
        created_at=NOW,
    )


def _unit_embedder(log: list[str]) -> Mock:
    embedder = Mock(spec=EmbedderClient)

    async def create(input_data):
        log.append('embed')
        return [0.1, 0.2]

    embedder.create = create
    return embedder


async def _run_tx(
    log: list[str],
    fence: PublishFence | None,
    provider: GraphProvider = GraphProvider.NEO4J,
    interface: Any = None,
    **extra: Any,
):
    driver = Mock()
    driver.provider = provider
    driver.graph_operations_interface = interface
    alice, bob = _node('Alice'), _node('Bob')
    edge = _edge(alice, bob)
    episode = _episode()
    kwargs: dict[str, Any] = {}
    if fence is not None:
        kwargs = {
            'publish_fence': fence,
            'fence_episode_uuid': episode.uuid,
            'previous_episode_uuids': ['p1', 'p2'],
        }
    kwargs.update(extra)
    await add_nodes_and_edges_bulk_tx(
        _FakeTx(log),  # type: ignore[arg-type]
        [episode],
        [],
        [alice, bob],
        [edge],
        _unit_embedder(log),
        driver=driver,
        **kwargs,
    )
    return episode, alice, bob, edge


@pytest.mark.parametrize('provider', [GraphProvider.NEO4J, GraphProvider.KUZU])
async def test_hooks_bracket_the_saves_after_embeddings(provider):
    log: list[str] = []
    fence = _RecordingFence(log)
    episode, alice, bob, edge = await _run_tx(log, fence, provider)

    assert log.count('embed') == 3
    first_save = log.index('save')
    assert log.index('before') == first_save - 1
    assert max(i for i, entry in enumerate(log) if entry == 'embed') < log.index('before')
    assert log[-1] == 'after'
    assert log.count('before') == 1 and log.count('after') == 1
    last_save = max(i for i, entry in enumerate(log) if entry == 'save')
    assert log.index('after') == last_save + 1

    assert fence.before_calls[0]['episode_uuid'] == episode.uuid
    assert fence.before_calls[0]['previous'] == ['p1', 'p2']
    assert fence.after_calls[0]['nodes'] == [alice.uuid, bob.uuid]
    assert fence.after_calls[0]['edges'] == [edge.uuid]
    assert fence.after_calls[0]['previous'] == ['p1', 'p2']
    assert fence.before_calls[0]['tx'] is fence.after_calls[0]['tx']


async def test_hooks_bracket_the_saves_on_graph_operations_interface():
    log: list[str] = []
    interface = Mock()
    for method in (
        'episodic_node_save_bulk',
        'node_save_bulk',
        'episodic_edge_save_bulk',
        'edge_save_bulk',
    ):

        async def save(*args, **kwargs):
            log.append('save')

        setattr(interface, method, save)

    fence = _RecordingFence(log)
    await _run_tx(log, fence, interface=interface)

    assert log.count('save') == 4
    assert log.index('before') == log.index('save') - 1
    assert log[-1] == 'after'
    assert max(i for i, entry in enumerate(log) if entry == 'embed') < log.index('before')


async def test_before_write_raise_propagates_and_nothing_is_saved():
    log: list[str] = []
    with pytest.raises(RuntimeError, match='before failed'):
        await _run_tx(log, _RecordingFence(log, fail_before=True))
    assert 'save' not in log
    assert 'after' not in log


async def test_after_write_raise_propagates():
    log: list[str] = []
    with pytest.raises(RuntimeError, match='after failed'):
        await _run_tx(log, _RecordingFence(log, fail_after=True))
    assert log[-1] == 'after'


async def test_none_fence_makes_no_hook_calls():
    log: list[str] = []
    await _run_tx(log, None)
    assert 'before' not in log and 'after' not in log
    assert log.count('save') == 4


def test_publish_fence_kwargs_is_empty_without_a_fence():
    assert publish_fence_kwargs(None, 'e', ['p']) == {}


def test_recording_fence_satisfies_protocol():
    assert isinstance(_RecordingFence([]), PublishFence)


async def test_uuid_and_new_episode_uuid_are_mutually_exclusive():
    graphiti = Graphiti.__new__(Graphiti)
    with pytest.raises(ValueError, match='new_episode_uuid'):
        await graphiti.add_episode(
            name='n',
            episode_body='b',
            source_description='s',
            reference_time=NOW,
            uuid='existing',
            new_episode_uuid='new',
        )


@pytest.mark.parametrize('extra', [{'saga': 'a-saga'}, {'update_communities': True}])
async def test_publish_fence_rejects_writes_outside_the_transaction(extra):
    graphiti = Graphiti.__new__(Graphiti)
    with pytest.raises(ValueError, match='publish_fence'):
        await graphiti.add_episode(
            name='n',
            episode_body='b',
            source_description='s',
            reference_time=NOW,
            publish_fence=_RecordingFence([]),
            **extra,
        )


# ---------------------------------------------------------------------------
# Integration tests (real Neo4j; stubbed LLM, embedder and resolution)
# ---------------------------------------------------------------------------

NEO4J_URI = os.getenv('NEO4J_URI', 'bolt://localhost:7687')
NEO4J_USER = os.getenv('NEO4J_USER', 'neo4j')
NEO4J_PASSWORD = os.getenv('NEO4J_PASSWORD', 'test')


class _Env:
    def __init__(self, graphiti: Graphiti, group: str):
        self.graphiti = graphiti
        self.driver = graphiti.driver
        self.group = group
        self.retrieved: list[list[str]] = []

    async def count(self, cypher: str, **params: Any) -> int:
        records, _, _ = await self.driver.execute_query(cypher, g=self.group, **params)
        return records[0]['c']

    async def episodes_named(self, episode_uuid: str) -> int:
        return await self.count(
            'MATCH (e:Episodic {uuid: $u}) RETURN count(e) AS c', u=episode_uuid
        )

    async def entities(self) -> int:
        return await self.count('MATCH (n:Entity {group_id: $g}) RETURN count(n) AS c')

    async def entity_edges(self) -> int:
        return await self.count('MATCH ()-[r:RELATES_TO {group_id: $g}]->() RETURN count(r) AS c')

    async def mentions(self) -> int:
        return await self.count('MATCH ()-[r:MENTIONS {group_id: $g}]->() RETURN count(r) AS c')

    async def probes(self) -> int:
        return await self.count('MATCH (m:FenceProbe {g: $g}) RETURN count(m) AS c')

    async def seed_episode(self, name: str, valid_at: datetime) -> EpisodicNode:
        episode = _episode(self.group, name, valid_at)
        await episode.save(self.driver)
        return episode

    async def add(self, **kwargs: Any):
        return await self.graphiti.add_episode(
            name='fenced',
            episode_body='Alice likes Bob',
            source_description='test',
            reference_time=NOW,
            group_id=self.group,
            **kwargs,
        )

    async def assert_nothing_persisted(self, episode_uuid: str) -> None:
        assert await self.episodes_named(episode_uuid) == 0
        assert await self.entities() == 0
        assert await self.entity_edges() == 0
        assert await self.mentions() == 0
        assert await self.probes() == 0


@pytest.fixture
async def env(monkeypatch):
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    driver = Neo4jDriver(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)
    try:
        await driver.health_check()
    except Exception as exc:  # pragma: no cover - environment dependent
        await driver.close()
        pytest.skip(f'Neo4j not reachable: {exc}')

    group = f'fence_{uuid_lib.uuid4().hex[:12]}'
    embedder = Mock(spec=EmbedderClient)
    embedder.create = AsyncMock(return_value=[0.1, 0.2, 0.3])
    graphiti = Graphiti(
        graph_driver=driver,
        llm_client=Mock(spec=LLMClient),
        embedder=embedder,
        cross_encoder=Mock(spec=CrossEncoderClient),
    )
    environment = _Env(graphiti, group)

    # The test DB has one database; scope by group_id only, never by cloning a database.
    monkeypatch.setattr(
        graphiti,
        '_resolve_request_scope',
        lambda group_id: (group_id, graphiti.driver, graphiti.clients),
    )

    async def fake_combined(clients, ep, previous_episodes, **kwargs):
        environment.retrieved.append([p.uuid for p in previous_episodes])
        alice, bob = _node('Alice', group), _node('Bob', group)
        return [alice, bob], [_edge(alice, bob, group)], {alice.uuid: [0], bob.uuid: [0]}

    async def fake_resolve_nodes(clients, extracted_nodes, ep, previous_episodes, entity_types):
        return extracted_nodes, {}, []

    async def fake_resolve_edges(clients, edges, ep, nodes, edge_types, edge_type_map):
        return edges, [], list(edges)

    async def fake_attributes(clients, nodes, ep, previous_episodes, entity_types, edges):
        return nodes

    monkeypatch.setattr(graphiti_module, 'extract_nodes_and_edges', fake_combined)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_nodes', fake_resolve_nodes)
    monkeypatch.setattr(graphiti_module, 'resolve_edge_pointers', lambda edges, uuid_map: edges)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edges', fake_resolve_edges)
    monkeypatch.setattr(graphiti_module, 'extract_attributes_from_nodes', fake_attributes)

    try:
        yield environment
    finally:
        await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)
        await driver.execute_query('MATCH (m:FenceProbe {g: $g}) DETACH DELETE m', g=group)
        await driver.close()


class _ProbeFence:
    """Writes a FenceProbe in the publish transaction; optionally raises."""

    def __init__(
        self,
        group: str,
        fail_before: Exception | None = None,
        fail_after: Exception | None = None,
        fail_first_only: bool = False,
    ):
        self.group = group
        self.fail_before = fail_before
        self.fail_after = fail_after
        self.fail_first_only = fail_first_only
        self.before_runs = 0
        self.after_runs = 0
        self.previous: list[str] = []
        self.node_uuids: list[str] = []
        self.edge_uuids: list[str] = []

    async def before_write(self, tx, *, episode_uuid, previous_episode_uuids):
        self.before_runs += 1
        self.previous = list(previous_episode_uuids)
        if self.fail_before is not None and (not self.fail_first_only or self.before_runs == 1):
            raise self.fail_before

    async def after_write(
        self,
        tx,
        *,
        episode_uuid,
        entity_node_uuids,
        entity_edge_uuids,
        previous_episode_uuids,
    ):
        self.after_runs += 1
        self.node_uuids = list(entity_node_uuids)
        self.edge_uuids = list(entity_edge_uuids)
        await tx.run('MERGE (m:FenceProbe {ep: $e, g: $g})', e=episode_uuid, g=self.group)
        if self.fail_after is not None and (not self.fail_first_only or self.after_runs == 1):
            raise self.fail_after


@pytest.mark.integration
async def test_new_episode_uuid_is_the_persisted_episode_uuid_int(env):
    wanted = str(uuid_lib.uuid4())
    result = await env.add(new_episode_uuid=wanted)

    assert result.episode.uuid == wanted
    assert await env.episodes_named(wanted) == 1
    assert await env.entities() == 2
    assert await env.entity_edges() == 1


@pytest.mark.integration
async def test_before_write_raise_persists_nothing_int(env):
    wanted = str(uuid_lib.uuid4())
    fence = _ProbeFence(env.group, fail_before=RuntimeError('veto'))
    with pytest.raises(RuntimeError, match='veto'):
        await env.add(new_episode_uuid=wanted, publish_fence=fence)

    assert fence.before_runs == 1 and fence.after_runs == 0
    await env.assert_nothing_persisted(wanted)


@pytest.mark.integration
async def test_after_write_raise_persists_nothing_int(env):
    wanted = str(uuid_lib.uuid4())
    fence = _ProbeFence(env.group, fail_after=RuntimeError('late veto'))
    with pytest.raises(RuntimeError, match='late veto'):
        await env.add(new_episode_uuid=wanted, publish_fence=fence)

    # The probe was written inside the transaction before the raise, and rolled back with it.
    assert fence.after_runs == 1
    await env.assert_nothing_persisted(wanted)


@pytest.mark.integration
async def test_previous_episode_uuids_match_the_retrieved_episodes_int(env):
    first = await env.seed_episode('first', NOW - timedelta(days=2))
    second = await env.seed_episode('second', NOW - timedelta(days=1))
    fence = _ProbeFence(env.group)

    await env.add(new_episode_uuid=str(uuid_lib.uuid4()), publish_fence=fence)

    # Retrieval is chronological (oldest first), exactly what the prompts received.
    assert env.retrieved == [[first.uuid, second.uuid]]
    assert fence.previous == [first.uuid, second.uuid]


@pytest.mark.integration
async def test_fence_reports_written_entity_uuids_int(env):
    fence = _ProbeFence(env.group)
    result = await env.add(publish_fence=fence)

    assert fence.node_uuids == [n.uuid for n in result.nodes]
    assert fence.edge_uuids == [e.uuid for e in result.edges]


@pytest.mark.integration
async def test_hook_write_lands_atomically_with_the_episode_int(env):
    wanted = str(uuid_lib.uuid4())
    fence = _ProbeFence(env.group)
    await env.add(new_episode_uuid=wanted, publish_fence=fence)

    assert await env.episodes_named(wanted) == 1
    assert await env.probes() == 1


@pytest.mark.integration
async def test_none_fence_matches_unfenced_behaviour_int(env):
    wanted = str(uuid_lib.uuid4())
    result = await env.add(new_episode_uuid=wanted, publish_fence=None)

    assert result.episode.uuid == wanted
    assert await env.episodes_named(wanted) == 1
    assert await env.entities() == 2
    assert await env.entity_edges() == 1
    assert await env.mentions() == 2
    assert await env.probes() == 0


@pytest.mark.integration
@pytest.mark.parametrize('failing_hook', ['before', 'after'])
async def test_transient_retry_reruns_hooks_without_duplicates_int(env, failing_hook):
    wanted = str(uuid_lib.uuid4())
    transient = Neo4jError._hydrate_neo4j(
        code='Neo.TransientError.Transaction.DeadlockDetected', message='simulated deadlock'
    )
    assert isinstance(transient, TransientError)
    fence = _ProbeFence(
        env.group,
        fail_before=transient if failing_hook == 'before' else None,
        fail_after=transient if failing_hook == 'after' else None,
        fail_first_only=True,
    )

    await env.add(new_episode_uuid=wanted, publish_fence=fence)

    assert fence.before_runs == 2
    assert fence.after_runs == (1 if failing_hook == 'before' else 2)
    assert await env.episodes_named(wanted) == 1
    assert await env.entities() == 2
    assert await env.entity_edges() == 1
    assert await env.mentions() == 2
    assert await env.probes() == 1
