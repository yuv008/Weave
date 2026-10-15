"""remember and recall_memory: the caller's own agent memory.

Memory is a pointer, never a copy: an entry holds node ids and a short note.
Ids must be visible when stored and are re-gated on every recall, so losing
access to a source also loses the memories that point into it.
"""

from agents.memory import recall as gated
from agents.memory.store import Episode
from agents.tools.base import ToolContext, ToolInputError, ToolResult, tool
from agents.tools.common import visible_node_ids

MAX_IDS = 20
MAX_RECALL = 20


@tool(
    "remember",
    use_when="The user asks you to remember something for later, such as a follow-up or a preferred answer, "
             "tied to node ids you already found in this conversation.",
    dont_use_when="Storing facts, numbers or quotes (memory holds ids and a short note only), "
                  "or anything the user did not ask you to keep.",
    args={
        "note": "A short note in your own words, e.g. 'follow up with Priya on SSO dates'. No facts or figures.",
        "node_ids": "Node ids from earlier tool results that the note is about.",
    },
    mode="memory",
)
def remember(ctx: ToolContext, note: str, node_ids: list[str]) -> ToolResult:
    """Save a short note about some node ids to the caller's private memory."""

    ns = gated.namespace(ctx)
    if ns is None:
        raise ToolInputError("memory is only kept for a person on a private surface")
    memory = gated.store(ctx)
    note = " ".join((note or "").split())
    if not note:
        raise ToolInputError("give a note")
    if len(note) > ctx.cfg.answer.note_max_chars:
        raise ToolInputError(
            f"note must be at most {ctx.cfg.answer.note_max_chars} characters; store ids, not facts"
        )
    ids = list(dict.fromkeys(node_ids))
    if not ids or len(ids) > MAX_IDS:
        raise ToolInputError(f"give 1-{MAX_IDS} node ids")
    missing = [i for i in ids if i not in visible_node_ids(ctx, ids)]
    if missing:
        raise ToolInputError(f"these ids are not available: {missing}")
    episode_id = memory.add_episode(
        Episode(ns.tenant, ns.person_id, ns.agent, note=note, node_ids=ids, kind="note")
    )
    return ToolResult("remember", {"memory_id": episode_id, "note": note}, node_ids=ids)


@tool(
    "recall_memory",
    use_when="The question refers to something earlier ('like last time', 'that decision we discussed') "
             "or you want ids from the caller's past questions on the same subject.",
    dont_use_when="Looking up company facts (use the graph tools; memory only points at nodes).",
    args={
        "query": "Words to match against past questions and notes, e.g. 'SSO provider'.",
        "limit": "Most entries to return.",
    },
    mode="memory",
)
def recall_memory(ctx: ToolContext, query: str, limit: int = 5) -> ToolResult:
    """The caller's past questions and notes that match, each re-checked against what the caller may see now."""

    result = ToolResult("recall_memory", {"memories": []})
    if gated.namespace(ctx) is None or ctx.memory is None:
        result.message = "No memory is used here."
        return result
    limit = max(1, min(limit, MAX_RECALL))
    for ep in gated.recall(ctx, query or "", limit):
        entry = {"memory_id": ep.id, "kind": ep.kind, "node_ids": ep.node_ids,
                 "at": ep.created_at.isoformat() if ep.created_at else None}
        if ep.question:
            entry["question"] = ep.question
        if ep.note:
            entry["note"] = ep.note
        if ep.feedback:
            entry["feedback"] = ep.feedback.get("decision")
        result.data["memories"].append(entry)
        result.node_ids += ep.node_ids
    if not result.data["memories"]:
        result.message = "Nothing relevant in your memory."
    return result
