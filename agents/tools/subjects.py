"""Turn what the caller typed (an id, a name or an alias) into a subject node.

Unknown and provisional subjects come back as ``None``, and tools answer
"nothing found" for them exactly as for subjects with no visible facts.
"""

from dataclasses import dataclass

from agents.tools.base import ToolContext
from shared.schema import Label, Prop

SUBJECT_LABELS = (Label.TOPIC, Label.PROJECT, Label.CUSTOMER, Label.DEAL)


@dataclass(frozen=True)
class Subject:
    id: str
    name: str
    label: str


def _query(labels: tuple[str, ...], hide_provisional: bool) -> str:
    label_test = " OR ".join(f"n:{label}" for label in labels)
    provisional = f" AND coalesce(n.{Prop.PROVISIONAL}, false) = false" if hide_provisional else ""
    return f"""
MATCH (n)
WHERE ({label_test}){provisional}
  AND (n.{Prop.ID} = $ref
       OR toLower(n.{Prop.NAME}) = $lower
       OR any(a IN coalesce(n.{Prop.ALIASES}, []) WHERE toLower(a) = $lower))
RETURN n.{Prop.ID}, n.{Prop.NAME}, labels(n)
"""


def resolve_subject(ctx: ToolContext, ref: str | None, labels: tuple[str, ...] = SUBJECT_LABELS) -> Subject | None:
    if not ref or not ref.strip():
        return None
    ref = ref.strip()
    rows = ctx.rows(_query(labels, ctx.cfg.hide_provisional), ref=ref, lower=ref.lower())
    if not rows:
        return None

    def rank(row) -> tuple[int, int]:
        label = next(label for label in labels if label in row[2])
        return (0 if row[0] == ref else 1, labels.index(label))

    node_id, name, node_labels = min(rows, key=rank)
    label = next(label for label in labels if label in node_labels)
    return Subject(node_id, name, label)
