"""AnswerAgent: question in, cited answer card out.

1. Re-gate the conversation so far (a person's own thread, or a channel thread for a channel principal).
2. Route the question to an intent and match its subjects.
3. Recall memory (private surfaces only; counts toward the tool budget).
4. Run the bounded loop, compose claims, validate them, build the card.
5. Write one audit row (``kind='answer'``), the episode and the conversation turns.

Persistence failures are logged and never cost the caller the answer.
"""

import logging
import threading
import time
import uuid

from agents.answer.card import AnswerCard, build_card
from agents.answer.composer import Composer
from agents.answer.intents import ALLOWED, Intent, Route, Router
from agents.answer.loop import Planner, RunState, ToolCall, execute, run_loop
from agents.answer.planner import playbook_for
from agents.answer.subjects import match_subjects, subjects_for
from agents.answer.validator import validate
from agents.gate.principal import Kind, Principal
from agents.memory import recall as gated
from agents.memory.store import ConversationNotFound, Episode, new_id
from agents.runtime import Runtime
from agents.tools.common import visible_node_ids

log = logging.getLogger("weave.answer")

HISTORY_TURNS = 10
TITLE_CHARS = 80

__all__ = ["AnswerAgent", "ConversationNotFound", "build_agent"]


class AnswerAgent:
    def __init__(self, runtime: Runtime, *, router: Router, planner: Planner, composer: Composer,
                 agent: str = "answer"):
        self.runtime = runtime
        self.router = router
        self.planner = planner
        self.composer = composer
        self.agent = agent

    def answer(self, principal: Principal, question: str, **kwargs) -> AnswerCard:
        """Answer ``question`` as ``principal``, on the principal company's own model key when it saved one."""

        from agents.answer.llm import using_tenant

        with using_tenant(principal.tenant):
            return self._answer(principal, question, **kwargs)

    def _answer(self, principal: Principal, question: str, *, conversation_id: str | None = None,
                request_id: str | None = None, intent: Intent | str | None = None,
                subject_ids: list[str] | None = None, fact_ids: list[str] | None = None,
                ensure_conversation: bool = False, persist: bool = True) -> AnswerCard:
        """Answer ``question`` as ``principal``.

        Internal callers (the watcher handoff) may fix the ``intent`` (the
        router is skipped), name the ``subject_ids`` instead of matching them
        from the question, and hand in ``fact_ids`` a draft states first. Ids
        are gated as the principal; the ones they may not see are dropped.
        """

        started = time.monotonic()
        request_id = request_id or uuid.uuid4().hex[:16]
        cfg = self.runtime.cfg.answer
        episode_id = new_id("ep")
        ctx = self.runtime.context(principal, agent=self.agent, episode_id=episode_id)
        private = gated.namespace(ctx) is not None and ctx.memory is not None

        channel = principal.channel_group if principal.kind is Kind.CHANNEL else None
        shared = channel is not None and ctx.memory is not None
        if conversation_id and private:
            missing = ensure_conversation and ctx.memory.get_conversation(conversation_id) is None
            history = [] if missing else gated.history(ctx, conversation_id, HISTORY_TURNS)
        elif conversation_id and shared:
            history = gated.shared_history(ctx, conversation_id, channel, HISTORY_TURNS)
        else:
            history = []
        route = Route(Intent(intent), "given") if intent else self.router.route(question)
        state = RunState(question, route.intent, principal, history=history)
        state.subjects = subjects_for(ctx, subject_ids) if subject_ids else []
        if not state.subjects:
            state.subjects = match_subjects(ctx, question)
        for turn in reversed(history):
            if state.subjects:
                break
            if turn.role == "user":
                state.subjects = match_subjects(ctx, turn.text)
        if fact_ids:
            visible = visible_node_ids(ctx, fact_ids)
            state.seed_facts = [i for i in dict.fromkeys(fact_ids) if i in visible]
        state.known_ids.update(m.id for m in state.subjects)
        state.known_ids.update(state.seed_facts)
        state.known_ids.update(i for t in history for i in t.node_ids)
        state.playbook = playbook_for(ctx.memory, principal, route.intent)

        if private and cfg.recall_limit > 0:
            execute(state, ctx, ToolCall("recall_memory", {"query": question, "limit": cfg.recall_limit}), None)
        run_loop(state, ctx, self.planner, allowed=ALLOWED[route.intent], max_calls=cfg.max_tool_calls,
                 cache=self.runtime.cache)
        try:
            claims = self.composer.compose(state)
        except Exception:
            log.exception("composer failed (request %s)", request_id)
            claims = []
        claims, dropped = validate(claims, state, cfg.citation_policy)
        card = build_card(state, claims, dropped, self.runtime.cfg)
        card.request_id = request_id
        outcome = "ok" if claims or card.proposal else "empty"
        if not persist and (private or shared) and not conversation_id:
            conversation_id = new_id("conv")

        def _save() -> None:
            if private:
                card.conversation_id, card.episode_id = self._remember(
                    ctx, state, card, conversation_id, episode_id, outcome,
                )
            elif shared:
                card.conversation_id, card.episode_id = self._remember_channel(
                    ctx, state, card, conversation_id, episode_id, outcome, channel,
                )
            self._audit(principal, state, card, outcome, request_id, started)

        if persist:
            _save()
            return card
        if private or shared:
            card.episode_id = episode_id
            card.conversation_id = conversation_id
        frozen = card.public()
        card.public = lambda: frozen
        self._persist_thread = threading.Thread(target=_save, name="answer-persist", daemon=True)
        self._persist_thread.start()
        return card

    def _remember(self, ctx, state: RunState, card: AnswerCard, conversation_id, episode_id, outcome):
        memory, ns = ctx.memory, gated.namespace(ctx)
        try:
            conversation_id = _open_conversation(
                memory, conversation_id, ns.tenant, ns.person_id, str(ctx.principal.surface),
                state.question[:TITLE_CHARS],
            )
            if conversation_id is None:
                return None, None
            _store_episode(memory, conversation_id, ns.tenant, ns.person_id, ns.agent, "person", state, card,
                           episode_id, outcome)
        except Exception:
            log.warning("memory write failed", exc_info=True)
            return conversation_id, None
        return conversation_id, episode_id

    def _remember_channel(self, ctx, state: RunState, card: AnswerCard, conversation_id, episode_id, outcome,
                          audience: str):
        """Store the answer against the channel, not the person who asked."""

        memory = ctx.memory
        try:
            conversation_id = _open_conversation(
                memory, conversation_id, ctx.principal.tenant, audience, str(ctx.principal.surface),
                state.question[:TITLE_CHARS],
            )
            if conversation_id is None:
                return None, None
            _store_episode(memory, conversation_id, ctx.principal.tenant, audience, self.agent, audience, state,
                           card, episode_id, outcome)
        except Exception:
            log.warning("memory write failed", exc_info=True)
            return conversation_id, None
        return conversation_id, episode_id

    def _audit(self, principal, state: RunState, card: AnswerCard, outcome, request_id, started) -> None:
        audit = self.runtime.audit
        if audit is None:
            return
        try:
            audit.record_answer(
                request_id=request_id, principal=principal, question=state.question, intent=card.intent,
                tools=[s.tool for s in state.trace if s.status != "rejected"],
                node_ids=[i for s in state.trace for i in s.node_ids], hidden=state.hidden, outcome=outcome,
                ms=int((time.monotonic() - started) * 1000), conversation_id=card.conversation_id,
            )
        except Exception:
            log.warning("answer audit write failed (request %s)", request_id, exc_info=True)


def _open_conversation(memory, conversation_id, tenant, person_id, surface, title) -> str | None:
    if conversation_id:
        existing = memory.get_conversation(conversation_id)
        if existing is not None:
            if (existing.tenant, existing.person_id) != (tenant, person_id):
                return None
            return conversation_id
    return memory.create_conversation(tenant, person_id, surface, title, conversation_id=conversation_id)


def _store_episode(memory, conversation_id, tenant, person_id, agent, audience, state, card, episode_id, outcome):
    memory.add_turn(conversation_id, "user", state.question, [])
    memory.add_turn(conversation_id, "agent", card.answer, card.node_ids)
    memory.add_episode(Episode(
        tenant, person_id, agent, question=state.question, node_ids=card.node_ids,
        intent=card.intent, tools=card.trace, outcome=outcome, conversation_id=conversation_id,
        audience=audience, id=episode_id,
    ))


def build_agent(runtime: Runtime, llm=None, agent: str = "answer") -> AnswerAgent:
    """The agent with the components ``answer.llm.provider`` selects (rule-based for ``none``)."""

    from agents.answer.llm import build_components

    router, planner, composer = build_components(runtime.cfg, llm)
    return AnswerAgent(runtime, router=router, planner=planner, composer=composer, agent=agent)
