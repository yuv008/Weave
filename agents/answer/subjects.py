"""Find the subjects a question is about: topics, projects, customers, deals and people.

Names and aliases are matched as whole words, longest first, without
overlaps. When a word matches nothing exactly, a distinctive first word of a
name still counts ("Acme" for "Acme Corp" and "Acme renewal"). Provisional
entities never match when the config hides them.
"""

import re
from dataclasses import dataclass

from agents.tools.base import ToolContext
from agents.tools.common import visible_node_ids
from shared.schema import Label, Prop

MATCH_LABELS = (Label.TOPIC, Label.PROJECT, Label.CUSTOMER, Label.DEAL, Label.PERSON)
_NON_WORD = re.compile(r"[^a-z0-9]+")
_MIN_FIRST_WORD = 4


@dataclass(frozen=True)
class Match:
    id: str
    name: str
    label: str
    phrase: str


def normalize(text: str) -> str:
    return " ".join(_NON_WORD.sub(" ", (text or "").lower()).split())


def _query(hide_provisional: bool) -> str:
    labels = " OR ".join(f"n:{label}" for label in MATCH_LABELS)
    provisional = f" AND coalesce(n.{Prop.PROVISIONAL}, false) = false" if hide_provisional else ""
    return (
        f"MATCH (n) WHERE ({labels}){provisional} "
        f"RETURN n.{Prop.ID}, n.{Prop.NAME}, coalesce(n.{Prop.ALIASES}, []), labels(n)"
    )


def match_subjects(ctx: ToolContext, question: str) -> list[Match]:
    text = f" {normalize(question)} "
    candidates: list[tuple[str, str, str, str]] = []
    first_words: dict[str, list[tuple[str, str, str]]] = {}
    for node_id, name, aliases, labels in ctx.rows(_query(ctx.cfg.hide_provisional)):
        if not name:
            continue
        label = next(label for label in MATCH_LABELS if label in labels)
        for phrase in {normalize(name), *(normalize(a) for a in aliases)}:
            if phrase:
                candidates.append((phrase, node_id, name, label))
        first = normalize(name).split(" ")[0]
        minimum = 3 if label == Label.PERSON else _MIN_FIRST_WORD
        if len(first) >= minimum:
            first_words.setdefault(first, []).append((node_id, name, label))

    taken: list[tuple[int, int]] = []
    found: dict[str, Match] = {}
    for phrase, node_id, name, label in sorted(candidates, key=lambda c: -len(c[0])):
        start = text.find(f" {phrase} ")
        if start < 0:
            continue
        span = (start + 1, start + 1 + len(phrase))
        if node_id in found or any(span[0] < end and start_ < span[1] for start_, end in taken):
            continue
        taken.append(span)
        found[node_id] = Match(node_id, name, label, phrase)

    covered = {w for m in found.values() for w in m.phrase.split()}
    for word in text.split():
        if word in covered:
            continue
        for node_id, name, label in first_words.get(word, []):
            found.setdefault(node_id, Match(node_id, name, label, word))
    return list(found.values())


_BY_ID = f"""
UNWIND $ids AS nid
MATCH (n {{{Prop.ID}: nid}})
RETURN n.{Prop.ID}, n.{Prop.NAME}, labels(n)
"""


def subjects_for(ctx: ToolContext, node_ids: list[str]) -> list[Match]:
    """Matches for known subject ids the caller may see, in the order given."""

    ids = list(dict.fromkeys(i for i in node_ids if isinstance(i, str) and i))
    visible = visible_node_ids(ctx, ids)
    rows = {}
    for node_id, name, labels in ctx.rows(_BY_ID, ids=[i for i in ids if i in visible]):
        label = next((label for label in MATCH_LABELS if label in labels), None)
        if name and label:
            rows[node_id] = Match(node_id, name, label, normalize(name))
    return [rows[i] for i in ids if i in rows]


def first(matches: list[Match], *labels: str) -> Match | None:
    """The first match by label preference."""

    for label in labels:
        for m in matches:
            if m.label == label:
                return m
    return None
