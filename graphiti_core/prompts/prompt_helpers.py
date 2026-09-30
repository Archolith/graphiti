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
"""

import json
from typing import Any

DO_NOT_ESCAPE_UNICODE = '\nDo not escape unicode characters.\n'

_EMBEDDING_KEY_SUFFIX = '_embedding'
_MIN_VECTOR_LENGTH = 64
_SAMPLED_HEAD_LENGTH = 8
# Menhir merge bookkeeping stored as entity attributes. Node attributes are serialized into the
# dedup, summary and attribute-extraction contexts, so without this the audit trail reaches the
# model and grows with every merge.
_MERGE_LINEAGE_KEYS = frozenset({'merge_audit', 'merged_from', 'last_merge_op_id'})


def without_merge_lineage(attributes: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an attributes dict without merge-lineage keys.

    For prompt contexts that are not rendered through ``to_prompt_json``.
    """
    return {key: value for key, value in attributes.items() if key not in _MERGE_LINEAGE_KEYS}


def _looks_like_embedding_vector(value: Any) -> bool:
    """
    Check whether a value structurally looks like an embedding vector.

    The rule is intentionally a sampled-head check: the value must be a list or
    tuple longer than 64 items whose FIRST EIGHT items are int/float (bool
    excluded). Short numeric lists, bool lists, and string lists are preserved.
    """
    if not isinstance(value, (list, tuple)):
        return False
    if len(value) <= _MIN_VECTOR_LENGTH:
        return False
    return all(
        isinstance(item, (int, float)) and not isinstance(item, bool)
        for item in value[:_SAMPLED_HEAD_LENGTH]
    )


def _convert_value(value: Any) -> Any:
    """
    Convert an unsupported leaf value into a JSON-serializable primitive.

    Fallback order: callable ``isoformat``, then ``iso_format``, then
    ``to_native``. Each is called without arguments. If a conversion returns
    the same object, method traversal stops and ``str(value)`` is used. If a
    conversion returns str/int/float/bool/None, it is returned directly.
    Otherwise the fallback runs recursively on the converted value. If no
    conversion applies, ``str(value)`` is used. Exceptions raised by malformed
    conversion methods propagate unchanged.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    for attr in ('isoformat', 'iso_format', 'to_native'):
        method = getattr(value, attr, None)
        if callable(method):
            converted = method()
            if converted is value:
                return str(value)
            if converted is None or isinstance(converted, (str, int, float, bool)):
                return converted
            return _convert_value(converted)

    return str(value)


def _normalize(value: Any) -> Any:
    """
    Recursively copy and normalize a value for prompt serialization.

    Dict entries whose string key ends with ``_embedding``, is a merge-lineage
    key (``merge_audit``, ``merged_from``, ``last_merge_op_id``), or whose value
    structurally looks like an embedding vector are omitted. Nested lists and
    tuples are copied recursively. The caller's input is never mutated.
    """
    if isinstance(value, dict):
        normalized: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and (
                key.endswith(_EMBEDDING_KEY_SUFFIX) or key in _MERGE_LINEAGE_KEYS
            ):
                continue
            if _looks_like_embedding_vector(item):
                continue
            normalized[key] = _normalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return _convert_value(value)


def to_prompt_json(data: Any, ensure_ascii: bool = False, indent: int | None = None) -> str:
    """
    Serialize data to JSON for use in prompts.

    Args:
        data: The data to serialize
        ensure_ascii: If True, escape non-ASCII characters. If False (default), preserve them.
        indent: Number of spaces for indentation. Defaults to None (minified).

    Returns:
        JSON string representation of the data

    Notes:
        By default (ensure_ascii=False), non-ASCII characters (e.g., Korean, Japanese, Chinese)
        are preserved in their original form in the prompt, making them readable
        in LLM logs and improving model understanding.

        Embedding vectors and merge lineage (``merge_audit``, ``merged_from``,
        ``last_merge_op_id``) are removed before serialization so prompts stay small:
        dict entries whose string key ends with ``_embedding`` are dropped, as are
        list/tuple values longer than 64 items whose first eight items are numbers
        (bool excluded). Temporal and other non-JSON-native values are converted
        via ``isoformat``/``iso_format``/``to_native`` and ultimately ``str``.
        The input data is never mutated.
    """
    return json.dumps(_normalize(data), ensure_ascii=ensure_ascii, indent=indent)
