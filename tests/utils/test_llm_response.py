import pytest
from pydantic import BaseModel, ValidationError

from graphiti_core.errors import GraphitiError, LLMResponseValidationError
from graphiti_core.utils.llm_response import parse_llm_response


class Item(BaseModel):
    name: str


class Envelope(BaseModel):
    items: list[Item]


VALID = {'items': [{'name': 'a'}]}


def test_dict_passes_through():
    result = parse_llm_response(Envelope, VALID)
    assert result == Envelope(**VALID)


def test_invalid_dict_raises_pydantic_validation_error():
    with pytest.raises(ValidationError):
        parse_llm_response(Envelope, {'items': 'nope'})


def test_single_element_list_with_valid_envelope_is_unwrapped():
    assert parse_llm_response(Envelope, [VALID]) == Envelope(**VALID)


def test_single_element_list_with_non_envelope_dict_raises():
    with pytest.raises(LLMResponseValidationError):
        parse_llm_response(Envelope, [{'name': 'a'}])


@pytest.mark.parametrize('payload', [[VALID, VALID], [], 'secret-text', None, 5, ['x']])
def test_other_shapes_raise(payload):
    with pytest.raises(LLMResponseValidationError) as exc_info:
        parse_llm_response(Envelope, payload)
    message = str(exc_info.value)
    assert 'Envelope' in message
    assert type(payload).__name__ in message
    assert 'secret-text' not in message


def test_error_does_not_include_content():
    with pytest.raises(LLMResponseValidationError) as exc_info:
        parse_llm_response(Envelope, [{'name': 'SENSITIVE'}])
    assert 'SENSITIVE' not in str(exc_info.value)


def test_error_is_graphiti_and_value_error():
    err = LLMResponseValidationError('M', 'list')
    assert isinstance(err, GraphitiError)
    assert isinstance(err, ValueError)
