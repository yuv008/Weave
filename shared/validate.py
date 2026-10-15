"""Contract checks for any ``co:{tenant}`` graph.

Works on plain rows, so the same checks run on the fixture offline and on
a live graph export. Nodes without a contract label (old ``Tenant``,
``SyncState`` and so on) are ignored.
"""

from collections import defaultdict
from dataclasses import dataclass

from shared.acl import is_acl
from shared.schema import (
    CASCADE_POLICIES,
    CLAIM_LABELS,
    CLAIM_STATUSES,
    COMMITMENT_PROGRESS,
    CONTRACT_LABELS,
    EDGE_ENDPOINTS,
    LEVELS,
    PROJECTION_STATUSES,
    REQUIRED_EDGE_PROPS,
    REQUIRED_PROPS,
    TIMESTAMP_PROPS,
    Label,
    Prop,
    Rel,
    Status,
)
from shared.timefmt import is_ts


@dataclass(frozen=True)
class NodeRow:
    labels: tuple[str, ...]
    props: dict


@dataclass(frozen=True)
class EdgeRow:
    rel: str
    src: str
    dst: str
    props: dict


def validate(tenant: str, nodes: list[NodeRow], edges: list[EdgeRow]) -> list[str]:
    errors: list[str] = []
    by_id: dict[str, tuple[str, dict]] = {}

    for node in nodes:
        contract = [label for label in node.labels if label in CONTRACT_LABELS]
        if not contract:
            continue
        node_id = node.props.get(Prop.ID)
        if len(contract) > 1:
            errors.append(f"{node_id}: more than one contract label {contract}")
            continue
        label = contract[0]
        if not isinstance(node_id, str) or not node_id.startswith(f"{tenant}:"):
            errors.append(f"{label} {node_id!r}: id must start with '{tenant}:'")
            continue
        if node_id in by_id:
            errors.append(f"{node_id}: duplicate id")
            continue
        by_id[node_id] = (label, node.props)
        errors += _node_errors(tenant, node_id, label, node.props)

    out_edges: dict[str, list[EdgeRow]] = defaultdict(list)
    for edge in edges:
        src, dst = by_id.get(edge.src), by_id.get(edge.dst)
        if src is None or dst is None:
            if edge.rel in EDGE_ENDPOINTS:
                errors.append(f"{edge.rel} {edge.src} -> {edge.dst}: endpoint is not a contract node")
            continue
        rule = EDGE_ENDPOINTS.get(edge.rel)
        if rule is None:
            errors.append(f"{edge.rel}: not a contract edge type")
            continue
        if src[0] not in rule[0] or dst[0] not in rule[1]:
            errors.append(f"{edge.rel} {src[0]} -> {dst[0]}: endpoints not allowed")
        for prop in REQUIRED_EDGE_PROPS.get(edge.rel, ()):
            if edge.props.get(prop) is None:
                errors.append(f"{edge.rel} {edge.src} -> {edge.dst}: missing {prop}")
        if edge.rel == Rel.EVIDENCED_BY:
            errors += _evidence_errors(edge)
        out_edges[edge.src].append(edge)

    for node_id, (label, props) in by_id.items():
        mine = out_edges[node_id]
        if label in CLAIM_LABELS and not any(e.rel == Rel.EVIDENCED_BY for e in mine):
            errors.append(f"{label} {node_id}: no EVIDENCED_BY")
        if label == Label.ITEM and sum(e.rel == Rel.IN_SOURCE for e in mine) != 1:
            errors.append(f"Item {node_id}: needs exactly one IN_SOURCE")
        if label == Label.PROJECTION:
            targets = [e.dst for e in mine if e.rel == Rel.SUMMARIZES]
            if targets != [props.get(Prop.ORIGIN_ID)]:
                errors.append(f"Projection {node_id}: SUMMARIZES must point at origin_id only")
        for edge in mine:
            if edge.rel != Rel.SUPERSEDES:
                continue
            if edge.dst == node_id:
                errors.append(f"{node_id}: SUPERSEDES itself")
                continue
            old = by_id[edge.dst][1]
            if old.get(Prop.STATUS) != Status.SUPERSEDED:
                errors.append(f"{edge.dst}: superseded by {node_id} but status is {old.get(Prop.STATUS)!r}")
            if not old.get(Prop.VALID_TO):
                errors.append(f"{edge.dst}: superseded but valid_to is missing")
    return errors


def _node_errors(tenant: str, node_id: str, label: str, props: dict) -> list[str]:
    errors = []
    if props.get(Prop.TENANT_ID) != tenant:
        errors.append(f"{node_id}: tenant_id is {props.get(Prop.TENANT_ID)!r}")
    for prop in REQUIRED_PROPS.get(label, ()):
        if props.get(prop) is None:
            errors.append(f"{label} {node_id}: missing {prop}")
    for prop in TIMESTAMP_PROPS:
        value = props.get(prop)
        if value is not None and not is_ts(value):
            errors.append(f"{node_id}: {prop} {value!r} is not a contract timestamp")
    if label in CLAIM_LABELS and props.get(Prop.STATUS) not in CLAIM_STATUSES:
        errors.append(f"{node_id}: status {props.get(Prop.STATUS)!r} not in {CLAIM_STATUSES}")
    if label in CLAIM_LABELS:
        confidence = props.get(Prop.CONFIDENCE)
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append(f"{node_id}: confidence must be between 0 and 1")
        if props.get(Prop.STATUS) == Status.ACTIVE and props.get(Prop.VALID_TO) is not None:
            errors.append(f"{node_id}: active claim has valid_to")
    if label == Label.COMMITMENT and props.get(Prop.PROGRESS) not in COMMITMENT_PROGRESS:
        errors.append(f"{node_id}: progress {props.get(Prop.PROGRESS)!r} not in {COMMITMENT_PROGRESS}")
    if label == Label.PROJECTION:
        if props.get(Prop.STATUS) not in PROJECTION_STATUSES:
            errors.append(f"{node_id}: status {props.get(Prop.STATUS)!r} not in {PROJECTION_STATUSES}")
        errors += _acl_list_errors(node_id, Prop.AUDIENCE, props.get(Prop.AUDIENCE))
        if props.get(Prop.LEVEL_FLOOR) not in LEVELS:
            errors.append(f"{node_id}: level_floor must be one of {LEVELS}")
    if label in (Label.ITEM, Label.SOURCE):
        errors += _acl_list_errors(node_id, Prop.ACL_GROUPS, props.get(Prop.ACL_GROUPS))
        if props.get(Prop.MIN_LEVEL) not in LEVELS:
            errors.append(f"{node_id}: min_level must be one of {LEVELS}")
    if label == Label.SOURCE and props.get(Prop.CASCADE_POLICY) not in CASCADE_POLICIES:
        errors.append(f"{node_id}: cascade_policy not in {CASCADE_POLICIES}")
    if label == Label.PERSON and props.get(Prop.LEVEL) not in LEVELS:
        errors.append(f"{node_id}: level must be one of {LEVELS}")
    return errors


def _acl_list_errors(node_id: str, prop: str, value) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f"{node_id}: {prop} must be a non-empty list"]
    return [f"{node_id}: bad acl string {group!r} in {prop}" for group in value if not is_acl(group)]


def _evidence_errors(edge: EdgeRow) -> list[str]:
    errors = []
    span = edge.props.get(Prop.SPAN)
    if not (isinstance(span, list) and len(span) == 2 and all(isinstance(v, int) for v in span) and span[0] < span[1]):
        errors.append(f"EVIDENCED_BY {edge.src} -> {edge.dst}: span must be [start, end]")
    confidence = edge.props.get(Prop.CONFIDENCE)
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        errors.append(f"EVIDENCED_BY {edge.src} -> {edge.dst}: confidence must be between 0 and 1")
    return errors


def export_graph(graph) -> tuple[list[NodeRow], list[EdgeRow]]:
    """Read a live FalkorDB graph into rows, read-only."""

    nodes = [
        NodeRow(tuple(row[0].labels), dict(row[0].properties))
        for row in graph.ro_query("MATCH (n) RETURN n").result_set
    ]
    edges = [
        EdgeRow(row[0], row[1], row[2], dict(row[3].properties))
        for row in graph.ro_query("MATCH (a)-[r]->(b) RETURN type(r), a.id, b.id, r").result_set
    ]
    return nodes, edges
