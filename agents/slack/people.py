"""Map a Slack user id onto a Person node. The map in ``.env`` is only a fallback."""

from dataclasses import dataclass

from shared.schema import Label, Prop

_PEOPLE = f"""
MATCH (p:{Label.PERSON})
WHERE p.{Prop.TENANT_ID} = $tenant
RETURN p.{Prop.ID}, p.{Prop.EMAIL}, p.{Prop.NAME}, p.{Prop.SLACK_ID}
"""


@dataclass(frozen=True)
class PersonLink:
    id: str
    email: str
    name: str
    slack_id: str | None


def parse_user_map(raw: str | None) -> dict[str, str]:
    """``U012=sam,U045=priya`` from ``SLACK_USER_MAP``. Empty or broken pairs are skipped."""

    found: dict[str, str] = {}
    for part in (raw or "").split(","):
        slack_id, sep, who = part.strip().partition("=")
        if sep and slack_id.strip() and who.strip():
            found[slack_id.strip()] = who.strip()
    return found


def load_people(graph, tenant: str) -> list[PersonLink]:
    rows = graph.ro_query(_PEOPLE, {"tenant": tenant}).result_set
    return [PersonLink(person_id, email or "", name or "", slack_id) for person_id, email, name, slack_id in rows]


def match_slack(people: list[PersonLink], slack_id: str, user_map: dict[str, str]) -> str | None:
    """The person id for this Slack user, or ``None`` when the workspace does not know them."""

    for person in people:
        if person.slack_id and person.slack_id == slack_id:
            return person.id
    token = user_map.get(slack_id)
    if not token:
        return None
    wanted = token.casefold()
    for person in people:
        local = person.email.split("@", 1)[0].casefold() if person.email else ""
        if token == person.id or wanted in (person.email.casefold(), person.name.casefold(), local):
            return person.id
    return None
