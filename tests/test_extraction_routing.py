"""Focused tests for fork-native single-episode extraction routing (Phase C1g).

Covers:
- default route selection (combined vs separate fallback for custom edge schemas)
- hook overrides (route forcing, full extraction results, invalid returns)
- edge delivery without global symbol rebinding (precomputed edges skip
  ``extract_edges``) and separate-route fallback
- public ``add_episode`` wiring on the ordinary (combined) route, DB-free
- exception and cancellation semantics (no state to reset)
- concurrency isolation between overlapping routing decisions
- structural check that the routing mechanism imports no Menhir module
"""

import ast
import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

import graphiti_core.graphiti as graphiti_module
from graphiti_core.edges import EntityEdge
from graphiti_core.extraction_routing import (
    ExtractionRoute,
    SingleEpisodeExtractionContext,
    SingleEpisodeExtractionHook,
    SingleEpisodeExtractionResult,
    default_extraction_route,
)
from graphiti_core.graphiti import AddEpisodeResults, Graphiti
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

GROUP_ID = 'routing_test_group'
NOW = datetime(2026, 9, 14)


class _FakeEdgeType(BaseModel):
    strength: int


def _make_episode(name: str = 'test_episode') -> EpisodicNode:
    return EpisodicNode(
        name=name,
        group_id=GROUP_ID,
        labels=[],
        source=EpisodeType.message,
        source_description='test',
        content='Alice likes Bob',
        created_at=NOW,
        valid_at=NOW,
        entity_edges=[],
    )


def _make_node(name: str) -> EntityNode:
    return EntityNode(
        name=name,
        group_id=GROUP_ID,
        labels=['Entity'],
        created_at=NOW,
        summary='',
    )


def _make_edge(source: EntityNode, target: EntityNode, fact: str) -> EntityEdge:
    return EntityEdge(
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        group_id=GROUP_ID,
        name='LIKES',
        fact=fact,
        episodes=[],
        created_at=NOW,
    )


def _make_graphiti(hook=None) -> Graphiti:
    """Build a Graphiti without touching DB/network constructors."""
    graphiti = Graphiti.__new__(Graphiti)
    graphiti.clients = Mock()
    graphiti.single_episode_extraction_hook = hook
    return graphiti


def _combined_fixture():
    node_a = _make_node('Alice')
    node_b = _make_node('Bob')
    return (
        [node_a, node_b],
        [_make_edge(node_a, node_b, 'Alice likes Bob')],
        {node_a.uuid: [0], node_b.uuid: [0]},
    )


async def _route(
    graphiti: Graphiti,
    monkeypatch,
    *,
    edge_types=None,
    edge_type_map=None,
    combined_result=None,
    nodes_result=None,
):
    """Invoke _extract_single_episode with extraction functions stubbed out."""
    if edge_type_map is None:
        edge_type_map = {('Entity', 'Entity'): []}
    if combined_result is None:
        combined_result = _combined_fixture()
    if nodes_result is None:
        nodes_result = ([_make_node('Alice')], {'node': [0]})

    calls = {'combined': 0, 'separate': 0}

    async def fake_combined(clients, episode, previous_episodes, **kwargs):
        calls['combined'] += 1
        assert kwargs['edge_type_map'] == {('Entity', 'Entity'): []}
        return combined_result

    async def fake_separate(clients, episode, previous_episodes, *args, **kwargs):
        calls['separate'] += 1
        return nodes_result

    monkeypatch.setattr(graphiti_module, 'extract_nodes_and_edges', fake_combined)
    monkeypatch.setattr(graphiti_module, 'extract_nodes', fake_separate)

    result = await graphiti._extract_single_episode(
        _make_episode(),
        [],
        None,
        None,
        edge_type_map,
        edge_types,
        None,
    )
    return result, calls


def test_default_route_combined_without_edge_types(monkeypatch):
    assert default_extraction_route(None) is ExtractionRoute.COMBINED
    assert default_extraction_route({}) is ExtractionRoute.COMBINED


def test_default_route_separate_with_custom_edge_types(monkeypatch):
    assert default_extraction_route({'WEAK': _FakeEdgeType}) is ExtractionRoute.SEPARATE


async def test_default_policy_routes_combined(monkeypatch):
    graphiti = _make_graphiti()
    (nodes, edges, index_map, route), calls = await _route(graphiti, monkeypatch)

    assert route is ExtractionRoute.COMBINED
    assert calls == {'combined': 1, 'separate': 0}
    assert len(nodes) == 2
    assert edges is not None and len(edges) == 1
    assert index_map == {node.uuid: [0] for node in nodes}


async def test_custom_edge_schemas_fall_back_to_separate_route(monkeypatch):
    graphiti = _make_graphiti()
    (nodes, edges, index_map, route), calls = await _route(
        graphiti, monkeypatch, edge_types={'WEAK': _FakeEdgeType}
    )

    assert route is ExtractionRoute.SEPARATE
    assert calls == {'combined': 0, 'separate': 1}
    assert edges is None
    assert index_map == {'node': [0]}


async def test_hook_can_force_separate_route(monkeypatch):
    class ForceSeparate:
        async def extract_single_episode(self, context):
            return ExtractionRoute.SEPARATE

    graphiti = _make_graphiti(hook=ForceSeparate())
    (nodes, edges, index_map, route), calls = await _route(graphiti, monkeypatch)

    assert route is ExtractionRoute.SEPARATE
    assert calls == {'combined': 0, 'separate': 1}
    assert edges is None


async def test_hook_can_force_combined_route_despite_custom_edge_types(monkeypatch):
    class ForceCombined:
        async def extract_single_episode(self, context):
            return ExtractionRoute.COMBINED

    graphiti = _make_graphiti(hook=ForceCombined())
    (nodes, edges, index_map, route), calls = await _route(
        graphiti, monkeypatch, edge_types={'WEAK': _FakeEdgeType}
    )

    assert route is ExtractionRoute.COMBINED
    assert calls == {'combined': 1, 'separate': 0}
    assert edges is not None


async def test_hook_provided_result_skips_builtin_extraction(monkeypatch):
    node_a = _make_node('Alice')
    node_b = _make_node('Bob')
    edge = _make_edge(node_a, node_b, 'Alice likes Bob')
    hook_result = SingleEpisodeExtractionResult(
        nodes=[node_a, node_b],
        edges=[edge],
        node_episode_index_map={node_a.uuid: [0], node_b.uuid: [0]},
    )

    class Provider:
        async def extract_single_episode(self, context):
            return hook_result

    graphiti = _make_graphiti(hook=Provider())
    (nodes, edges, index_map, route), calls = await _route(graphiti, monkeypatch)

    assert route is None
    assert calls == {'combined': 0, 'separate': 0}
    assert nodes == hook_result.nodes
    assert edges == [edge]
    assert index_map == hook_result.node_episode_index_map


async def test_hook_receives_request_local_context(monkeypatch):
    captured: list[SingleEpisodeExtractionContext] = []
    edge_type_map = {('Entity', 'Entity'): []}

    class Recorder:
        async def extract_single_episode(self, context):
            captured.append(context)
            return None

    graphiti = _make_graphiti(hook=Recorder())
    await _route(
        graphiti, monkeypatch, edge_types={'WEAK': _FakeEdgeType}, edge_type_map=edge_type_map
    )

    assert len(captured) == 1
    context = captured[0]
    assert context.clients is graphiti.clients
    assert context.episode.group_id == GROUP_ID
    assert context.previous_episodes == []
    assert context.entity_types is None
    assert context.excluded_entity_types is None
    assert context.edge_type_map is edge_type_map
    assert context.edge_types == {'WEAK': _FakeEdgeType}
    assert context.custom_extraction_instructions is None


async def test_hook_invalid_return_raises_type_error(monkeypatch):
    class BadHook:
        async def extract_single_episode(self, context):
            return 'combined'

    graphiti = _make_graphiti(hook=BadHook())
    with pytest.raises(TypeError, match='single_episode_extraction_hook'):
        await _route(graphiti, monkeypatch)


async def test_hook_exception_propagates_and_state_is_clean(monkeypatch):
    class ExplodingHook:
        async def extract_single_episode(self, context):
            raise RuntimeError('hook exploded')

    graphiti = _make_graphiti(hook=ExplodingHook())
    with pytest.raises(RuntimeError, match='hook exploded'):
        await _route(graphiti, monkeypatch)

    # After the failure, the same instance routes normally (nothing to reset).
    graphiti.single_episode_extraction_hook = None
    (nodes, edges, index_map, route), calls = await _route(graphiti, monkeypatch)
    assert route is ExtractionRoute.COMBINED
    assert calls == {'combined': 1, 'separate': 0}


async def test_cancellation_propagates_and_instance_remains_usable(monkeypatch):
    class CancellingHook:
        async def extract_single_episode(self, context):
            raise asyncio.CancelledError()

    graphiti = _make_graphiti(hook=CancellingHook())
    with pytest.raises(asyncio.CancelledError):
        await _route(graphiti, monkeypatch)

    graphiti.single_episode_extraction_hook = None
    (nodes, edges, index_map, route), calls = await _route(graphiti, monkeypatch)
    assert route is ExtractionRoute.COMBINED


async def test_concurrent_routing_calls_do_not_leak_state(monkeypatch):
    node_a = _make_node('Alice')
    node_b = _make_node('Bob')

    class ScopedHook:
        def __init__(self, result):
            self.result = result
            self.saw_episode = None

        async def extract_single_episode(self, context):
            self.saw_episode = context.episode.name
            await asyncio.sleep(0)
            return self.result

    result_combined = SingleEpisodeExtractionResult(
        nodes=[node_a], edges=[], node_episode_index_map={}
    )
    result_custom = SingleEpisodeExtractionResult(
        nodes=[node_b], edges=[], node_episode_index_map={}
    )
    hook_a = ScopedHook(result_combined)
    hook_b = ScopedHook(result_custom)
    graphiti_a = _make_graphiti(hook=hook_a)
    graphiti_b = _make_graphiti(hook=hook_b)

    combined, custom = await asyncio.gather(
        _route(graphiti_a, monkeypatch),
        _route(graphiti_b, monkeypatch, edge_types={'WEAK': _FakeEdgeType}),
    )

    (nodes_a, edges_a, index_map_a, route_a), calls_a = combined
    (nodes_b, edges_b, index_map_b, route_b), calls_b = custom

    assert hook_a.saw_episode == hook_b.saw_episode == 'test_episode'
    assert route_a is None and route_b is None
    assert calls_a == calls_b == {'combined': 0, 'separate': 0}
    assert nodes_a == [node_a] and nodes_b == [node_b]
    assert index_map_a == {} and index_map_b == {}


async def test_precomputed_edges_skip_extract_edges(monkeypatch):
    """Combined/hook edges are carried as arguments; extract_edges is never called."""
    node_a = _make_node('Alice')
    node_b = _make_node('Bob')
    edge = _make_edge(node_a, node_b, 'Alice likes Bob')

    async def forbidden_extract_edges(*args, **kwargs):
        raise AssertionError('extract_edges must not run when edges are precomputed')

    def fake_resolve_edge_pointers(edges, uuid_map):
        return edges

    async def fake_resolve_extracted_edges(
        clients, edges, episode, nodes, edge_types, edge_type_map
    ):
        return edges, [], list(edges)

    monkeypatch.setattr(graphiti_module, 'extract_edges', forbidden_extract_edges)
    monkeypatch.setattr(graphiti_module, 'resolve_edge_pointers', fake_resolve_edge_pointers)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edges', fake_resolve_extracted_edges)

    graphiti = _make_graphiti()
    resolved, invalidated, new_edges = await graphiti._extract_and_resolve_edges(
        _make_episode(),
        [node_a, node_b],
        [],
        {('Entity', 'Entity'): []},
        GROUP_ID,
        None,
        [node_a, node_b],
        {},
        precomputed_edges=[edge],
    )

    assert resolved == [edge]
    assert invalidated == []
    assert new_edges == [edge]


async def test_separate_route_still_calls_extract_edges(monkeypatch):
    """Fallback path: without precomputed edges, extract_edges runs as before."""
    node_a = _make_node('Alice')
    node_b = _make_node('Bob')
    edge = _make_edge(node_a, node_b, 'Alice likes Bob')
    extract_edges_calls: list[list[EntityNode]] = []

    async def fake_extract_edges(clients, episode, extracted_nodes, *args, **kwargs):
        extract_edges_calls.append(extracted_nodes)
        return [edge]

    def fake_resolve_edge_pointers(edges, uuid_map):
        return edges

    async def fake_resolve_extracted_edges(
        clients, edges, episode, nodes, edge_types, edge_type_map
    ):
        return edges, [], list(edges)

    monkeypatch.setattr(graphiti_module, 'extract_edges', fake_extract_edges)
    monkeypatch.setattr(graphiti_module, 'resolve_edge_pointers', fake_resolve_edge_pointers)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edges', fake_resolve_extracted_edges)

    graphiti = _make_graphiti()
    resolved, invalidated, new_edges = await graphiti._extract_and_resolve_edges(
        _make_episode(),
        [node_a, node_b],
        [],
        {('Entity', 'Entity'): []},
        GROUP_ID,
        None,
        [node_a, node_b],
        {},
    )

    assert extract_edges_calls == [[node_a, node_b]]
    assert resolved == [edge]
    assert new_edges == [edge]


@pytest.mark.parametrize('base_database', [GROUP_ID, 'another_database'])
async def test_add_episode_combined_route_wiring_end_to_end(monkeypatch, base_database):
    """Public add_episode wiring: combined route carries edges; extract_edges never runs."""
    node_a, node_b = _make_node('Alice'), _make_node('Bob')
    edge = _make_edge(node_a, node_b, 'Alice likes Bob')
    episode = _make_episode()
    calls = {'combined': 0, 'separate_nodes': 0, 'extract_edges': 0, 'precomputed': 0}

    async def fake_combined(clients, ep, previous_episodes, **kwargs):
        assert clients.driver._database == GROUP_ID
        calls['combined'] += 1
        return [node_a, node_b], [edge], {node_a.uuid: [0], node_b.uuid: [0]}

    async def forbidden_extract_nodes(*args, **kwargs):
        calls['separate_nodes'] += 1
        raise AssertionError('extract_nodes must not run on the combined route')

    async def forbidden_extract_edges(*args, **kwargs):
        calls['extract_edges'] += 1
        raise AssertionError('extract_edges must not run on the combined route')

    async def fake_resolve_nodes(clients, extracted_nodes, ep, previous_episodes, entity_types):
        assert clients.driver._database == GROUP_ID
        return [node_a, node_b], {}, []

    def fake_resolve_pointers(edges, uuid_map):
        calls['precomputed'] += 1
        return edges

    async def fake_resolve_edges(clients, edges, ep, nodes, edge_types, edge_type_map):
        assert clients.driver._database == GROUP_ID
        assert edges == [edge]
        return edges, [], list(edges)

    async def fake_extract_attributes(clients, nodes, ep, previous_episodes, entity_types, edges):
        assert clients.driver._database == GROUP_ID
        return nodes

    monkeypatch.setattr(graphiti_module, 'extract_nodes_and_edges', fake_combined)
    monkeypatch.setattr(graphiti_module, 'extract_nodes', forbidden_extract_nodes)
    monkeypatch.setattr(graphiti_module, 'extract_edges', forbidden_extract_edges)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_nodes', fake_resolve_nodes)
    monkeypatch.setattr(graphiti_module, 'resolve_edge_pointers', fake_resolve_pointers)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edges', fake_resolve_edges)
    monkeypatch.setattr(graphiti_module, 'extract_attributes_from_nodes', fake_extract_attributes)
    monkeypatch.setattr(graphiti_module, 'get_default_group_id', lambda provider: GROUP_ID)

    graphiti = _make_graphiti()
    graphiti.driver = Mock()
    graphiti.driver._database = base_database
    graphiti.driver.clone.side_effect = lambda database: Mock(_database=database)
    graphiti.clients.driver = graphiti.driver
    graphiti.clients.model_copy.side_effect = lambda update: Mock(**update)

    async def fake_retrieve_episodes(*args, **kwargs):
        return []

    async def fake_process_episode_data(
        episode,
        nodes,
        entity_edges,
        now,
        group_id,
        saga=None,
        saga_previous_episode_uuid=None,
        node_episode_index_map=None,
        clients=None,
    ):
        assert clients.driver._database == GROUP_ID
        assert entity_edges == [edge]
        return [], episode

    graphiti.retrieve_episodes = fake_retrieve_episodes
    graphiti._process_episode_data = fake_process_episode_data

    span = Mock()
    span_cm = Mock()
    span_cm.__enter__ = Mock(return_value=span)
    span_cm.__exit__ = Mock(return_value=False)
    graphiti.tracer = Mock()
    graphiti.tracer.start_span.return_value = span_cm

    result: AddEpisodeResults = await graphiti.add_episode(
        name=episode.name,
        episode_body=episode.content,
        source_description='test',
        reference_time=NOW,
        group_id=GROUP_ID,
    )

    assert calls == {
        'combined': 1,
        'separate_nodes': 0,
        'extract_edges': 0,
        'precomputed': 1,
    }
    assert result.edges == [edge]
    assert result.nodes == [node_a, node_b]
    assert graphiti.driver._database == base_database
    assert graphiti.clients.driver is graphiti.driver
    span.add_attributes.assert_called_once()
    assert span.add_attributes.call_args[0][0]['extraction.route'] == 'combined'


def test_routing_mechanism_imports_no_menhir_modules():
    """Structural check: routing code must not import any menhir package/module."""
    repo_root = Path(__file__).resolve().parents[1]
    targets = [
        repo_root / 'graphiti_core' / 'extraction_routing.py',
        repo_root / 'graphiti_core' / 'graphiti.py',
    ]
    for path in targets:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                module_names = [node.module] if node.module else []
            else:
                continue
            for module_name in module_names:
                assert module_name.split('.')[0].lower() != 'menhir', (
                    f'{path.name} imports Menhir module: {module_name}'
                )


def test_hook_protocol_is_runtime_checkable():
    class Hook:
        async def extract_single_episode(self, context):
            return None

    assert isinstance(Hook(), SingleEpisodeExtractionHook)
    assert not isinstance(object(), SingleEpisodeExtractionHook)
