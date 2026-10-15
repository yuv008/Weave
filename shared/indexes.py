"""Indexes for ``co:{tenant}``. Safe to call twice.

Vector search runs on the Postgres ``chunk`` table, not in FalkorDB.
"""

from shared.schema import CONTRACT_LABELS, Label, Prop

_RANGE = tuple((label, Prop.ID) for label in CONTRACT_LABELS) + (
    (Label.DECISION, Prop.STATUS),
    (Label.COMMITMENT, Prop.STATUS),
    (Label.FIGURE, Prop.STATUS),
    (Label.PROJECTION, Prop.STATUS),
    (Label.ITEM, Prop.MIN_LEVEL),
    (Label.PERSON, Prop.EMAIL),
)

_FULLTEXT = ((Label.TOPIC, Prop.NAME), (Label.PROJECT, Prop.NAME), (Label.CUSTOMER, Prop.NAME))


def index_statements() -> list[str]:
    statements = [f"CREATE INDEX FOR (n:{label}) ON (n.{prop})" for label, prop in _RANGE]
    statements += [
        f"CALL db.idx.fulltext.createNodeIndex('{label}', '{prop}')" for label, prop in _FULLTEXT
    ]
    return statements


def ensure_contract_indexes(graph) -> None:
    for statement in index_statements():
        try:
            graph.query(statement)
        except Exception as exc:
            if "already" not in str(exc).lower():
                raise
