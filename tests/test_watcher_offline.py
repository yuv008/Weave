"""The impact watcher offline: notice wording, per-recipient filtering, idempotent tasks, handoff and sender.

The graph is a stub, ``impact_of`` returns a scripted per-recipient view, and
visibility is a per-person set, so these tests check the watcher's own logic.
The real gate runs in ``tests/test_watcher_live.py``.
"""

from types import SimpleNamespace

import pytest

from agents.answer.subjects import Match, normalize
from agents.memory.store import Task
from agents.tools.base import ToolResult
from agents.watcher import handoff as handoff_mod
from agents.watcher import sender as sender_mod
from agents.watcher import watcher as watcher_mod
from agents.watcher.outbox import ChangeEvent, MemoryOutbox
from agents.watcher.sender import DRAFT_POINTER, DryRunTransport, SlackTransport, run_sends, transport_for
from agents.watcher.watcher import ImpactWatcher, compose_notice, evidence_ids, handoff_id, notice_id
from agents.watcher.worker import STEPS, parse_steps, tick
from shared.schema import Label, Prop, Rel
from tests.answer_support import SubjectGraph, offline_runtime

HANDOFF_KEYS = {"event_id", "change", "recipient", "commitment_ids", "customer_ids", "deal_ids"}


class WatchGraph(SubjectGraph):
    """Also answers the watcher's customer and deal lookups for the Acme commitment."""

    def __init__(self, fx):
        super().__init__(fx)
        self.customer, self.deal = fx.ids["customer_acme"], fx.ids["deal_acme"]

    def ro_query(self, query, params=None):
        if f"[:{Rel.PROMISED_TO}]" in query:
            self.queries.append(query)
            return SimpleNamespace(result_set=[[self.customer]])
        if f"[:{Rel.FOR}]" in query:
            self.queries.append(query)
            return SimpleNamespace(result_set=[[self.deal]])
        return super().ro_query(query, params)


def commitment(fx, owner):
    return {"id": fx.ids["commitment_acme_sso"], "label": Label.COMMITMENT,
            "name": "SSO will be live for Acme by Nov 15.", "status": "active", "progress": "open",
            "due": "2026-11-15", "owners": [{"id": owner, "name": "Priya", "dept": "Sales"}], "dept": "Sales"}


@pytest.fixture()
def env(monkeypatch):
    env = offline_runtime()
    fx = env.fx
    env.graph._graph = WatchGraph(fx)
    env.outbox = MemoryOutbox.from_fixture(fx)
    [env.event] = env.outbox.pending("acme")
    env.ids = SimpleNamespace(
        d1=fx.ids["decision_provider_a"], d2=fx.ids["decision_provider_b"], c1=fx.ids["commitment_acme_sso"],
        acme=fx.ids["customer_acme"], deal=fx.ids["deal_acme"], priya=fx.ids["priya"], sam=fx.ids["sam"],
        dev=fx.ids["dev"],
    )
    ids = env.ids
    env.recipients = [ids.priya, ids.sam]
    env.visible = {
        ids.priya: {ids.c1, ids.d2, ids.acme, ids.deal},
        ids.sam: {ids.d1, ids.d2},
    }
    env.views = {
        ids.priya: {"root": {"id": ids.d1, "label": Label.DECISION, "restricted": True,
                             "replaced_by": [{"id": ids.d2, "statement": "Use Provider B for SSO."}]},
                    "affected": [commitment(fx, ids.priya)]},
        ids.sam: {"root": {"id": ids.d1, "label": Label.DECISION, "name": "Use Provider A for SSO.",
                           "replaced_by": [{"id": ids.d2, "statement": "Use Provider B for SSO."}]},
                  "affected": [commitment(fx, ids.priya)]},
    }
    env.calls = []

    def fake_invoke(name, ctx, args, cache=None):
        who = ctx.principal.user_id
        env.calls.append((name, who, args))
        view = env.views[who]
        node_ids = [view["root"]["id"], *(n["id"] for n in view["affected"])]
        return ToolResult(name, view, node_ids=node_ids, hidden=1)

    def visible(ctx, node_ids):
        return {i for i in node_ids if i in env.visible.get(ctx.principal.user_id, set())}

    monkeypatch.setattr(watcher_mod, "affected_owners", lambda graph, fact_id: list(env.recipients))
    monkeypatch.setattr(watcher_mod, "invoke", fake_invoke)
    monkeypatch.setattr(watcher_mod, "visible_node_ids", visible)
    monkeypatch.setattr(sender_mod, "visible_node_ids", visible)
    monkeypatch.setattr(handoff_mod, "visible_node_ids", visible)
    names = {n.id: n for n in fx.nodes}

    def subjects(ctx, node_ids):
        shown = visible(ctx, node_ids)
        return [Match(i, names[i].props[Prop.NAME], names[i].label, normalize(names[i].props[Prop.NAME]))
                for i in node_ids if i in shown and Prop.NAME in names[i].props]

    monkeypatch.setattr(handoff_mod, "subjects_for", subjects)
    return env


class StubAgent:
    """Stands in for the answer agent: records the call and writes a proposal as the caller."""

    agent = "answer"

    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []

    def answer(self, principal, question, *, intent=None, subject_ids=None, fact_ids=None, **_):
        self.calls.append({"who": principal.user_id, "question": question, "intent": str(intent),
                           "subject_ids": subject_ids, "fact_ids": fact_ids})
        task = Task(principal.tenant, "proposal_email", "proposed", node_ids=list(fact_ids),
                    payload={"draft": "- SSO will be live for Acme by Nov 15.\n- Use Provider B for SSO."},
                    created_by=principal.user_id, episode_id="ep_stub")
        self.runtime.memory.add_task(task)
        return SimpleNamespace(proposal={"tool": "draft_email", "proposal_id": task.id}, episode_id="ep_stub")


def tasks(env, kind=None):
    return [t for t in env.runtime.memory.tasks.values() if kind is None or t.kind == kind]


def test_notice_wording_follows_the_change_kind():
    root = {"id": "d", "label": Label.DECISION, "restricted": True}
    assert compose_notice("superseded", root, [], []) == "A decision your work depends on was replaced."
    assert compose_notice("retracted", root, [], []) == "A decision your work depends on was withdrawn."
    assert compose_notice("contradicts", root, [], []).endswith("is contradicted by newer information.")
    figure = {"id": "f", "label": Label.FIGURE, "name": "Q4 budget"}
    assert compose_notice("superseded", figure, [], []) == 'The figure "Q4 budget", which your work depends on, was replaced.'


def test_notice_states_only_what_the_recipient_was_shown():
    owned = [{"id": "c1", "label": Label.COMMITMENT, "name": "SSO will be live for Acme by Nov 15.", "due": "2026-11-15"}]
    newer = [{"id": "d2", "statement": "Use Provider B for SSO."}]
    hidden_root = {"id": "d1", "label": Label.DECISION, "restricted": True}
    text = compose_notice("superseded", hidden_root, owned, newer)
    assert text.splitlines() == [
        "A decision your work depends on was replaced.",
        'Your commitment "SSO will be live for Acme by Nov 15." (due 2026-11-15) is at risk.',
        'Now in effect: "Use Provider B for SSO."',
    ]
    assert evidence_ids(hidden_root, owned, newer) == ["c1", "d2"]
    summarized = {**hidden_root, "summary": "Engineering switched providers.", "projection_id": "p2"}
    assert "What changed: Engineering switched providers." in compose_notice("superseded", summarized, owned, [])
    assert evidence_ids(summarized, owned, []) == ["c1", "p2"]
    projected = [{"id": "d2", "restricted": True, "summary": "Provider switch summary", "projection_id": "p2"}]
    assert compose_notice("superseded", hidden_root, owned, projected).endswith("Now in effect: Provider switch summary")
    assert evidence_ids(hidden_root, owned, projected) == ["c1", "p2"]


def test_only_the_owner_is_notified_from_her_own_view(env):
    ids = env.ids
    [outcome] = ImpactWatcher(env.runtime, env.outbox).drain("acme", 10)
    assert outcome.notified == [ids.priya] and outcome.filtered == 1 and not outcome.error
    assert [(name, who) for name, who, _ in env.calls] == [("impact_of", ids.priya), ("impact_of", ids.sam)]
    assert env.calls[0][2] == {"target": f"{ids.d1}:superseded"}

    notice = env.runtime.memory.get_task(notice_id(env.event.id, ids.priya))
    assert (notice.kind, notice.state, notice.target_agent) == ("slack_dm", "ready", "slack_sender")
    text = notice.payload["text"]
    assert "SSO will be live for Acme by Nov 15" in text and "Provider B" in text and "Provider A" not in text
    assert notice.payload["evidence_ids"] == notice.node_ids == [ids.c1, ids.d2]
    assert set(notice.payload["evidence_ids"]) <= env.visible[ids.priya]
    assert notice.payload["handoff_id"] == handoff_id(env.event.id, ids.priya)
    assert notice.payload["from"] == "impact_watcher" and notice.payload["recipient"] == ids.priya
    assert not [t for t in tasks(env) if t.payload.get("recipient") == ids.sam]
    assert env.outbox.pending("acme") == []


def test_handoff_payload_holds_ids_only(env):
    ids = env.ids
    ImpactWatcher(env.runtime, env.outbox).drain("acme", 10)
    handoff = env.runtime.memory.get_task(handoff_id(env.event.id, ids.priya))
    assert (handoff.kind, handoff.state, handoff.target_agent) == ("draft_customer_update", "ready", "answer")
    assert set(handoff.payload) == HANDOFF_KEYS
    assert handoff.payload["commitment_ids"] == [ids.c1]
    assert handoff.payload["customer_ids"] == [ids.acme] and handoff.payload["deal_ids"] == [ids.deal]
    assert handoff.payload["change"] == f"{ids.d1}:superseded"
    values = [v for v in handoff.payload.values()]
    assert all(isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v)) for v in values)
    assert "SSO will be live" not in str(handoff.payload) and "Provider" not in str(handoff.payload)


def test_audit_has_one_row_per_recipient_and_names_nothing_for_the_filtered(env):
    ImpactWatcher(env.runtime, env.outbox).drain("acme", 10)
    rows = [r for r in env.runtime.audit.rows if r["kind"] == "watcher"]
    by_who = {r["principal"].user_id: r for r in rows}
    assert len(rows) == 2 and set(by_who) == {env.ids.priya, env.ids.sam}
    priya, sam = by_who[env.ids.priya], by_who[env.ids.sam]
    assert priya["outcome"] == "notified" and priya["tools"] == ["impact_of"] and priya["node_ids"]
    assert sorted(priya["task_ids"]) == sorted([notice_id(env.event.id, env.ids.priya),
                                                handoff_id(env.event.id, env.ids.priya)])
    assert sam["outcome"] == "filtered" and sam["node_ids"] == [] and sam["task_ids"] == [] and sam["seen"] == 2


def test_a_recipient_who_sees_nothing_gets_nothing(env):
    env.recipients = [env.ids.sam, "acme:person:nobody"]
    [outcome] = ImpactWatcher(env.runtime, env.outbox).drain("acme", 10)
    assert outcome.notified == [] and outcome.filtered == 2 and tasks(env) == []
    assert env.outbox.pending("acme") == []


def test_rerunning_an_event_writes_nothing_new(env):
    watcher = ImpactWatcher(env.runtime, env.outbox)
    [first] = watcher.drain("acme", 10)
    assert len(first.written) == 2 and first.existing == []
    assert watcher.drain("acme", 10) == []
    before = {t.id: t.payload for t in tasks(env)}
    env.outbox.add(env.event)
    [again] = watcher.drain("acme", 10)
    assert again.written == [] and sorted(again.existing) == sorted(first.written)
    assert {t.id: t.payload for t in tasks(env)} == before


def test_a_failing_event_stays_pending_and_is_audited(env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("graph down")

    monkeypatch.setattr(watcher_mod, "invoke", boom)
    [outcome] = ImpactWatcher(env.runtime, env.outbox).drain("acme", 10)
    assert outcome.error and outcome.notified == []
    assert [e.id for e in env.outbox.pending("acme")] == [env.event.id]
    assert [r["outcome"] for r in env.runtime.audit.rows if r["kind"] == "watcher"] == ["error"]


def test_drain_skips_a_failed_event_and_takes_the_next(env, monkeypatch):
    other = ChangeEvent("acme:figure:x:superseded", "acme", "acme:figure:x", "superseded", None, "2026-10-13T00:00:00Z")
    env.outbox.add(other)
    real = watcher_mod.invoke

    def flaky(name, ctx, args, cache=None):
        if args["target"].startswith(env.ids.d1):
            raise RuntimeError("boom")
        return real(name, ctx, args, cache)

    monkeypatch.setattr(watcher_mod, "invoke", flaky)
    outcomes = ImpactWatcher(env.runtime, env.outbox).drain("acme", 10)
    assert [(o.event_id, o.error) for o in outcomes] == [(env.event.id, True), (other.id, False)]
    assert [e.id for e in env.outbox.pending("acme")] == [env.event.id]


def test_handoff_drafts_as_the_recipient(env):
    ImpactWatcher(env.runtime, env.outbox).drain("acme", 10)
    agent = StubAgent(env.runtime)
    [done] = handoff_mod.run_handoffs(env.runtime, agent, "acme", "w1")
    ids = env.ids
    assert done["state"] == "done" and done["recipient"] == ids.priya and done["episode_id"] == "ep_stub"
    [call] = agent.calls
    assert call["who"] == ids.priya and call["intent"] == "draft" and call["fact_ids"] == [ids.c1]
    assert call["subject_ids"][:2] == [ids.acme, ids.deal]
    assert call["question"] == "Draft a status update for Acme Corp"
    proposal = env.runtime.memory.get_task(done["proposal_id"])
    assert proposal.kind == "proposal_email" and proposal.created_by == ids.priya
    assert env.runtime.memory.get_task(handoff_id(env.event.id, ids.priya)).state == "done"


def test_handoff_expires_when_the_commitment_is_no_longer_visible(env):
    ImpactWatcher(env.runtime, env.outbox).drain("acme", 10)
    env.visible[env.ids.priya].discard(env.ids.c1)
    agent = StubAgent(env.runtime)
    [expired] = handoff_mod.run_handoffs(env.runtime, agent, "acme", "w1")
    assert expired["state"] == "expired" and agent.calls == []


def test_sender_regates_and_expires_without_sending(env):
    ImpactWatcher(env.runtime, env.outbox).drain("acme", 10)
    env.visible[env.ids.priya] = set()
    [outcome] = run_sends(env.runtime, "acme", "w1")
    notice = env.runtime.memory.get_task(notice_id(env.event.id, env.ids.priya))
    assert outcome["state"] == notice.state == "expired" and "delivery" not in notice.payload


def test_tick_runs_events_then_handoffs_then_sends(env):
    agent = StubAgent(env.runtime)
    result = tick(env.runtime, "acme", outbox=env.outbox, agent=agent)
    ids = env.ids
    assert [o.notified for o in result.events] == [[ids.priya]]
    assert [h["state"] for h in result.handoffs] == ["done"] and [s["state"] for s in result.sends] == ["sent"]
    notice = env.runtime.memory.get_task(notice_id(env.event.id, ids.priya))
    delivery = notice.payload["delivery"]
    assert notice.state == "sent" and delivery["transport"] == "dry_run" and delivery["to"] == ids.priya
    pointer = DRAFT_POINTER.format(proposal_id=result.handoffs[0]["proposal_id"])
    assert delivery["text"] == f"{notice.payload['text']}\n\n{pointer}"
    summary = result.summary()
    assert "events=1 notified=1 filtered=1 written=2" in summary and "handoffs=done:1 sends=sent:1" in summary
    assert "Provider" not in summary and ids.priya not in summary

    again = tick(env.runtime, "acme", outbox=env.outbox, agent=agent)
    assert (again.events, again.handoffs, again.sends) == ([], [], [])


def test_tick_steps_can_be_chosen(env):
    result = tick(env.runtime, "acme", outbox=env.outbox, steps=("events",))
    assert len(result.events) == 1 and result.handoffs == [] and result.sends == []
    assert {t.state for t in tasks(env)} == {"ready"}
    assert parse_steps("sends, events") == ("events", "sends") and parse_steps(",".join(STEPS)) == STEPS
    with pytest.raises(ValueError):
        parse_steps("events,mail")


def test_transport_is_a_dry_run_for_the_fixture_tenant():
    assert isinstance(transport_for("slack", "acme", "xoxb-test", fixture_tenant="acme"), DryRunTransport)
    assert isinstance(transport_for("dry_run", "real", "xoxb-test", fixture_tenant="acme"), DryRunTransport)
    assert isinstance(transport_for("slack", "real", None, fixture_tenant="acme"), DryRunTransport)
    slack = transport_for("slack", "real", "xoxb-test", fixture_tenant="acme")
    assert isinstance(slack, SlackTransport) and "xoxb" not in repr(slack)
    with pytest.raises(ValueError):
        transport_for("email", "real", "xoxb-test", fixture_tenant="acme")


def test_slack_transport_opens_a_dm_then_posts_with_the_token_in_the_header_only():
    calls = []

    def http(url, headers, data):
        calls.append((url, headers, data))
        if url.endswith("conversations.open"):
            return 200, b'{"ok": true, "channel": {"id": "D0TEST"}}'
        return 200, b'{"ok": true, "ts": "1.2"}'

    receipt = SlackTransport("xoxb-test", http=http).send("p1", "U0ABCDEFGH", "hello")
    assert receipt == {"transport": "slack", "to": "p1", "channel": "D0TEST", "ts": "1.2"}
    assert [c[0].rsplit("/", 1)[1] for c in calls] == ["conversations.open", "chat.postMessage"]
    assert all(c[1]["Authorization"] == "Bearer xoxb-test" and b"xoxb" not in c[2] for c in calls)

    def refused(url, headers, data):
        return 200, b'{"ok": false, "error": "channel_not_found"}'

    with pytest.raises(sender_mod.SlackSendError, match="channel_not_found"):
        SlackTransport("xoxb-test", http=refused).send("p1", "U0ABCDEFGH", "hello")


def test_watcher_config_defaults_to_a_dry_run(env):
    watcher = env.runtime.cfg.watcher
    assert watcher.send == "dry_run" and watcher.batch > 0 and watcher.every > 0
