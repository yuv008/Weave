"""Seed the answer agent's playbooks (global rows, tenant_id NULL). Safe to run twice.

A new version is written only when the steps differ from the latest active one.

Usage: python -m scripts.seed_playbooks [--approved-by NAME]
"""

import argparse

from agents.answer.intents import ALLOWED, Intent
from agents.answer.planner import STEPS

PLAYBOOKS: list[tuple[str, Intent, list[str]]] = [
    # An API question from engineering: the active decision (with what it replaced), cited.
    ("engineering", Intent.LOOKUP, ["lookup", "search_if_empty"]),
    # Provenance: the decision, then its evidence and threads.
    ("default", Intent.PROVENANCE, ["decision", "trace", "search_if_empty"]),
    # Flow 2, a sales status draft: what the deal needs, the decision, the numbers, then the draft.
    ("sales", Intent.DRAFT, ["dependencies", "decision", "figures", "propose"]),
    # Impact: what rests on a replaced decision, then what depends on the subject.
    ("default", Intent.IMPACT, ["decision", "impact", "dependencies", "search_if_empty"]),
]


def check(playbooks=PLAYBOOKS) -> None:
    for role, intent, steps in playbooks:
        unknown = [s for s in steps if s not in STEPS]
        if unknown:
            raise ValueError(f"{role}/{intent}: unknown steps {unknown}")
        if intent not in ALLOWED:
            raise ValueError(f"{role}: unknown intent {intent}")


def seed(store, approved_by: str | None = None, playbooks=PLAYBOOKS) -> list[tuple[str, str, int]]:
    check(playbooks)
    written = []
    for role, intent, steps in playbooks:
        if store.playbook("", role, str(intent)) == steps:
            continue
        written.append((role, str(intent), store.put_playbook(None, role, str(intent), steps, approved_by)))
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-by", default=None)
    args = parser.parse_args()

    from agents.memory.store import PostgresMemoryStore
    from shared.env import load_env
    from shared.pg import connect_pg

    load_env()
    written = seed(PostgresMemoryStore(connect_pg), args.approved_by)
    for role, intent, version in written:
        print(f"{role}/{intent}: version {version}")
    if not written:
        print("playbooks already up to date")


if __name__ == "__main__":
    main()
