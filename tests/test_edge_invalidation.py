"""Focused no-DB tests for the fork-native edge-invalidation (veto) hook.

Covers:
- no hook and an always-INVALIDATE hook are identical to upstream on paths 1, 2 and 3
- the motivating counterexample: a lower-authority source cannot end a user edge
- mixed decisions in one call, and pairs the hook must never be asked about
- NEWER_CANDIDATE (dated and undated-start branches) and OWN_END veto semantics
- copies of one stored edge across mentions (reconcile contract)
- strict return validation, exception propagation
- call-site wiring (resolve_extracted_edges, dedupe_edges_bulk, add_triplet, AST guard)
- no cross-talk between concurrent resolutions
"""

import ast
import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest

import graphiti_core.graphiti as graphiti_module
import graphiti_core.utils.bulk_utils as bulk_utils
import graphiti_core.utils.maintenance.edge_operations as edge_ops
from graphiti_core.cross_encoder import CrossEncoderClient
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.edge_expiry import (
    EdgeExpiryContext,
    EdgeExpiryDecision,
    edge_expiry_kwargs,
)
from graphiti_core.edge_invalidation import (
    EdgeInvalidationContext,
    EdgeInvalidationDecision,
    EdgeInvalidationReason,
    edge_invalidation_kwargs,
    evaluate_edge_invalidation_decision,
)
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder import EmbedderClient
from graphiti_core.errors import EdgeNotFoundError, NodeNotFoundError
from graphiti_core.graphiti import Graphiti
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_config import SearchResults
from graphiti_core.utils.maintenance.edge_operations import (
    resolve_extracted_edge,
    resolve_extracted_edges,
)

GROUP_ID = 'invalidation_test_group'
FIXED_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TRIP_START = datetime(2026, 3, 27, tzinfo=timezone.utc)
TRIP_END = datetime(2026, 4, 1, tzinfo=timezone.utc)
MILAN_START = datetime(2026, 3, 28, tzinfo=timezone.utc)

KEEP = EdgeInvalidationDecision.KEEP
INVALIDATE = EdgeInvalidationDecision.INVALIDATE
Reason = EdgeInvalidationReason


class PolicyHook:
    """Records every context and decides through a pure function of the context."""

    def __init__(self, policy: Callable[[EdgeInvalidationContext], object] = lambda c: INVALIDATE):
        self.policy = policy
        self.contexts: list[EdgeInvalidationContext] = []

    async def decide_edge_invalidation(self, context: EdgeInvalidationContext):
        self.contexts.append(context)
        return self.policy(context)

    def reasons(self) -> list[EdgeInvalidationReason]:
        return [c.reason for c in self.contexts]


class RecordingExpiryHook:
    def __init__(self, decision: EdgeExpiryDecision = EdgeExpiryDecision.EXPIRE):
        self.decision = decision
        self.contexts: list[EdgeExpiryContext] = []

    async def decide_edge_expiry(self, context: EdgeExpiryContext) -> EdgeExpiryDecision:
        self.contexts.append(context)
        return self.decision


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    monkeypatch.setattr(edge_ops, 'utc_now', lambda: FIXED_NOW)


def _edge(
    fact: str,
    *,
    valid_at: datetime | None = TRIP_START,
    invalid_at: datetime | None = None,
    expired_at: datetime | None = None,
    episodes: list[str] | None = None,
    attributes: dict | None = None,
    uuid: str | None = None,
) -> EntityEdge:
    return EntityEdge(
        uuid=uuid if uuid is not None else str(uuid4()),
        source_node_uuid='source_uuid',
        target_node_uuid='target_uuid',
        name='TRAVELED_TO',
        group_id=GROUP_ID,
        fact=fact,
        episodes=episodes if episodes is not None else [],
        created_at=FIXED_NOW - timedelta(days=30),
        valid_at=valid_at,
        invalid_at=invalid_at,
        expired_at=expired_at,
        attributes=dict(attributes) if attributes is not None else {},
    )


def _episode(uuid: str = 'episode_uuid', source_description: str = 'desc') -> EpisodicNode:
    return EpisodicNode(
        uuid=uuid,
        name='Episode',
        group_id=GROUP_ID,
        source=EpisodeType.message,
        source_description=source_description,
        content='Episode content',
        valid_at=FIXED_NOW,
    )


def _llm(duplicate_facts: list[int] | None = None, contradicted_facts: list[int] | None = None):
    client = MagicMock()
    client.generate_response = AsyncMock(
        return_value={
            'duplicate_facts': duplicate_facts or [],
            'contradicted_facts': contradicted_facts or [],
        }
    )
    return client


async def _run(
    hook,
    *,
    extracted: EntityEdge | None = None,
    related: list[EntityEdge] | None = None,
    existing: list[EntityEdge] | None = None,
    duplicate: list[int] | None = None,
    contradicted: list[int] | None = None,
    episode: EpisodicNode | None = None,
    expiry_hook=None,
):
    """Resolve one edge. ``duplicate`` indexes ``related``; ``contradicted`` indexes ``existing``."""
    related = related if related is not None else []
    existing = existing if existing is not None else []
    extracted = extracted or _edge('Alice traveled to Rome')
    llm = _llm(duplicate, [len(related) + i for i in (contradicted or [])])
    return await resolve_extracted_edge(
        llm,
        extracted,
        related,
        existing,
        episode or _episode(),
        None,
        **edge_expiry_kwargs(expiry_hook),
        **edge_invalidation_kwargs(hook),
    )


def _snapshot(edge: EntityEdge) -> dict:
    return edge.model_dump(exclude={'uuid'})


def _days(n: int) -> timedelta:
    return timedelta(days=n)


# --- helper contract ------------------------------------------------------------------------


def test_kwargs_helper_is_empty_without_hook():
    hook = PolicyHook()
    assert edge_invalidation_kwargs(None) == {}
    assert edge_invalidation_kwargs(hook) == {'edge_invalidation_hook': hook}


@pytest.mark.parametrize('bad', [True, 'keep', None, 1])
def test_evaluate_rejects_non_enum(bad):
    with pytest.raises(TypeError, match='EdgeInvalidationDecision'):
        evaluate_edge_invalidation_decision(bad)
    assert evaluate_edge_invalidation_decision(KEEP) is KEEP


# --- 1. no-hook identity --------------------------------------------------------------------


def _scenario_own_end():
    stored = _edge('Alice was in Rome', invalid_at=TRIP_END, episodes=['first'])
    return {'related': [stored], 'duplicate': [0], 'extracted': _edge('Alice visited')}, [stored]


def _scenario_newer():
    c1, c2 = (
        _edge('Milan', valid_at=TRIP_START + _days(2)),
        _edge('Oslo', valid_at=TRIP_START + _days(5)),
    )
    return {'existing': [c1, c2], 'contradicted': [1, 0]}, [c1, c2]


def _scenario_contradiction():
    o1 = _edge('Paris', valid_at=TRIP_START - _days(10))
    o2 = _edge('Lyon', valid_at=TRIP_START - _days(20))
    ended = _edge('Oslo', valid_at=TRIP_START - _days(30), invalid_at=TRIP_START - _days(25))
    return {'existing': [o1, o2, ended], 'contradicted': [0, 1, 2]}, [o1, o2, ended]


def _scenario_undated_world_end():
    milan = _edge('Milan', valid_at=TRIP_END - _days(12))
    extracted = _edge('Rome until Apr 1', valid_at=None, invalid_at=TRIP_END)
    return {'existing': [milan], 'contradicted': [0], 'extracted': extracted, 'world_end': True}, [
        milan
    ]


_SCENARIOS = {
    'own_end': _scenario_own_end,
    'newer': _scenario_newer,
    'contradiction': _scenario_contradiction,
    'undated_world_end': _scenario_undated_world_end,
}


async def _run_scenario(name: str, hook):
    kwargs, tracked = _SCENARIOS[name]()
    world_end = kwargs.pop('world_end', False)
    expiry = RecordingExpiryHook(EdgeExpiryDecision.WORLD_END) if world_end else None
    resolved, invalidated, duplicates = await _run(hook, expiry_hook=expiry, **kwargs)
    return {
        'resolved': _snapshot(resolved),
        'invalidated': [tracked.index(e) for e in invalidated],
        'duplicates': [tracked.index(e) for e in duplicates],
        'tracked': [_snapshot(e) for e in tracked],
    }


@pytest.mark.parametrize('name', list(_SCENARIOS))
async def test_always_invalidate_hook_is_identical_to_no_hook(name):
    """Fails if a hook that never vetoes changes any result (fields, order, identity)."""
    baseline = await _run_scenario(name, None)
    hook = PolicyHook()
    with_hook = await _run_scenario(name, hook)
    assert with_hook == baseline
    assert hook.contexts  # the hook really ran in every scenario


# --- 2. source-tier counterexample ----------------------------------------------------------


def _tier_policy(context: EdgeInvalidationContext):
    agent_episode = context.episode is not None and context.episode.source_description == (
        'agent-writer'
    )
    if context.target_edge.attributes.get('source') == 'user' and agent_episode:
        return KEEP
    return INVALIDATE


async def test_agent_episode_cannot_end_a_user_edge():
    stored = _edge(
        'Alice lives in Rome', valid_at=TRIP_START - _days(10), attributes={'source': 'user'}
    )
    new = _edge('Alice lives in Milan', valid_at=TRIP_START)
    hook = PolicyHook(_tier_policy)

    resolved, invalidated, _ = await _run(
        hook,
        extracted=new,
        existing=[stored],
        contradicted=[0],
        episode=_episode(source_description='agent-writer'),
    )

    assert resolved is new
    assert invalidated == []
    assert stored.invalid_at is None and stored.expired_at is None
    assert hook.reasons() == [Reason.CONTRADICTION]


async def test_control_user_episode_ends_a_user_edge():
    stored = _edge(
        'Alice lives in Rome', valid_at=TRIP_START - _days(10), attributes={'source': 'user'}
    )
    new = _edge('Alice lives in Milan', valid_at=TRIP_START)

    _, invalidated, _ = await _run(
        PolicyHook(_tier_policy),
        extracted=new,
        existing=[stored],
        contradicted=[0],
        episode=_episode(source_description='user-chat'),
    )

    assert invalidated == [stored]
    assert stored.invalid_at == TRIP_START and stored.expired_at == FIXED_NOW


async def test_user_episode_ends_an_agent_edge():
    stored = _edge(
        'Alice lives in Rome', valid_at=TRIP_START - _days(10), attributes={'source': 'agent'}
    )
    new = _edge('Alice lives in Milan', valid_at=TRIP_START)

    _, invalidated, _ = await _run(
        PolicyHook(_tier_policy),
        extracted=new,
        existing=[stored],
        contradicted=[0],
        episode=_episode(source_description='user-chat'),
    )

    assert invalidated == [stored]
    assert stored.expired_at == FIXED_NOW


# --- 3. mixed decisions ---------------------------------------------------------------------


async def test_mixed_decisions_only_vetoed_candidate_survives_and_order_is_kept():
    def build():
        o1 = _edge('Paris', valid_at=TRIP_START - _days(10))
        o2 = _edge('Lyon', valid_at=TRIP_START - _days(20))
        o3 = _edge('Oslo', valid_at=TRIP_START - _days(30))
        return [o1, o2, o3]

    baseline_edges = build()
    _, baseline, _ = await _run(None, existing=baseline_edges, contradicted=[0, 2, 1])
    baseline_order = [baseline_edges.index(e) for e in baseline]
    assert len(baseline_order) == 3

    edges = build()
    middle = edges[1]
    hook = PolicyHook(lambda c: KEEP if c.target_edge is middle else INVALIDATE)
    _, invalidated, _ = await _run(hook, existing=edges, contradicted=[0, 2, 1])

    assert [edges.index(e) for e in invalidated] == [i for i in baseline_order if i != 1]
    assert middle.invalid_at is None and middle.expired_at is None
    for e in invalidated:
        assert e.invalid_at == TRIP_START and e.expired_at == FIXED_NOW
    assert len(hook.contexts) == 3
    assert all(c.reason is Reason.CONTRADICTION and not c.target_is_new for c in hook.contexts)


async def test_contradiction_context_names_candidate_as_target_and_extracted_as_cause():
    stored = _edge('Rome', valid_at=TRIP_START - _days(5))
    new = _edge('Milan', valid_at=TRIP_START)
    hook = PolicyHook()
    resolved, _, _ = await _run(hook, extracted=new, existing=[stored], contradicted=[0])
    (context,) = hook.contexts
    assert context.target_edge is stored
    assert context.cause_edge is new and resolved is new
    assert context.episode is not None and context.episode.uuid == 'episode_uuid'


# --- 4. pairs the hook must never be asked about --------------------------------------------


async def test_hook_not_asked_for_candidate_the_predicate_skips():
    ended = _edge('Oslo', valid_at=TRIP_START - _days(30), invalid_at=TRIP_START - _days(25))
    hook = PolicyHook(lambda c: KEEP)
    _, invalidated, _ = await _run(hook, existing=[ended], contradicted=[0])
    assert hook.contexts == []
    assert invalidated == []
    assert ended.expired_at is None


async def test_hook_not_asked_for_new_edges_own_end():
    extracted = _edge('Rome trip', invalid_at=TRIP_END)
    hook = PolicyHook(lambda c: KEEP)
    expiry = RecordingExpiryHook()
    resolved, _, _ = await _run(
        hook, extracted=extracted, existing=[_edge('x', valid_at=TRIP_END)], expiry_hook=expiry
    )
    assert resolved is extracted
    assert Reason.OWN_END not in hook.reasons()
    assert resolved.expired_at == FIXED_NOW  # not vetoable: the edge expiry hook owns it
    assert len(expiry.contexts) == 1


# --- 5. NEWER_CANDIDATE ---------------------------------------------------------------------


async def test_newer_candidate_first_keep_second_invalidate():
    c1 = _edge('Milan', valid_at=TRIP_START + _days(2))
    c2 = _edge('Oslo', valid_at=TRIP_START + _days(5))
    hook = PolicyHook(lambda c: KEEP if c.cause_edge is c1 else INVALIDATE)

    resolved, _, _ = await _run(hook, existing=[c2, c1], contradicted=[0, 1])

    assert hook.reasons() == [Reason.NEWER_CANDIDATE, Reason.NEWER_CANDIDATE]
    assert [c.cause_edge for c in hook.contexts] == [c1, c2]
    assert resolved.expired_at == FIXED_NOW
    assert resolved.invalid_at == c2.valid_at


async def test_newer_candidates_all_keep_leaves_resolved_unexpired():
    c1 = _edge('Milan', valid_at=TRIP_START + _days(2))
    c2 = _edge('Oslo', valid_at=TRIP_START + _days(5))
    hook = PolicyHook(lambda c: KEEP)

    resolved, invalidated, _ = await _run(hook, existing=[c1, c2], contradicted=[0, 1])

    assert len(hook.contexts) == 2
    assert resolved.expired_at is None and resolved.invalid_at is None
    assert invalidated == []


async def test_newer_candidate_stops_at_first_invalidate():
    c1 = _edge('Milan', valid_at=TRIP_START + _days(2))
    c2 = _edge('Oslo', valid_at=TRIP_START + _days(5))
    hook = PolicyHook()
    resolved, _, _ = await _run(hook, existing=[c1, c2], contradicted=[0, 1])
    assert [c.cause_edge for c in hook.contexts] == [c1]
    assert resolved.invalid_at == c1.valid_at


async def test_target_is_new_true_for_new_edge_false_for_matched_duplicate():
    c = _edge('Milan', valid_at=TRIP_START + _days(2))
    hook_new = PolicyHook()
    new = _edge('Alice in Rome')
    await _run(hook_new, extracted=new, existing=[c], contradicted=[0])
    (ctx,) = hook_new.contexts
    assert ctx.target_edge is new and ctx.target_is_new is True

    stored = _edge('Alice was in Rome', episodes=['first'])
    c2 = _edge('Milan', valid_at=TRIP_START + _days(2))
    hook_dup = PolicyHook()
    resolved, _, _ = await _run(
        hook_dup, extracted=_edge('Alice visited'), related=[stored], duplicate=[0],
        existing=[c2], contradicted=[0],
    )  # fmt: skip
    assert resolved is stored
    (ctx,) = hook_dup.contexts
    assert ctx.reason is Reason.NEWER_CANDIDATE
    assert ctx.target_edge is stored and ctx.target_is_new is False


# --- 6. NEWER_CANDIDATE, undated-start branch -----------------------------------------------


@pytest.mark.parametrize('decision, expired', [(KEEP, None), (INVALIDATE, FIXED_NOW)])
async def test_undated_start_world_end_candidate_is_vetoable(decision, expired):
    extracted = _edge('Rome until Apr 1', valid_at=None, invalid_at=TRIP_END)
    milan = _edge('Milan', valid_at=TRIP_END - _days(12))
    hook = PolicyHook(lambda c: decision)

    resolved, _, _ = await _run(
        hook,
        extracted=extracted,
        existing=[milan],
        contradicted=[0],
        expiry_hook=RecordingExpiryHook(EdgeExpiryDecision.WORLD_END),
    )

    assert hook.reasons() == [Reason.NEWER_CANDIDATE]
    assert hook.contexts[0].cause_edge is milan and hook.contexts[0].target_is_new is True
    assert resolved.expired_at == expired
    assert resolved.invalid_at == TRIP_END


# --- 7. OWN_END -----------------------------------------------------------------------------


async def test_own_end_asks_expiry_hook_first_then_keep_leaves_edge_live():
    """Fails if OWN_END skips the expiry hook, ignores KEEP, or mislabels target/cause."""
    stored = _edge('Alice was in Rome', invalid_at=TRIP_END, episodes=['first'])
    extracted = _edge('Alice visited')
    expiry = RecordingExpiryHook()
    hook = PolicyHook(lambda c: KEEP)

    resolved, _, _ = await _run(
        hook, extracted=extracted, related=[stored], duplicate=[0], expiry_hook=expiry
    )

    assert resolved is stored
    assert len(expiry.contexts) == 1
    assert hook.reasons() == [Reason.OWN_END]
    ctx = hook.contexts[0]
    assert ctx.target_edge is stored and ctx.cause_edge is extracted
    assert ctx.target_is_new is False
    assert stored.expired_at is None and stored.invalid_at == TRIP_END


async def test_own_end_world_end_on_valid_window_does_not_ask_invalidation_hook():
    """Fails if the invalidation hook is consulted for an end the expiry hook called world-time."""
    stored = _edge('Alice was in Rome', invalid_at=TRIP_END, episodes=['first'])
    expiry = RecordingExpiryHook(EdgeExpiryDecision.WORLD_END)
    hook = PolicyHook(lambda c: KEEP)

    await _run(
        hook, extracted=_edge('Alice visited'), related=[stored], duplicate=[0], expiry_hook=expiry
    )

    assert len(expiry.contexts) == 1
    assert hook.contexts == []
    assert stored.expired_at is None and stored.invalid_at == TRIP_END


async def test_own_end_invalidate_calls_expiry_hook_as_without_hook():
    base_expiry = RecordingExpiryHook()
    base_stored = _edge('Alice was in Rome', invalid_at=TRIP_END, episodes=['first'])
    await _run(
        None, extracted=_edge('Alice visited'), related=[base_stored], duplicate=[0],
        expiry_hook=base_expiry,
    )  # fmt: skip

    expiry = RecordingExpiryHook()
    stored = _edge('Alice was in Rome', invalid_at=TRIP_END, episodes=['first'])
    await _run(
        PolicyHook(), extracted=_edge('Alice visited'), related=[stored], duplicate=[0],
        expiry_hook=expiry,
    )  # fmt: skip

    assert len(expiry.contexts) == len(base_expiry.contexts) == 1
    assert expiry.contexts[0].is_duplicate is True
    assert stored.expired_at == base_stored.expired_at == FIXED_NOW


async def test_own_end_keep_still_runs_newer_candidate_path():
    stored = _edge('Alice was in Rome', invalid_at=TRIP_END, episodes=['first'])
    milan = _edge('Milan', valid_at=TRIP_START + _days(2))
    hook = PolicyHook(lambda c: KEEP if c.reason is Reason.OWN_END else INVALIDATE)

    resolved, _, _ = await _run(
        hook, extracted=_edge('Alice visited'), related=[stored], duplicate=[0],
        existing=[milan], contradicted=[0],
    )  # fmt: skip

    assert hook.reasons() == [Reason.OWN_END, Reason.NEWER_CANDIDATE]
    assert resolved is stored
    assert stored.invalid_at == milan.valid_at and stored.expired_at == FIXED_NOW


def _own_end_keep_policy(otherwise):
    return lambda c: KEEP if c.reason is Reason.OWN_END else otherwise


async def test_own_end_keep_never_moves_invalid_at_later_than_own_end():
    """Fails if a kept stored edge lets a candidate starting after its end push invalid_at later."""

    def build():
        stored = _edge(
            'Alice was in Rome', valid_at=TRIP_START, invalid_at=TRIP_END, episodes=['first']
        )
        return stored, _edge('Oslo', valid_at=TRIP_END + _days(5))

    base_stored, base_later = build()
    _, base_invalidated, _ = await _run(
        None, extracted=_edge('Alice visited'), related=[base_stored], duplicate=[0],
        existing=[base_later], contradicted=[0],
    )  # fmt: skip

    stored, later = build()
    hook = PolicyHook(_own_end_keep_policy(INVALIDATE))
    _, invalidated, _ = await _run(
        hook, extracted=_edge('Alice visited'), related=[stored], duplicate=[0],
        existing=[later], contradicted=[0],
    )  # fmt: skip

    assert stored.invalid_at == TRIP_END and stored.expired_at is None
    assert hook.reasons() == [Reason.OWN_END]  # the out-of-window candidate is never asked
    assert later.invalid_at is None and later.expired_at is None
    assert invalidated == [] and base_invalidated == []
    assert stored.invalid_at <= base_stored.invalid_at  # type: ignore[operator]


async def test_own_end_keep_in_window_newer_candidate_can_only_move_invalid_at_earlier():
    """Fails if an in-window NEWER_CANDIDATE INVALIDATE does not truncate a kept edge earlier."""

    def build():
        stored = _edge(
            'Alice was in Rome', valid_at=TRIP_START, invalid_at=TRIP_END, episodes=['first']
        )
        return stored, _edge('Milan', valid_at=TRIP_START + _days(2))

    base_stored, base_mid = build()
    await _run(
        None, extracted=_edge('Alice visited'), related=[base_stored], duplicate=[0],
        existing=[base_mid], contradicted=[0],
    )  # fmt: skip

    stored, mid = build()
    hook = PolicyHook(_own_end_keep_policy(INVALIDATE))
    await _run(
        hook, extracted=_edge('Alice visited'), related=[stored], duplicate=[0],
        existing=[mid], contradicted=[0],
    )  # fmt: skip

    assert hook.reasons() == [Reason.OWN_END, Reason.NEWER_CANDIDATE]
    assert stored.invalid_at == mid.valid_at and stored.expired_at == FIXED_NOW
    assert stored.invalid_at < TRIP_END  # type: ignore[operator]
    assert stored.invalid_at <= base_stored.invalid_at  # type: ignore[operator]


@pytest.mark.parametrize('decision, expired', [(KEEP, None), (INVALIDATE, FIXED_NOW)])
async def test_undated_stored_duplicate_own_end_keep_still_asks_newer_candidate(decision, expired):
    """Fails if OWN_END KEEP on an undated-start stored edge blocks the NEWER_CANDIDATE ask."""
    stored = _edge('Alice was in Rome', valid_at=None, invalid_at=TRIP_END, episodes=['first'])
    mid = _edge('Milan', valid_at=TRIP_END - _days(12))
    hook = PolicyHook(_own_end_keep_policy(decision))

    resolved, _, _ = await _run(
        hook, extracted=_edge('Alice visited'), related=[stored], duplicate=[0],
        existing=[mid], contradicted=[0],
    )  # fmt: skip

    assert resolved is stored
    assert hook.reasons() == [Reason.OWN_END, Reason.NEWER_CANDIDATE]
    assert hook.contexts[1].cause_edge is mid and hook.contexts[1].target_edge is stored
    assert stored.expired_at == expired
    assert stored.invalid_at == TRIP_END


# --- 7b. CONTRADICTION: prefilter, cause, memoization ---------------------------------------


async def test_contradiction_candidate_flipped_by_hook_is_not_invalidated_unasked():
    """Fails if a candidate the predicate skipped at prefilter is ended after its dates change."""
    ended = _edge('Oslo', valid_at=TRIP_START - _days(30), invalid_at=TRIP_START - _days(25))
    live = _edge('Paris', valid_at=TRIP_START - _days(10))

    def policy(context):
        if context.target_edge is live:
            ended.invalid_at = None  # predicate for `ended` flips False -> True after its prefilter
        return INVALIDATE

    hook = PolicyHook(policy)
    _, invalidated, _ = await _run(hook, existing=[live, ended], contradicted=[0, 1])

    # candidates sort by valid_at, so `ended` was prefiltered (False) before `live` was asked
    assert [c.target_edge for c in hook.contexts] == [live]
    assert invalidated == [live]
    assert ended.invalid_at is None and ended.expired_at is None


async def test_contradiction_allowed_candidate_whose_predicate_flips_is_not_invalidated():
    """Fails if an INVALIDATE answer bypasses the predicate re-check in resolve_edge_contradictions."""
    victim = _edge('Paris', valid_at=TRIP_START - _days(10))
    original = victim.valid_at

    def policy(context):
        victim.valid_at = TRIP_START + _days(5)  # now newer than the mention: predicate False
        return INVALIDATE

    hook = PolicyHook(policy)
    _, invalidated, _ = await _run(hook, existing=[victim], contradicted=[0])

    assert len(hook.contexts) == 1
    assert invalidated == []
    assert victim.invalid_at is None and victim.expired_at is None
    assert victim.valid_at != original


async def test_contradiction_only_asked_and_invalidated_when_both_predicate_and_hook_agree():
    """Fails if a hook-KEEP or predicate-False candidate ends up in the invalidated list."""
    allowed = _edge('Paris', valid_at=TRIP_START - _days(10))
    vetoed = _edge('Lyon', valid_at=TRIP_START - _days(20))
    skipped = _edge('Oslo', valid_at=TRIP_START - _days(30), invalid_at=TRIP_START - _days(25))
    hook = PolicyHook(lambda c: KEEP if c.target_edge is vetoed else INVALIDATE)

    _, invalidated, _ = await _run(
        hook, existing=[allowed, vetoed, skipped], contradicted=[0, 1, 2]
    )

    assert invalidated == [allowed]
    assert vetoed.expired_at is None and skipped.expired_at is None
    assert {id(c.target_edge) for c in hook.contexts} == {id(allowed), id(vetoed)}


async def test_contradiction_cause_is_extracted_edge_not_matched_stored_duplicate():
    """Fails if CONTRADICTION names the stored duplicate (not this mention) as the cause."""
    stored = _edge('Alice was in Rome', episodes=['first'], attributes={'source': 'user'})
    extracted = _edge('Alice visited Rome')
    older = _edge('Paris', valid_at=TRIP_START - _days(5))
    hook = PolicyHook()

    resolved, invalidated, _ = await _run(
        hook, extracted=extracted, related=[stored], duplicate=[0],
        existing=[older], contradicted=[0], episode=_episode('episode_other'),
    )  # fmt: skip

    assert resolved is stored and invalidated == [older]
    (ctx,) = hook.contexts
    assert ctx.reason is Reason.CONTRADICTION
    assert ctx.cause_edge is extracted and ctx.cause_edge is not stored
    assert ctx.target_edge is older and ctx.target_is_new is False
    assert ctx.episode is not None and ctx.episode.uuid == 'episode_other'
    assert 'episode_other' in stored.episodes and 'episode_other' not in extracted.episodes


@pytest.mark.parametrize('case', ['contradiction', 'newer'])
async def test_repeated_contradicted_index_asks_hook_once_and_matches_no_hook(case):
    """Fails if a (reason, target, cause) pair is asked twice; 'contradiction' must also equal no-hook (not 'newer', KEEP)."""
    candidate_start = TRIP_START - _days(10) if case == 'contradiction' else TRIP_START + _days(3)
    # contradiction: INVALIDATE ends the candidate; newer: KEEP is asked once, then memoized
    decision = INVALIDATE if case == 'contradiction' else KEEP

    async def go(hook):
        candidate = _edge('Paris', valid_at=candidate_start)
        resolved, invalidated, _ = await _run(hook, existing=[candidate], contradicted=[0, 0])
        return _snapshot(resolved), [_snapshot(e) for e in invalidated], _snapshot(candidate)

    hook = PolicyHook(lambda c: decision)
    with_hook = await go(hook)
    if decision is INVALIDATE:
        assert with_hook == await go(None)
    assert len(hook.contexts) == 1
    assert hook.reasons() == [
        Reason.CONTRADICTION if case == 'contradiction' else Reason.NEWER_CANDIDATE
    ]


async def test_related_edge_contradicted_twice_is_asked_once():
    """Fails if a related edge the LLM lists twice as contradicted is asked more than once."""
    candidate = _edge('Paris', valid_at=TRIP_START - _days(10))
    hook = PolicyHook()

    llm = _llm(None, [0, 0])
    extracted = _edge('Milan', valid_at=TRIP_START)
    _, invalidated, _ = await resolve_extracted_edge(
        llm, extracted, [candidate], [], _episode(), None, **edge_invalidation_kwargs(hook)
    )

    assert len(hook.contexts) == 1
    assert invalidated == [candidate]


# --- 7c. predicate equivalence ----------------------------------------------------------------

_NAIVE_T0 = datetime(2026, 3, 1)
_AWARE_T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    'c_valid, c_invalid, r_valid, r_invalid, expected',
    [
        (_AWARE_T0, None, _AWARE_T0 + _days(5), None, True),
        (_NAIVE_T0, None, _NAIVE_T0 + _days(5), None, True),
        (_NAIVE_T0, None, _AWARE_T0 + _days(5), None, True),
        (None, None, _AWARE_T0 + _days(5), None, False),
        (_AWARE_T0, None, None, None, False),
        (_AWARE_T0, _AWARE_T0 + _days(2), _AWARE_T0 + _days(5), None, False),
        (_AWARE_T0, _NAIVE_T0 + _days(5), _AWARE_T0 + _days(5), None, False),
        (_AWARE_T0 + _days(9), None, _AWARE_T0 + _days(1), _NAIVE_T0 + _days(2), False),
        (_AWARE_T0, None, _NAIVE_T0, None, False),
    ],
)
def test_contradiction_predicate_matches_resolve_edge_contradictions(
    c_valid, c_invalid, r_valid, r_invalid, expected
):
    """Fails if the shared predicate's legacy decision changes for any date shape (expected column)."""
    candidate = _edge('Paris', valid_at=c_valid, invalid_at=c_invalid)
    resolved = _edge('Milan', valid_at=r_valid, invalid_at=r_invalid)

    predicate = edge_ops._contradiction_invalidates(candidate, resolved)
    mutated = edge_ops.resolve_edge_contradictions(resolved, [candidate.model_copy()])

    assert predicate == bool(mutated) == expected


# --- 8. copies of one stored edge across mentions -------------------------------------------


STORED_UUID = 'stored-rome-trip'


def _stored_copy() -> EntityEdge:
    return _edge(
        'Alice traveled to Rome from Mar 27 to Apr 1',
        uuid=STORED_UUID,
        invalid_at=TRIP_END,
        episodes=['episode_0'],
    )


async def _resolve_two_mentions(monkeypatch, hook, *, contradicting_first: bool):
    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    milan = _edge('Alice lives in Milan', uuid='milan', valid_at=MILAN_START)

    async def fake_search(clients, query, group_ids, config, search_filter):
        if search_filter.edge_uuids is not None:
            return SearchResults(edges=[_stored_copy()])
        return SearchResults(edges=[_stored_copy(), milan])

    contradicting = _edge('Alice was in Rome until she moved to Milan')
    restating = _edge('Alice had a Rome trip in late March')
    responses = {
        contradicting.fact: {'duplicate_facts': [0], 'contradicted_facts': [1]},
        restating.fact: {'duplicate_facts': [0], 'contradicted_facts': []},
    }

    async def generate_response(messages, **kwargs):
        text = str(messages)
        return next(resp for fact, resp in responses.items() if fact in text)

    llm = MagicMock()
    llm.generate_response = AsyncMock(side_effect=generate_response)
    monkeypatch.setattr(edge_ops, 'semaphore_gather', immediate_gather)
    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    monkeypatch.setattr(edge_ops, '_extract_edge_timestamps', AsyncMock(return_value=None))
    monkeypatch.setattr(EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))
    monkeypatch.setattr(edge_ops, 'search', fake_search)
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=llm, embedder=MagicMock(), cross_encoder=MagicMock()
    )
    nodes = [
        EntityNode(uuid='source_uuid', name='Alice', group_id=GROUP_ID, labels=['Entity']),
        EntityNode(uuid='target_uuid', name='Rome', group_id=GROUP_ID, labels=['Entity']),
    ]
    mentions = [contradicting, restating] if contradicting_first else [restating, contradicting]
    resolved, invalidated, _ = await resolve_extracted_edges(
        clients,  # type: ignore[arg-type]
        mentions,
        _episode('episode_1'),
        nodes,
        {},
        {},
        **edge_invalidation_kwargs(hook),
    )
    return [e for e in resolved + invalidated if e.uuid == STORED_UUID]


@pytest.mark.parametrize('contradicting_first', [True, False])
async def test_copies_stay_live_when_every_mention_is_vetoed(monkeypatch, contradicting_first):
    hook = PolicyHook(lambda c: KEEP)
    copies = await _resolve_two_mentions(monkeypatch, hook, contradicting_first=contradicting_first)

    assert len(copies) == 2
    assert Reason.NEWER_CANDIDATE in hook.reasons() and Reason.OWN_END in hook.reasons()
    for edge in copies:
        assert (edge.expired_at, edge.invalid_at) == (None, TRIP_END)


@pytest.mark.parametrize('contradicting_first', [True, False])
async def test_allowed_cause_in_one_mention_propagates_expiry_to_all_copies(
    monkeypatch, contradicting_first
):
    """Reconcile contract: the earliest expiry of any copy wins for every copy of the uuid."""
    hook = PolicyHook(lambda c: INVALIDATE if c.reason is Reason.NEWER_CANDIDATE else KEEP)
    copies = await _resolve_two_mentions(monkeypatch, hook, contradicting_first=contradicting_first)

    assert len(copies) == 2
    for edge in copies:
        assert (edge.expired_at, edge.invalid_at) == (FIXED_NOW, MILAN_START)


# --- 9. errors ------------------------------------------------------------------------------


class _RaisingHook:
    async def decide_edge_invalidation(self, context):
        raise RuntimeError('hook failed')


def _own_end_args():
    stored = _edge('Alice was in Rome', invalid_at=TRIP_END)
    return {'extracted': _edge('Alice visited'), 'related': [stored], 'duplicate': [0]}


def _newer_args():
    return {'existing': [_edge('Milan', valid_at=TRIP_START + _days(2))], 'contradicted': [0]}


def _contradiction_args():
    return {'existing': [_edge('Paris', valid_at=TRIP_START - _days(5))], 'contradicted': [0]}


_ERROR_SCENARIOS = [_own_end_args, _newer_args, _contradiction_args]


@pytest.mark.parametrize('build', _ERROR_SCENARIOS)
async def test_hook_exception_propagates(build):
    with pytest.raises(RuntimeError, match='hook failed'):
        await _run(_RaisingHook(), **build())


@pytest.mark.parametrize('build', _ERROR_SCENARIOS)
@pytest.mark.parametrize('bad', [True, 'keep', None])
async def test_non_enum_return_raises_typeerror(build, bad):
    with pytest.raises(TypeError, match='EdgeInvalidationDecision'):
        await _run(PolicyHook(lambda c: bad), **build())


# --- 10. threading --------------------------------------------------------------------------


@pytest.mark.parametrize('with_hook', [True, False])
async def test_resolve_extracted_edges_threads_hook(monkeypatch, with_hook):
    captured: list[dict] = []

    async def fake_resolve_extracted_edge(*args, **kwargs):
        captured.append(kwargs)
        return args[1], [], []

    async def immediate_gather(*aws, max_coroutines=None):
        return [await aw for aw in aws]

    monkeypatch.setattr(edge_ops, 'create_entity_edge_embeddings', AsyncMock(return_value=None))
    monkeypatch.setattr(EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))
    monkeypatch.setattr(edge_ops, 'semaphore_gather', immediate_gather)
    monkeypatch.setattr(edge_ops, 'search', AsyncMock(return_value=SearchResults()))
    monkeypatch.setattr(edge_ops, 'resolve_extracted_edge', fake_resolve_extracted_edge)
    clients = SimpleNamespace(
        driver=MagicMock(), llm_client=MagicMock(), embedder=MagicMock(), cross_encoder=MagicMock()
    )
    nodes = [
        EntityNode(uuid='source_uuid', name='Alice', group_id=GROUP_ID, labels=['Entity']),
        EntityNode(uuid='target_uuid', name='Rome', group_id=GROUP_ID, labels=['Entity']),
    ]
    hook = PolicyHook() if with_hook else None

    await resolve_extracted_edges(
        clients,  # type: ignore[arg-type]
        [_edge('Alice traveled to Rome')],
        _episode(),
        nodes,
        {},
        {},
        **edge_invalidation_kwargs(hook),
    )

    assert len(captured) == 1
    if with_hook:
        assert captured[0].get('edge_invalidation_hook') is hook
    else:
        assert 'edge_invalidation_hook' not in captured[0]


@pytest.mark.parametrize('with_hook', [True, False])
async def test_dedupe_edges_bulk_threads_hook(monkeypatch, with_hook):
    captured: list[dict] = []

    async def fake_embeddings(embedder, edges):
        for edge in edges:
            edge.fact_embedding = [0.1, 0.2, 0.3]

    async def fake_resolve_extracted_edge(*args, **kwargs):
        captured.append(kwargs)
        return args[1], [], []

    monkeypatch.setattr(bulk_utils, 'create_entity_edge_embeddings', fake_embeddings)
    monkeypatch.setattr(bulk_utils, 'resolve_extracted_edge', fake_resolve_extracted_edge)
    clients = SimpleNamespace(llm_client=MagicMock(), embedder=MagicMock())
    hook = PolicyHook() if with_hook else None

    await bulk_utils.dedupe_edges_bulk(
        clients,  # type: ignore[arg-type]
        [[_edge('Alice traveled to Rome')]],
        [(_episode(), [])],
        [],
        {},
        {},
        **edge_invalidation_kwargs(hook),
    )

    assert len(captured) == 1
    if with_hook:
        assert captured[0].get('edge_invalidation_hook') is hook
    else:
        assert 'edge_invalidation_hook' not in captured[0]


def _make_full_graphiti(edge_invalidation_hook=None) -> Graphiti:
    return Graphiti(
        graph_driver=Mock(spec=GraphDriver),
        llm_client=Mock(spec=LLMClient),
        embedder=Mock(spec=EmbedderClient),
        cross_encoder=Mock(spec=CrossEncoderClient),
        edge_invalidation_hook=edge_invalidation_hook,
    )


def test_graphiti_constructor_stores_edge_invalidation_hook():
    hook = PolicyHook()
    assert _make_full_graphiti(hook).edge_invalidation_hook is hook
    assert _make_full_graphiti().edge_invalidation_hook is None
    assert Graphiti.__new__(Graphiti).edge_invalidation_hook is None


async def _run_add_triplet(monkeypatch, graphiti: Graphiti) -> list[dict]:
    source = EntityNode(name='Alice', group_id=GROUP_ID, labels=['Entity'])
    target = EntityNode(name='Rome', group_id=GROUP_ID, labels=['Entity'])
    source.name_embedding = [0.1]
    target.name_embedding = [0.1]
    edge = EntityEdge(
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        name='TRAVELED_TO',
        group_id=GROUP_ID,
        fact='Alice traveled to Rome',
        episodes=[],
        created_at=FIXED_NOW,
        fact_embedding=[0.1],
    )
    captured: list[dict] = []

    async def fake_resolve_nodes(clients, extracted_nodes, *args, **kwargs):
        return [extracted_nodes[0]], {}, []

    async def fake_get_node_by_uuid(driver, node_uuid):
        raise NodeNotFoundError(node_uuid)

    async def fake_get_edge_by_uuid(driver, edge_uuid):
        raise EdgeNotFoundError(edge_uuid)

    async def fake_search(*args, **kwargs):
        result = Mock()
        result.edges = []
        return result

    async def fake_resolve_extracted_edge(*args, **kwargs):
        captured.append({'args': args, 'kwargs': kwargs})
        return edge, [], []

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(graphiti_module.EntityNode, 'get_by_uuid', fake_get_node_by_uuid)
    monkeypatch.setattr(graphiti_module.EntityEdge, 'get_by_uuid', fake_get_edge_by_uuid)
    monkeypatch.setattr(graphiti_module.EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))
    monkeypatch.setattr(graphiti_module, 'search', fake_search)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_edge', fake_resolve_extracted_edge)
    monkeypatch.setattr(graphiti_module, 'create_entity_edge_embeddings', noop)
    monkeypatch.setattr(graphiti_module, 'create_entity_node_embeddings', noop)
    monkeypatch.setattr(graphiti_module, 'add_nodes_and_edges_bulk', noop)
    monkeypatch.setattr(graphiti_module, 'resolve_extracted_nodes', fake_resolve_nodes)

    await graphiti.add_triplet(source, edge, target)
    return captured


async def test_add_triplet_threads_hook(monkeypatch):
    hook = PolicyHook()
    captured = await _run_add_triplet(monkeypatch, _make_full_graphiti(hook))
    assert len(captured) == 1
    assert captured[0]['kwargs'] == {'edge_invalidation_hook': hook}


async def test_add_triplet_no_hook_uses_legacy_signature(monkeypatch):
    captured = await _run_add_triplet(monkeypatch, _make_full_graphiti())
    assert len(captured) == 1
    assert captured[0]['kwargs'] == {}


# --- 11. structural guards ------------------------------------------------------------------

_RESOLVER_CALLS = {'resolve_extracted_edge', 'resolve_extracted_edges', 'dedupe_edges_bulk'}


def _resolver_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, 'attr', None)
            if name in _RESOLVER_CALLS:
                calls.append(node)
    return calls


def _names_hook(arg: ast.expr, hook_name: str) -> bool:
    """The argument is ``<hook_name>`` or ``<obj>.<hook_name>``, never a literal."""
    if isinstance(arg, ast.Name):
        return arg.id == hook_name
    return isinstance(arg, ast.Attribute) and arg.attr == hook_name


def _threads(call: ast.Call, helper: str, hook_name: str) -> bool:
    return any(
        kw.arg is None
        and isinstance(kw.value, ast.Call)
        and isinstance(kw.value.func, ast.Name)
        and kw.value.func.id == helper
        and len(kw.value.args) == 1
        and _names_hook(kw.value.args[0], hook_name)
        for kw in call.keywords
    )


def test_structural_guard_rejects_wrong_hook_arguments():
    for source in (
        'f(**edge_invalidation_kwargs(None))',
        'f(**edge_invalidation_kwargs(self.edge_expiry_hook))',
        'f(**edge_invalidation_kwargs())',
        'f(**edge_expiry_kwargs(edge_invalidation_hook))',
    ):
        call = ast.parse(source, mode='eval').body
        assert isinstance(call, ast.Call)
        assert not _threads(call, 'edge_invalidation_kwargs', 'edge_invalidation_hook')
    for source in (
        'f(**edge_invalidation_kwargs(edge_invalidation_hook))',
        'f(**edge_invalidation_kwargs(self.edge_invalidation_hook))',
    ):
        call = ast.parse(source, mode='eval').body
        assert isinstance(call, ast.Call)
        assert _threads(call, 'edge_invalidation_kwargs', 'edge_invalidation_hook')


def test_every_resolver_call_in_graphiti_core_threads_the_invalidation_hook():
    package = Path(__file__).resolve().parents[1] / 'graphiti_core'
    found = invalidation_threaded = expiry_threaded = 0
    for path in package.rglob('*.py'):
        for call in _resolver_calls(path):
            found += 1
            assert _threads(call, 'edge_invalidation_kwargs', 'edge_invalidation_hook'), (
                f'{path.name}:{call.lineno} does not thread edge_invalidation_hook'
            )
            invalidation_threaded += 1
            expiry_threaded += _threads(call, 'edge_expiry_kwargs', 'edge_expiry_hook')
    assert found > 0
    assert invalidation_threaded == expiry_threaded == found


def test_no_menhir_imports_in_edge_invalidation_mechanism():
    path = Path(__file__).resolve().parents[1] / 'graphiti_core' / 'edge_invalidation.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.lower().startswith('menhir')
        elif isinstance(node, ast.Import):
            assert all(not alias.name.lower().startswith('menhir') for alias in node.names)


# --- 12. concurrency ------------------------------------------------------------------------


async def test_concurrent_resolutions_use_their_own_episode():
    recorded: list[tuple[str, str]] = []

    class SlowHook:
        async def decide_edge_invalidation(self, context):
            await asyncio.sleep(0)  # force interleaving between the two resolutions
            recorded.append((context.episode.uuid, context.target_edge.uuid))  # type: ignore[union-attr]
            await asyncio.sleep(0)
            return _tier_policy(context)

    hook = SlowHook()

    def build():
        stored = _edge('Rome', valid_at=TRIP_START - _days(10), attributes={'source': 'user'})
        return stored, _edge('Alice lives in Milan', valid_at=TRIP_START)

    stored_a, new_a = build()
    stored_b, new_b = build()
    results = await asyncio.gather(
        _run(
            hook, extracted=new_a, existing=[stored_a], contradicted=[0],
            episode=_episode('episode_a', 'agent-writer'),
        ),
        _run(
            hook, extracted=new_b, existing=[stored_b], contradicted=[0],
            episode=_episode('episode_b', 'user-chat'),
        ),
    )  # fmt: skip

    assert sorted(recorded) == sorted([('episode_a', stored_a.uuid), ('episode_b', stored_b.uuid)])
    assert results[0][1] == []
    assert stored_a.invalid_at is None and stored_a.expired_at is None
    assert results[1][1] == [stored_b]
    assert stored_b.expired_at == FIXED_NOW


# --- 13. shared-object interleavings (bulk pass 1) and inverted windows ----------------------

N1_VALID = datetime(2026, 1, 1, tzinfo=timezone.utc)
N1_C1_VALID = datetime(2026, 3, 20, tzinfo=timezone.utc)
N1_EARLY = datetime(2026, 3, 10, tzinfo=timezone.utc)
N1_LATE = datetime(2026, 4, 1, tzinfo=timezone.utc)
SENTINEL = datetime(2026, 5, 5, 5, 5, tzinfo=timezone.utc)


class SuspendingHook:
    """Always INVALIDATE; suspends several loop ticks on ``suspend_on`` and logs ordering."""

    def __init__(self, suspend_on: EdgeInvalidationReason, watched: EntityEdge | None = None):
        self.suspend_on = suspend_on
        self.watched = watched
        self.log: list[str] = []
        self.contexts: list[EdgeInvalidationContext] = []
        self.invalid_at_on_resume: datetime | None = None
        self.expired_at_on_resume: datetime | None = None

    async def decide_edge_invalidation(self, context: EdgeInvalidationContext):
        self.contexts.append(context)
        if context.reason is self.suspend_on:
            self.log.append(f'{context.reason.name}:suspend')
            for _ in range(3):
                await asyncio.sleep(0)
            if self.watched is not None:
                self.invalid_at_on_resume = self.watched.invalid_at
                self.expired_at_on_resume = self.watched.expired_at
            self.log.append(f'{context.reason.name}:resume')
        else:
            self.log.append(f'{context.reason.name}:answer')
        return INVALIDATE


def _n1_calls(hook, call2_valid_at: datetime):
    """Two resolutions sharing EA: call 1 resolves to EA (NEWER_CANDIDATE C1); call 2 contradicts EA."""
    ea = _edge('Alice lives in Rome', valid_at=N1_VALID)
    c1 = _edge('Alice lives in Oslo', valid_at=N1_C1_VALID)
    x1 = _edge('Alice lives in Rome (again)', valid_at=N1_VALID)
    x2 = _edge('Alice lives in Milan', valid_at=call2_valid_at)
    call1 = _run(hook, extracted=x1, related=[ea], existing=[c1], duplicate=[0], contradicted=[0])
    call2 = _run(hook, extracted=x2, existing=[ea], contradicted=[0])
    return ea, call1, call2


@pytest.mark.parametrize(
    ('call2_valid_at', 'expected'),
    [(N1_EARLY, N1_EARLY), (N1_LATE, N1_C1_VALID)],
    ids=['call2-truncates-earlier', 'call2-truncates-later'],
)
async def test_shared_edge_truncated_during_newer_candidate_await_keeps_earlier_end(
    call2_valid_at, expected
):
    """Fails if path 2 overwrites a concurrent truncation with a LATER invalid_at after its await."""
    hook = SuspendingHook(Reason.NEWER_CANDIDATE)
    ea, call1, call2 = _n1_calls(hook, call2_valid_at)
    hook.watched = ea

    await asyncio.gather(call1, call2)

    # Real interleaving: call 2 asked CONTRADICTION while call 1 was suspended on NEWER_CANDIDATE.
    assert hook.log == [
        'NEWER_CANDIDATE:suspend',
        'CONTRADICTION:answer',
        'NEWER_CANDIDATE:resume',
    ]
    # ...and its truncation had already landed when call 1 resumed.
    assert hook.expired_at_on_resume == FIXED_NOW
    assert hook.invalid_at_on_resume == call2_valid_at
    assert ea.invalid_at == expected
    assert ea.expired_at == FIXED_NOW


@pytest.mark.parametrize('call2_valid_at', [N1_EARLY, N1_LATE], ids=['early', 'late'])
async def test_shared_edge_interleaved_end_is_never_later_than_any_no_hook_order(call2_valid_at):
    """Fails if the hooked interleaving ends the shared edge later than a sequential no-hook run."""
    hook = SuspendingHook(Reason.NEWER_CANDIDATE)
    ea, call1, call2 = _n1_calls(hook, call2_valid_at)
    await asyncio.gather(call1, call2)
    hooked_end = ea.invalid_at

    for order in (False, True):
        ea_plain, plain1, plain2 = _n1_calls(None, call2_valid_at)
        await asyncio.gather(*((plain2, plain1) if order else (plain1, plain2)))
        assert ea_plain.invalid_at is not None and hooked_end is not None
        assert hooked_end <= ea_plain.invalid_at


async def test_undated_branch_keeps_expiry_written_during_await():
    """Fails if the undated NEWER_CANDIDATE branch restamps expired_at after a concurrent expiry."""
    resolved = _edge('Alice lives in Rome', valid_at=None, invalid_at=N1_LATE)
    candidate = _edge('Alice lives in Oslo', valid_at=N1_C1_VALID)
    hook = SuspendingHook(Reason.NEWER_CANDIDATE)

    async def concurrent_expiry():
        hook.log.append('other:expire')
        resolved.expired_at = SENTINEL

    await asyncio.gather(
        _run(
            hook,
            extracted=resolved,
            existing=[candidate],
            contradicted=[0],
            expiry_hook=RecordingExpiryHook(EdgeExpiryDecision.WORLD_END),
        ),
        concurrent_expiry(),
    )

    assert hook.log == ['NEWER_CANDIDATE:suspend', 'other:expire', 'NEWER_CANDIDATE:resume']
    assert resolved.expired_at == SENTINEL
    assert resolved.invalid_at == N1_LATE


async def test_own_end_keeps_expiry_written_during_veto_await():
    """Fails if the OWN_END expiry write restamps expired_at over one a concurrent call wrote."""
    stored = _edge('Alice was in Rome', valid_at=TRIP_START, invalid_at=TRIP_END)
    hook = SuspendingHook(Reason.OWN_END)

    async def concurrent_expiry():
        hook.log.append('other:expire')
        stored.expired_at = SENTINEL

    await asyncio.gather(
        _run(hook, extracted=_edge('Alice visited Rome'), related=[stored], duplicate=[0]),
        concurrent_expiry(),
    )

    assert hook.log == ['OWN_END:suspend', 'other:expire', 'OWN_END:resume']
    assert stored.expired_at == SENTINEL


@pytest.mark.parametrize('window', ['inverted', 'equal'])
@pytest.mark.parametrize(
    'expiry_decision',
    [None, EdgeExpiryDecision.WORLD_END, EdgeExpiryDecision.EXPIRE],
    ids=['no-expiry-hook', 'world-end', 'expire'],
)
async def test_inverted_window_on_stored_duplicate_is_expired_without_own_end_ask(
    expiry_decision, window
):
    """Fails if OWN_END is asked (and a KEEP veto can keep alive) a malformed end <= start window."""
    end = TRIP_START if window == 'equal' else TRIP_START - _days(5)
    stored = _edge('Alice was in Rome', valid_at=TRIP_START, invalid_at=end)
    hook = PolicyHook(lambda c: KEEP)
    expiry_hook = None if expiry_decision is None else RecordingExpiryHook(expiry_decision)

    await _run(
        hook,
        extracted=_edge('Alice visited Rome'),
        related=[stored],
        duplicate=[0],
        expiry_hook=expiry_hook,
    )

    assert hook.contexts == []
    assert stored.expired_at == FIXED_NOW
