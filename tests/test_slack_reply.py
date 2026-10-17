"""Slack mentions and DMs: channel principal, one reply per event, episode audience."""

import threading

from fastapi.testclient import TestClient

from agents.answer.agent import build_agent
from agents.answer.card import AnswerCard
from agents.answer.composer import Claim
from agents.gate.principal import Kind, Surface
from agents.memory.recall import UNAVAILABLE_TURN, recall, shared_history
from agents.slack.accept import accept
from agents.slack.claims import MemoryReplyClaims
from agents.slack.people import PersonLink, match_slack
from agents.slack.reply import DM_OFFER, UNKNOWN_USER, answer_event, needs_dm_offer, render_reply
from app.main import create_app
from shared.acl import acl_group
from shared.schema import Label, Prop
from tests.answer_support import offline_runtime
from tests.test_events import SECRET, _post


class FakeSlack:
    def __init__(self, members, bot="UBOT"):
        self._members = list(members)
        self._bot = bot
        self.posts: list[tuple[str, str, str]] = []

    def bot_user(self) -> str:
        return self._bot

    def members(self, channel_id: str) -> list[str]:
        return list(self._members)

    def post_message(self, channel_id: str, text: str, thread_ts: str) -> dict:
        self.posts.append((channel_id, text, thread_ts))
        return {"ok": True}


def _people(env) -> list[PersonLink]:
    return [
        PersonLink(node.id, node.props[Prop.EMAIL], node.props[Prop.NAME], node.props.get(Prop.SLACK_ID))
        for node in env.fx.nodes
        if node.label == Label.PERSON
    ]


def _event(kind: str, *, user: str, channel: str, event_id: str, text: str = "Which SSO provider should I use?") -> dict:
    event = {"type": "app_mention" if kind == "channel" else "message", "user": user, "channel": channel,
             "text": text, "ts": "10.1"}
    if kind == "dm":
        event["channel_type"] = "im"
    elif kind == "group":
        event["channel_type"] = "mpim"
    return {"type": "event_callback", "event_id": event_id, "event": event}


def _deliver(env, payload, slack, claims, people=None, user_map=None):
    return answer_event(
        payload, runtime=env.runtime, agent=build_agent(env.runtime), slack=slack, claims=claims,
        tenant=env.fx.tenant, user_map=user_map or {}, people=people if people is not None else _people(env),
    )


def test_user_map_is_only_used_when_the_graph_has_no_slack_id():
    people = [PersonLink("person-sam", "sam@weave-demo.example", "Sam", "U0SAM")]
    assert match_slack(people, "U0SAM", {"UREAL": "priya"}) == "person-sam"
    assert match_slack(people, "UREAL", {"UREAL": "sam"}) == "person-sam"
    assert match_slack(people, "UOTHER", {"UREAL": "sam"}) is None


def test_channel_mention_uses_the_lowest_member_level():
    env = offline_runtime()
    people = _people(env)
    maya = "U0MAYA"
    claims = MemoryReplyClaims()
    alone = FakeSlack([maya, "UBOT"])
    payload = _event("channel", user=maya, channel="CNEW", event_id="Ev-maya")
    assert claims.claim("Ev-maya", env.fx.tenant, "CNEW", "10.1")
    _deliver(env, payload, alone, claims, people)
    group = acl_group("slack", "CNEW")
    episode = next(ep for ep in env.runtime.memory.episodes.values() if ep.audience == group)
    assert episode.person_id == group
    assert episode.audience == group

    claims2 = MemoryReplyClaims()
    mixed = FakeSlack([maya, "U0NOBODY", "UBOT"])
    payload2 = _event("channel", user=maya, channel="CNEW", event_id="Ev-low")
    assert claims2.claim("Ev-low", env.fx.tenant, "CNEW", "10.1")
    _deliver(env, payload2, mixed, claims2, people)
    assert mixed.posts
    assert DM_OFFER in mixed.posts[0][1]
    assert "hidden" not in mixed.posts[0][1].lower()


def test_unknown_member_forces_level_zero():
    env = offline_runtime()
    people = _people(env)
    from agents.slack.reply import _reply

    _, posted = _reply(
        _event("channel", user="U0MAYA", channel="CNEW", event_id="Ev")["event"],
        env.runtime, build_agent(env.runtime), FakeSlack(["U0MAYA"]), env.fx.tenant, {}, people, "UBOT",
        "CNEW", "10.1",
    )
    principal = env.runtime.resolver.for_channel(
        env.fx.tenant, acl_group("slack", "CNEW"), [env.fx.ids["maya"]], Surface.SLACK_CHANNEL,
    )
    assert principal.level == 2
    _, posted_low = _reply(
        _event("channel", user="U0MAYA", channel="CLOW", event_id="Ev2")["event"],
        env.runtime, build_agent(env.runtime), FakeSlack(["U0MAYA", "U0NOBODY"]), env.fx.tenant, {}, people,
        "UBOT", "CLOW", "10.1",
    )
    low = env.runtime.resolver.for_channel(
        env.fx.tenant, acl_group("slack", "CLOW"), [env.fx.ids["maya"], "U0NOBODY"], Surface.SLACK_CHANNEL,
    )
    assert low.level == 0
    assert low.kind is Kind.CHANNEL
    assert posted_low["offer"] is True
    assert posted["kind"] == "channel"


def test_dm_for_sam_stores_a_personal_episode():
    env = offline_runtime()
    claims = MemoryReplyClaims()
    slack = FakeSlack([])
    payload = _event("dm", user="U0SAM", channel="D1", event_id="Ev-dm")
    assert claims.claim("Ev-dm", env.fx.tenant, "D1", "10.1")
    _deliver(env, payload, slack, claims)
    episode = next(iter(env.runtime.memory.episodes.values()))
    assert episode.person_id == env.fx.ids["sam"]
    assert episode.audience == "person"
    assert slack.posts and UNKNOWN_USER not in slack.posts[0][1]
    recalled = recall(env.runtime.context(env.resolver.for_person(env.fx.tenant, env.fx.ids["sam"], Surface.SLACK_DM)),
                      "SSO provider", 5)
    assert episode.id in {item.id for item in recalled}


def test_channel_episode_is_not_recalled_for_sam():
    env = offline_runtime()
    claims = MemoryReplyClaims()
    slack = FakeSlack(["U0SAM", "UBOT"])
    payload = _event("channel", user="U0SAM", channel="CCHAN", event_id="Ev-ch")
    assert claims.claim("Ev-ch", env.fx.tenant, "CCHAN", "10.1")
    _deliver(env, payload, slack, claims)
    group = acl_group("slack", "CCHAN")
    episode = next(iter(env.runtime.memory.episodes.values()))
    assert episode.audience == group
    recalled = recall(env.runtime.context(env.resolver.for_person(env.fx.tenant, env.fx.ids["sam"], Surface.SLACK_DM)),
                      "SSO provider", 5)
    assert episode.id not in {item.id for item in recalled}


def test_unknown_dm_does_not_call_the_agent():
    env = offline_runtime()
    claims = MemoryReplyClaims()
    slack = FakeSlack([])
    calls = []

    class Agent:
        def answer(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("unknown user")

    payload = _event("dm", user="U0NOBODY", channel="D9", event_id="Ev-unknown")
    assert claims.claim("Ev-unknown", env.fx.tenant, "D9", "10.1")
    answer_event(
        payload, runtime=env.runtime, agent=Agent(), slack=slack, claims=claims, tenant=env.fx.tenant,
        user_map={}, people=_people(env),
    )
    assert calls == []
    assert slack.posts[0][1] == UNKNOWN_USER


def test_dm_offer_has_no_hidden_count():
    card = AnswerCard(
        question="budget", intent="lookup", answer="Nothing you can all see.",
        claims=[Claim("A public note.", [])], hidden=4,
    )
    assert needs_dm_offer(card)
    text = render_reply(card, offer=True)
    assert DM_OFFER in text
    assert "4" not in text
    assert "hidden" not in text.lower()


def test_duplicate_event_replies_once():
    claims = MemoryReplyClaims()
    replies = []
    payload = _event("channel", user="U0SAM", channel="C1", event_id="Ev-once")

    def spawn(fn):
        fn()

    for _ in range(2):
        accept(payload, ingest=lambda: None, reply=lambda: replies.append("go"), spawn=spawn, claims=claims,
               tenant="acme")
    assert replies == ["go"]


def test_bot_message_is_not_answered():
    env = offline_runtime()
    claims = MemoryReplyClaims()
    slack = FakeSlack(["UBOT"])
    payload = _event("dm", user="UBOT", channel="D1", event_id="Ev-bot")
    assert claims.claim("Ev-bot", env.fx.tenant, "D1", "10.1")
    assert _deliver(env, payload, slack, claims) is None
    assert slack.posts == []
    assert claims.events["Ev-bot"]["state"] == "ignored"


def test_thread_lock_is_per_thread():
    claims = MemoryReplyClaims()
    order = []
    started = threading.Event()
    release = threading.Event()

    def hold():
        with claims.thread("C", "1.0"):
            order.append("in")
            started.set()
            release.wait(2)
            order.append("out")

    def follow():
        started.wait(2)
        with claims.thread("C", "1.0"):
            order.append("next")

    first = threading.Thread(target=hold)
    second = threading.Thread(target=follow)
    first.start()
    second.start()
    assert started.wait(2)
    assert claims._threads[("C", "1.0")].locked()
    release.set()
    first.join(2)
    second.join(2)
    assert order == ["in", "out", "next"]


def test_channel_history_drops_a_turn_the_channel_can_no_longer_see():
    env = offline_runtime()
    group = acl_group("slack", "CCHAN")
    principal = env.resolver.for_channel(env.fx.tenant, group, [env.fx.ids["sam"]], Surface.SLACK_CHANNEL)
    memory = env.runtime.memory
    conversation = memory.create_conversation(env.fx.tenant, group, "slack_channel", "thread",
                                             conversation_id="slack:CCHAN:10.1")
    memory.add_turn(conversation, "user", "hello", [])
    memory.add_turn(conversation, "agent", "secret", ["missing-node"])
    turns = shared_history(env.runtime.context(principal), conversation, group)
    assert [turn.text for turn in turns] == ["hello", UNAVAILABLE_TURN]


def test_mention_through_http_replies_once():
    seen = []
    claims = MemoryReplyClaims()
    client = TestClient(create_app(
        graph=type("Silent", (), {"query": lambda self, *a, **k: type("R", (), {"result_set": []})()})(),
        signing_secret=SECRET, tenant_id="acme", connection_id="c", workspace_id="T",
        reply_claims=claims, slack_runner=lambda payload: seen.append(payload["event_id"]),
    ))
    payload = _event("channel", user="U0SAM", channel="C1", event_id="Ev-http")
    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200
    assert seen == ["Ev-http"]
