"""Turn one claimed Slack event into a gated answer and a thread reply.

A channel or group DM is answered as the channel principal. When that view
is empty or some evidence was filtered out, the reply says so and points the
asker at a DM. The DM itself is answered as that person. The offer never
includes a hidden count or a second, private answer.
"""

import logging
import re

from agents.answer.agent import AnswerAgent
from agents.gate.principal import Surface
from agents.slack.people import load_people, match_slack
from shared.acl import acl_group

log = logging.getLogger("weave.slack.reply")

MAX_TEXT = 3500
UNKNOWN_USER = "I don't know this Slack account, so I can't answer as you."
DM_OFFER = "This channel only includes what every member may see. DM me for the answer as you."
_MENTION = re.compile(r"<@[A-Z0-9]+>")


def question_text(event: dict) -> str:
    return " ".join(_MENTION.sub(" ", str(event.get("text") or "")).split())


def conversation_id(channel_id: str, thread_ts: str) -> str:
    return f"slack:{channel_id}:{thread_ts}"


def render_reply(card, *, offer: bool) -> str:
    """The card as a Slack message: the answer, then the citations the caller may see."""

    lines = [card.answer or "I couldn't find anything on that which you can see."]
    for index, claim in enumerate(card.claims, 1):
        lines.append(f"{index}. {claim.text}")
        for evidence_id in claim.evidence_ids:
            entry = card.evidence.get(evidence_id) or {}
            if entry.get("kind") == "projection":
                lines.append(f"   - restricted summary: {entry.get('summary')}")
            elif entry.get("kind") == "path":
                lines.append(f"   - {entry.get('src')} -{entry.get('rel')}-> {entry.get('dst')}")
            elif entry:
                lines.append(
                    f"   - {entry.get('author')} in {entry.get('source')} at {entry.get('at')}: "
                    f"\"{entry.get('quote')}\" {entry.get('url') or ''}".rstrip()
                )
    if offer:
        lines += ["", DM_OFFER]
    text = "\n".join(lines).strip()
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    return text


def needs_dm_offer(card) -> bool:
    return not card.claims and not card.proposal or card.hidden > 0


def answer_event(payload: dict, *, runtime, agent: AnswerAgent, slack, claims, tenant: str,
                 user_map: dict[str, str], people=None) -> dict | None:
    """Answer one already-claimed event. Returns what was posted, or ``None`` when nothing is sent."""

    event = payload.get("event") or {}
    event_id = payload.get("event_id") or ""
    channel_id = event.get("channel") or ""
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
    user = event.get("user") or ""
    try:
        bot = slack.bot_user()
        if not user or user == bot:
            claims.finish(event_id, "ignored")
            return None
        with claims.thread(channel_id, thread_ts):
            text, posted = _reply(event, runtime, agent, slack, tenant, user_map, people, bot, channel_id, thread_ts)
            if text:
                slack.post_message(channel_id, text, thread_ts)
            claims.finish(event_id, "replied" if text else "ignored")
            return posted
    except Exception as exc:
        log.warning("slack reply failed event=%s error=%s", event_id, type(exc).__name__)
        try:
            claims.finish(event_id, "failed")
        except Exception:
            log.warning("slack reply state update failed event=%s", event_id)
        return None


def _reply(event, runtime, agent, slack, tenant, user_map, people, bot, channel_id, thread_ts):
    question = question_text(event)
    if not question:
        return None, None
    people = people if people is not None else load_people(runtime.open_graph(tenant), tenant)
    kind = "dm" if event.get("channel_type") == "im" else "group" if event.get("channel_type") == "mpim" else "channel"
    if kind == "dm":
        person_id = match_slack(people, event.get("user") or "", user_map)
        if person_id is None:
            return UNKNOWN_USER, {"kind": "dm", "known": False}
        principal = runtime.resolver.for_person(tenant, person_id, Surface.SLACK_DM)
        card = agent.answer(
            principal, question, conversation_id=conversation_id(channel_id, thread_ts), ensure_conversation=True,
        )
        return render_reply(card, offer=False), {"kind": "dm", "known": True, "card": card}
    members = [member for member in slack.members(channel_id) if member and member != bot]
    member_ids = [match_slack(people, member, user_map) or member for member in members]
    if not member_ids:
        member_ids = [match_slack(people, event.get("user") or "", user_map) or (event.get("user") or channel_id)]
    surface = Surface.SLACK_GROUP_DM if kind == "group" else Surface.SLACK_CHANNEL
    principal = runtime.resolver.for_channel(tenant, acl_group("slack", channel_id), member_ids, surface)
    card = agent.answer(
        principal, question, conversation_id=conversation_id(channel_id, thread_ts), ensure_conversation=True,
    )
    offer = needs_dm_offer(card)
    return render_reply(card, offer=offer), {"kind": kind, "known": True, "card": card, "offer": offer}
