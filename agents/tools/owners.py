"""find_owner: who owns a subject and who knows most about it.

Expertise is scored at query time over the items the caller can see. The
stored ``KNOWS_ABOUT.score`` is computed over every item, so using it would
leak activity in sources the caller cannot read.
"""

from collections import defaultdict

from agents.gate.visible import item_visible
from agents.tools.base import Citation, PathEdge, ToolContext, ToolInputError, ToolResult, tool
from agents.tools.subjects import resolve_subject
from shared.schema import Label, Prop, Rel

TOP_EXPERTS = 3
CITATIONS_PER_EXPERT = 3

_OWNER_FIELDS = f"p.{Prop.ID} AS person, p.{Prop.NAME} AS name, p.{Prop.DEPT} AS dept"

_OWNERS = f"""
MATCH (p:{Label.PERSON})-[:{Rel.OWNS}]->(s {{{Prop.ID}: $subject}})
RETURN {_OWNER_FIELDS}, s.{Prop.ID} AS owned
UNION
MATCH (p:{Label.PERSON})-[:{Rel.OWNS}]->(d:{Label.DEAL})-[:{Rel.FOR}]->(s:{Label.CUSTOMER} {{{Prop.ID}: $subject}})
RETURN {_OWNER_FIELDS}, d.{Prop.ID} AS owned
"""

_ITEM_FIELDS = (
    f"{item_visible('i')} AS ok, {_OWNER_FIELDS}, i.{Prop.ID} AS item, i.{Prop.OCCURRED_AT} AS at, "
    f"i.{Prop.EXCERPT} AS excerpt, i.{Prop.SOURCE_URL} AS url, src.{Prop.NAME} AS source"
)

_ACTIVITY = f"""
MATCH (i:{Label.ITEM})-[:{Rel.MENTIONS}]->(s {{{Prop.ID}: $subject}})
MATCH (i)-[:{Rel.AUTHORED_BY}]->(p:{Label.PERSON})
OPTIONAL MATCH (i)-[:{Rel.IN_SOURCE}]->(src:{Label.SOURCE})
RETURN {_ITEM_FIELDS}, '{Rel.MENTIONS}' AS via
UNION
MATCH (c)-[:{Rel.ABOUT}]->(s {{{Prop.ID}: $subject}})
MATCH (c)-[:{Rel.EVIDENCED_BY}]->(i:{Label.ITEM})-[:{Rel.AUTHORED_BY}]->(p:{Label.PERSON})
OPTIONAL MATCH (i)-[:{Rel.IN_SOURCE}]->(src:{Label.SOURCE})
RETURN {_ITEM_FIELDS}, '{Rel.EVIDENCED_BY}' AS via
"""


@tool(
    "find_owner",
    use_when="You need who owns a project, topic or customer, or who to ask about it.",
    dont_use_when="You need what was decided (use get_decision) or what depends on it (use get_dependencies).",
    args={"subject": "A project, topic or customer name, alias or id, e.g. 'SSO integration' or 'Acme Corp'."},
)
def find_owner(ctx: ToolContext, subject: str) -> ToolResult:
    """Owners of the subject plus the top people by expertise over visible items, with evidence."""

    if not subject:
        raise ToolInputError("give a project, topic or customer")
    result = ToolResult("find_owner", {"owners": [], "experts": []})
    found = resolve_subject(ctx, subject, (Label.PROJECT, Label.TOPIC, Label.CUSTOMER))
    if found is None:
        result.message = "Nothing by that name is visible to you."
        return result
    result.data["subject"] = {"id": found.id, "name": found.name, "label": found.label}
    result.node_ids.append(found.id)

    for person, name, dept, owned in ctx.rows(_OWNERS, subject=found.id):
        result.data["owners"].append({"id": person, "name": name, "dept": dept, "owns": owned})
        result.node_ids.append(person)
        result.path.append(PathEdge(person, Rel.OWNS, owned))
        if owned != found.id:
            result.path.append(PathEdge(owned, Rel.FOR, found.id))

    half_life = ctx.cfg.expertise_half_life_days
    people: dict[str, dict] = {}
    items: dict[str, dict[str, dict]] = defaultdict(dict)
    hidden = set()
    for ok, person, name, dept, item, at, excerpt, url, source, via in ctx.rows(_ACTIVITY, subject=found.id):
        if not ok:
            hidden.add(item)
            continue
        people[person] = {"id": person, "name": name, "dept": dept}
        items[person][item] = {"at": at, "excerpt": excerpt, "url": url, "source": source, "via": via, "author": name}
    result.hidden += len(hidden)

    scored = []
    for person, mine in items.items():
        score = sum(0.5 ** (max(ctx.age_days(i["at"]) or 0, 0) / half_life) for i in mine.values())
        scored.append((round(score, 3), person))
    scored.sort(key=lambda pair: (-pair[0], people[pair[1]]["name"]))
    for score, person in scored[:TOP_EXPERTS]:
        newest = sorted(items[person].items(), key=lambda kv: kv[1]["at"] or "", reverse=True)[:CITATIONS_PER_EXPERT]
        result.data["experts"].append({**people[person], "score": score, "items": len(items[person]),
                                       "evidence": [item for item, _ in newest]})
        result.node_ids += [person, *(item for item, _ in newest)]
        for item, info in newest:
            result.citations.append(Citation(item, info["excerpt"], info["author"], info["at"], info["source"], info["url"]))
            result.path.append(PathEdge(item, Rel.AUTHORED_BY, person))
            if info["via"] == Rel.MENTIONS:
                result.path.append(PathEdge(item, Rel.MENTIONS, found.id))
    return result
