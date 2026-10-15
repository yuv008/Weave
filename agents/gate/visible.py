"""The one visibility filter. Every tool builds its WHERE clauses from here.

Each function returns a Cypher boolean expression over a node alias. The
caller's level, groups and freshly synced sources travel as ``$gate_level``,
``$gate_groups`` and ``$gate_fresh`` from ``gate_params``; tools merge those
into their own parameters.

FalkorDB does not support ``EXISTS { }`` subqueries, and it rejects pattern
comprehensions inside WHERE. The claim rule is therefore a WITH step:
``keep_visible_claims`` binds the visible evidence list and keeps only claims
where it is non-empty. Tools return that list, never unfiltered evidence.
"""

import re

from agents.gate.principal import Principal
from shared.acl import ORG_WIDE
from shared.schema import Label, ProjectionStatus, Prop, Rel

LEVEL = "$gate_level"
GROUPS = "$gate_groups"
FRESH = "$gate_fresh"
_ALIAS = re.compile(r"^[a-z_][a-z0-9_]*$")


def _alias(alias: str) -> str:
    if not _ALIAS.fullmatch(alias):
        raise ValueError(f"bad Cypher alias {alias!r}")
    return alias


def gate_params(principal: Principal) -> dict:
    return {
        "gate_level": principal.level,
        "gate_groups": sorted(principal.groups),
        "gate_fresh": sorted(principal.fresh),
    }


def item_visible(alias: str = "i") -> str:
    """Source rule: not deleted, level is high enough, every source group is fresh, groups overlap."""

    a = _alias(alias)
    return (
        f"(coalesce({a}.{Prop.DELETED}, false) = false"
        f" AND {a}.{Prop.MIN_LEVEL} <= {LEVEL}"
        f" AND all(gate_g IN {a}.{Prop.ACL_GROUPS} WHERE gate_g = '{ORG_WIDE}' OR gate_g IN {FRESH})"
        f" AND any(gate_g IN {a}.{Prop.ACL_GROUPS} WHERE gate_g IN {GROUPS}))"
    )


def visible_evidence(claim_alias: str = "d", item_alias: str = "gate_i") -> str:
    """List of the claim's evidence items the caller may see."""

    c, i = _alias(claim_alias), _alias(item_alias)
    return f"[({c})-[:{Rel.EVIDENCED_BY}]->({i}:{Label.ITEM}) WHERE {item_visible(i)} | {i}]"


def keep_visible_claims(alias: str = "d", carry: tuple[str, ...] = (), evidence_as: str = "gate_evidence") -> str:
    """Claim rule as a WITH step: keep claims with at least one visible evidence item.

    ``carry`` lists other aliases to keep in scope. The visible items are bound
    to ``evidence_as``.
    """

    names = [_alias(name) for name in (*carry, alias)]
    bound = _alias(evidence_as)
    return f"WITH {', '.join(names)}, {visible_evidence(alias)} AS {bound} WHERE size({bound}) > 0"


def keep_visible_edges(rel_alias: str = "r", carry: tuple[str, ...] = (), evidence_as: str = "gate_edge_evidence") -> str:
    """Edge rule for dependency edges: keep rows whose edge has at least one visible ``evidence_ids`` item.

    Two clauses (OPTIONAL MATCH, then WITH); ``carry`` lists the aliases to keep
    besides the edge. An edge without ``evidence_ids`` is dropped. Claims at
    either end still need ``keep_visible_claims``.
    """

    r = _alias(rel_alias)
    names = [_alias(name) for name in (*carry, r)]
    bound = _alias(evidence_as)
    return (
        f"OPTIONAL MATCH (gate_ei:{Label.ITEM}) "
        f"WHERE gate_ei.{Prop.ID} IN coalesce({r}.{Prop.EVIDENCE_IDS}, []) AND {item_visible('gate_ei')} "
        f"WITH {', '.join(names)}, collect(gate_ei) AS {bound} WHERE size({bound}) > 0"
    )


def chunk_visible_sql(table: str = "chunk") -> str:
    """SQL twin of ``item_visible`` for the Postgres ``chunk`` table, with the same ``gate_*`` parameters."""

    t = _alias(table)
    return (
        f"({t}.min_level <= %(gate_level)s"
        f" AND {t}.acl_groups && %(gate_groups)s::text[]"
        f" AND {t}.acl_groups <@ (%(gate_fresh)s::text[] || '{ORG_WIDE}'::text))"
    )


def projection_visible(alias: str = "p") -> str:
    """Projection rule: approved, audience matches, level floor met."""

    a = _alias(alias)
    return (
        f"({a}.{Prop.STATUS} = '{ProjectionStatus.APPROVED}'"
        f" AND {a}.{Prop.LEVEL_FLOOR} <= {LEVEL}"
        f" AND any(gate_g IN {a}.{Prop.AUDIENCE} WHERE gate_g IN {GROUPS}))"
    )
