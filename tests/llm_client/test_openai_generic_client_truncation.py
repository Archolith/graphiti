"""A cut-off or malformed reply must fail as such, never parse as a nested fragment."""

import json
from types import SimpleNamespace

import pytest

from graphiti_core.llm_client import OutputTruncatedError
from graphiti_core.llm_client.client import is_server_or_retry_error
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.llm_client.request_guard import RequestGuard
from graphiti_core.prompts.models import Message

VALID = '{"entities": [{"name": "a"}], "edges": [{"fact": "x"}]}'
CUT_IN_EDGES = '{"entities": [{"name": "a"}], "edges": [{"fact": "x"}, {"fa'
CUT_IN_ENTITIES = '{"entities": [{"name": "a"}, {"na'
BAD_TEXT_IN_EDGES = '{"entities": [], "edges": [{"fact": "x"}, oops]}'
BAD_TEXT_BETWEEN_KEYS = '{"entities": [{"name": "a"}] oops, "edges": []}'
CUT_ARRAY = '[{"name": "a"}, {"na'


class Completions:
    def __init__(self, content: str, finish_reason: str | None):
        self.calls = 0
        self.content = content
        self.finish_reason = finish_reason

    async def create(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                    finish_reason=self.finish_reason,
                )
            ]
        )


def _client(
    content: str, finish_reason: str | None = 'stop', guard: RequestGuard | None = None
) -> tuple[OpenAIGenericClient, Completions]:
    completions = Completions(content, finish_reason)
    client = OpenAIGenericClient(
        config=LLMConfig(api_key='test', model='test-model'),
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions), base_url=None),
        request_guard=guard,
    )
    return client, completions


def _messages() -> list[Message]:
    return [Message(role='system', content='s'), Message(role='user', content='u')]


@pytest.mark.parametrize('content', [CUT_IN_EDGES, CUT_IN_ENTITIES, VALID, ''])
async def test_length_finish_raises_truncation_for_any_body(content):
    client, _ = _client(content, finish_reason='length')

    with pytest.raises(OutputTruncatedError, match='max_tokens=123'):
        await client._generate_response(_messages(), max_tokens=123)


@pytest.mark.parametrize(
    'content',
    [
        CUT_IN_EDGES,
        CUT_IN_ENTITIES,
        BAD_TEXT_IN_EDGES,
        BAD_TEXT_BETWEEN_KEYS,
        CUT_ARRAY,
        f'```json\n{CUT_IN_EDGES}',
        f'  \n{CUT_IN_ENTITIES}',
    ],
)
@pytest.mark.parametrize('finish_reason', ['stop', None])
async def test_broken_document_raises_instead_of_returning_a_fragment(content, finish_reason):
    client, _ = _client(content, finish_reason=finish_reason)

    with pytest.raises(json.JSONDecodeError):
        await client._generate_response(_messages())


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        (VALID, json.loads(VALID)),
        (f'  {VALID}  ', json.loads(VALID)),
        (f'```json\n{VALID}\n```', json.loads(VALID)),
        ('{"foo": "bar"} note: {incomplete', {'foo': 'bar'}),
        ('Here it is: {"foo": "bar"}', {'foo': 'bar'}),
        ('{broken {"foo": "bar"}', {'foo': 'bar'}),
        ('[Note] {"foo": "bar"}', {'foo': 'bar'}),
    ],
)
async def test_complete_and_prose_wrapped_replies_still_parse(content, expected):
    client, _ = _client(content)

    assert await client._generate_response(_messages()) == expected


async def test_truncation_is_not_retried_at_the_same_limit():
    client, completions = _client(CUT_IN_EDGES, finish_reason='length')

    with pytest.raises(OutputTruncatedError):
        await client.generate_response(_messages())

    assert completions.calls == 1
    assert is_server_or_retry_error(OutputTruncatedError('cut off')) is False


async def test_failure_listener_sees_truncation_with_response_metadata():
    failures = []

    class Listener:
        def on_request_failed(self, context, error, phase, response):
            failures.append((error, phase, response))

    client, _ = _client(
        CUT_IN_EDGES, finish_reason='length', guard=RequestGuard(failure_listener=Listener())
    )

    with pytest.raises(OutputTruncatedError):
        await client._generate_response(_messages())

    assert len(failures) == 1
    error, phase, response = failures[0]
    assert isinstance(error, OutputTruncatedError)
    assert phase == 'provider_error'
    assert response is not None
