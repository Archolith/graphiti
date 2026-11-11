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

Neutral typed extension hook that runs inside the publish transaction.

``add_episode`` persists an episode and everything derived from it in one managed
transaction. A caller passes a :class:`PublishFence` to ``add_episode`` to run its own
statements in that same transaction, before and after the graph writes. The fence is
policy-free: it receives the open transaction and the identifiers involved and decides
for itself what to read, write or veto. No module-global state is involved.
"""

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PublishFence(Protocol):
    """Protocol for callers that run statements inside the publish transaction.

    Both methods run inside the single publish transaction, on the same ``tx`` the graph
    writes use. An exception raised by either method propagates unchanged and rolls the
    whole transaction back, so nothing of the episode is persisted. A managed transaction
    can be retried by the driver after a transient error, so both methods must be
    idempotent: a second run against a fresh transaction must give the same result.

    - ``before_write``: runs after embeddings are generated and before the first save.
      ``previous_episode_uuids`` are the episodes the call actually retrieved as context.
    - ``after_write``: runs after the last save. ``entity_node_uuids`` and
      ``entity_edge_uuids`` are the entity nodes and edges written by this call.
    """

    async def before_write(
        self,
        tx: Any,
        *,
        episode_uuid: str,
        previous_episode_uuids: Sequence[str],
    ) -> None: ...

    async def after_write(
        self,
        tx: Any,
        *,
        episode_uuid: str,
        entity_node_uuids: Sequence[str],
        entity_edge_uuids: Sequence[str],
        previous_episode_uuids: Sequence[str],
    ) -> None: ...


def publish_fence_kwargs(
    fence: PublishFence | None,
    episode_uuid: str | None,
    previous_episode_uuids: Sequence[str] | None,
) -> dict[str, Any]:
    """Keyword arguments that thread ``fence`` to a bulk-write call.

    Empty when there is no fence, so the no-fence call keeps the legacy signature.
    """
    if fence is None:
        return {}
    return {
        'publish_fence': fence,
        'fence_episode_uuid': episode_uuid,
        'previous_episode_uuids': list(previous_episode_uuids or []),
    }
