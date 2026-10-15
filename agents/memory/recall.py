"""Reading memory back through the gate.

Every entry is re-checked on every read: an episode or a conversation turn
whose ids are no longer all visible to the caller is dropped (episodes) or
replaced with a placeholder (turns), and nothing says how many. Nothing is
recalled for channels or on shared surfaces.
"""

from dataclasses import dataclass

from agents.memory.store import ConversationNotFound, Conversation, Episode, Turn
from agents.tools.base import ToolContext, ToolInputError
from agents.tools.common import visible_node_ids

UNAVAILABLE_TURN = "earlier answer no longer available"


@dataclass(frozen=True)
class Namespace:
    tenant: str
    person_id: str
    agent: str


def namespace(ctx: ToolContext) -> Namespace | None:
    """The caller's memory namespace, or ``None`` where memory is off (channels, shared surfaces)."""

    p = ctx.principal
    if not p.reads_private or not p.user_id:
        return None
    return Namespace(p.tenant, p.user_id, ctx.agent)


def store(ctx: ToolContext):
    if ctx.memory is None:
        raise ToolInputError("memory is not available here")
    return ctx.memory


def recall(ctx: ToolContext, query: str, limit: int) -> list[Episode]:
    ns = namespace(ctx)
    if ns is None or limit <= 0 or ctx.memory is None:
        return []
    candidates = ctx.memory.search_episodes(ns.tenant, ns.person_id, ns.agent, query, limit * 4)
    visible = visible_node_ids(ctx, [i for ep in candidates for i in ep.node_ids])
    kept = [ep for ep in candidates if all(i in visible for i in ep.node_ids)]
    return kept[:limit]


def own_conversation(ctx: ToolContext, conversation_id: str) -> Conversation:
    ns = namespace(ctx)
    conv = ctx.memory.get_conversation(conversation_id) if ns and ctx.memory else None
    if conv is None or (conv.tenant, conv.person_id) != (ns.tenant, ns.person_id):
        raise ConversationNotFound(conversation_id)
    return conv


def history(ctx: ToolContext, conversation_id: str, limit: int = 10) -> list[Turn]:
    """Earlier turns of the caller's own conversation, each re-gated."""

    own_conversation(ctx, conversation_id)
    turns = ctx.memory.turns(conversation_id, limit)
    visible = visible_node_ids(ctx, [i for t in turns for i in t.node_ids])
    return [
        t if all(i in visible for i in t.node_ids) else Turn(t.role, UNAVAILABLE_TURN, [], t.at)
        for t in turns
    ]
