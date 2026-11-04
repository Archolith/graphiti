"""Validation of parsed LLM structured responses before model construction."""

import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from graphiti_core.errors import LLMResponseValidationError

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)


def parse_llm_response(model: type[T], llm_response: Any) -> T:
    """Build ``model`` from a parsed LLM response.

    A dict is passed straight to ``model(**llm_response)``. A list holding exactly
    one dict is unwrapped if that dict validates. Anything else raises
    ``LLMResponseValidationError`` without echoing the response content.
    """
    if isinstance(llm_response, dict):
        return model(**llm_response)

    if (
        isinstance(llm_response, list)
        and len(llm_response) == 1
        and isinstance(llm_response[0], dict)
    ):
        try:
            result = model(**llm_response[0])
        except ValidationError:
            pass
        else:
            logger.warning('Unwrapped single-element list LLM response for %s', model.__name__)
            return result

    raise LLMResponseValidationError(model.__name__, type(llm_response).__name__)
