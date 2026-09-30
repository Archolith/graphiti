from unittest.mock import AsyncMock, MagicMock

from pydantic import BaseModel

from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.maintenance.node_operations import (
    _extract_entity_attributes,
    extract_attributes_from_nodes,
)


class EmptySchema(BaseModel):
    pass


class PersonSchema(BaseModel):
    favorite_food: str


def _make_clients():
    driver = MagicMock()
    embedder = MagicMock()
    embedder.create_batch = AsyncMock(side_effect=lambda inputs: [[0.0, 0.0] for _ in inputs])
    cross_encoder = MagicMock()
    llm_client = MagicMock()
    llm_generate = AsyncMock()
    llm_client.generate_response = llm_generate

    clients = GraphitiClients.model_construct(
        driver=driver,
        embedder=embedder,
        cross_encoder=cross_encoder,
        llm_client=llm_client,
    )

    return clients, llm_generate


async def test_untyped_entity_type_preserves_attributes_without_llm():
    clients, llm_generate = _make_clients()
    node = EntityNode(
        name='Alice',
        group_id='group',
        labels=['Entity'],
        attributes={'external_property': 'keep-me'},
    )

    result = await _extract_entity_attributes(clients.llm_client, node, None, None, None)

    assert result == {'external_property': 'keep-me'}
    llm_generate.assert_not_awaited()


async def test_untyped_result_is_distinct_shallow_copy():
    clients, llm_generate = _make_clients()
    node = EntityNode(
        name='Bob',
        group_id='group',
        labels=['Entity'],
        attributes={'external_property': 'keep-me'},
    )

    result = await _extract_entity_attributes(clients.llm_client, node, None, None, None)

    assert result is not node.attributes
    assert result == node.attributes
    result['external_property'] = 'mutated'
    assert node.attributes['external_property'] == 'keep-me'
    llm_generate.assert_not_awaited()


async def test_empty_fields_schema_preserves_attributes_without_llm():
    clients, llm_generate = _make_clients()
    node = EntityNode(
        name='Carol',
        group_id='group',
        labels=['Entity'],
        attributes={'external_property': 'keep-me'},
    )

    result = await _extract_entity_attributes(clients.llm_client, node, None, None, EmptySchema)

    assert result == {'external_property': 'keep-me'}
    llm_generate.assert_not_awaited()


async def test_untyped_empty_attributes_returns_empty_dict():
    clients, llm_generate = _make_clients()
    node = EntityNode(name='Dave', group_id='group', labels=['Entity'])

    result = await _extract_entity_attributes(clients.llm_client, node, None, None, None)

    assert result == {}
    llm_generate.assert_not_awaited()


async def test_extract_attributes_from_nodes_preserves_untyped_attributes():
    clients, llm_generate = _make_clients()
    typed_node = EntityNode(
        name='Eve',
        group_id='group',
        labels=['Entity', 'Person'],
        attributes={'external_property': 'keep-me'},
    )
    untyped_node = EntityNode(
        name='Frank',
        group_id='group',
        labels=['Entity'],
        attributes={'external_property': 'keep-me'},
    )
    original_untyped_attributes = untyped_node.attributes
    llm_generate.return_value = {'favorite_food': 'sushi'}

    result = await extract_attributes_from_nodes(
        clients,
        [typed_node, untyped_node],
        entity_types={'Person': PersonSchema},
    )

    # Untyped node keeps its attributes via the returned copy (compare against the
    # pre-call mapping: result[1] is the same node object after assignment).
    assert result[1].attributes == original_untyped_attributes
    assert result[1].attributes is not original_untyped_attributes

    # Typed node goes through the LLM and overlays extracted fields on prior attributes.
    llm_generate.assert_awaited_once()
    assert result[0].attributes == {'external_property': 'keep-me', 'favorite_food': 'sushi'}


async def test_typed_schema_overlay_keeps_fields_omitted_by_llm():
    clients, llm_generate = _make_clients()
    node = EntityNode(
        name='Grace',
        group_id='group',
        labels=['Entity', 'Person'],
        attributes={'favorite_food': 'ramen', 'external_property': 'keep-me'},
    )
    llm_generate.return_value = {'favorite_food': 'sushi'}

    result = await _extract_entity_attributes(clients.llm_client, node, None, None, PersonSchema)

    # LLM response wins for extracted fields; prior values are retained otherwise.
    assert result == {'external_property': 'keep-me', 'favorite_food': 'sushi'}
    llm_generate.assert_awaited_once()


async def test_typed_extraction_prompt_excludes_merge_lineage_but_node_keeps_it():
    clients, llm_generate = _make_clients()
    lineage = {
        'merge_audit': ['{"absorbed_uuid": "a"}'],
        'merged_from': ['a'],
        'last_merge_op_id': 'op-1',
    }
    node = EntityNode(
        name='Grace',
        group_id='group',
        labels=['Entity', 'Person'],
        attributes={'favorite_food': 'ramen', **lineage},
    )
    llm_generate.return_value = {'favorite_food': 'sushi'}

    result = await _extract_entity_attributes(clients.llm_client, node, None, None, PersonSchema)

    prompt = ''.join(message.content for message in llm_generate.await_args.args[0])
    assert not any(key in prompt for key in lineage)
    assert 'ramen' in prompt
    # Lineage is stored bookkeeping: it stays on the node, only the prompt omits it.
    assert result == {'favorite_food': 'sushi', **lineage}
