"""The demo company, built exactly to ``shared.schema``.

Cast: Priya (Sales AE, 1), Rohan (Sales director, 3), Dev (backend, 1),
Maya (Eng manager, 2), Sam (Eng intern, 0). Customer: Acme Corp.

Story in the graph:
- D1 "Provider A for SSO" (Oct 3) is superseded by D2 "Provider B" (Oct 12).
- Priya's commitment to Acme (C1) depends on D1, so impact_of(D1) finds her.
- D2 is also on the org-wide roadmap issue, so Sales can see the switch.
- The Q4 budget figure (F1) is evidenced in #leadership (min_level 2) and
  the leadership budget sheet (min_level 3); it supersedes a September
  draft figure (F0).
- C1 depends on D1 and on the SSO project; the Acme deal needs the
  project. Each dependency edge carries ``evidence_ids``.
- Leadership's Q4 priority (D3) reaches everyone as approved projection P1.
- Engineering's switch (D2) reaches #deal-acme as approved projection P2.
- P3 is a draft projection of F1; the gate must hide it.
- One unprocessed ChangeEvent: D1 superseded by D2.

The first five sources reproduce the visibility matrix on page 15 of the
architecture doc (public eng channel, eng team repo, #eng-leads,
#leadership, leadership budget sheet). #eng-sso, #deal-acme, Sales deals
and the roadmap repo carry the story.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from shared.acl import ORG_WIDE, acl_group, parse_acl
from shared.config import WeaveConfig, load_config, source_acl
from shared.ids import (
    claim_id,
    document_id,
    entity_id,
    github_issue_id,
    item_id,
    person_id,
    projection_id,
    slack_channel_id,
    slack_message_id,
    source_id,
)
from shared.schema import (
    Label,
    Progress,
    ProjectionStatus,
    Prop,
    Rel,
    Status,
)
from shared.timefmt import to_ts

EXTRACTOR_VERSION = "fixture-1"
SLACK_TEAM = "T0DEMO"
SLACK_DOMAIN = "weave-demo"
NOTION_WORKSPACE = "W0NOTION"
ENG_REPO = "demo-org/platform"
ROADMAP_REPO = "demo-org/roadmap"
BUDGET_FOLDER = "F0BUDGET"


@dataclass(frozen=True)
class Node:
    labels: tuple[str, ...]
    props: dict

    @property
    def id(self) -> str:
        return self.props[Prop.ID]

    @property
    def label(self) -> str:
        return self.labels[0]


@dataclass(frozen=True)
class Edge:
    rel: str
    src: str
    dst: str
    props: dict = field(default_factory=dict)


@dataclass
class Fixture:
    tenant: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    memberships: list[dict] = field(default_factory=list)
    source_sync: list[dict] = field(default_factory=list)
    change_events: list[dict] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)
    ids: dict[str, str] = field(default_factory=dict)

    def node(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def node_by_key(self, key: str) -> Node:
        return self.node(self.ids[key])


def _t(month: int, day: int, hour: int = 9, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def _slack_ts(moment: datetime) -> str:
    return f"{int(moment.timestamp())}.000100"


def _slack_version(ts: str) -> str:
    seconds, _, fraction = ts.partition(".")
    return f"{int(seconds):016d}.{fraction.ljust(6, '0')}"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(tenant: str = "acme", config: WeaveConfig | None = None) -> Fixture:
    cfg = config or load_config()
    fx = Fixture(tenant=tenant)
    add = fx.nodes.append

    def link(rel: str, src: str, dst: str, **props) -> None:
        fx.edges.append(Edge(rel, src, dst, props))

    # Org
    sales = entity_id(tenant, Label.DEPARTMENT, "Sales")
    eng = entity_id(tenant, Label.DEPARTMENT, "Engineering")
    add(Node((Label.DEPARTMENT,), {Prop.ID: sales, Prop.TENANT_ID: tenant, Prop.NAME: "Sales", Prop.KIND: "department"}))
    add(Node((Label.DEPARTMENT,), {Prop.ID: eng, Prop.TENANT_ID: tenant, Prop.NAME: "Engineering", Prop.KIND: "department"}))
    sales_team = entity_id(tenant, Label.TEAM, "Enterprise Sales")
    platform = entity_id(tenant, Label.TEAM, "Platform")
    add(Node((Label.TEAM,), {Prop.ID: sales_team, Prop.TENANT_ID: tenant, Prop.NAME: "Enterprise Sales", Prop.KIND: "team"}))
    add(Node((Label.TEAM,), {Prop.ID: platform, Prop.TENANT_ID: tenant, Prop.NAME: "Platform", Prop.KIND: "team"}))
    link(Rel.PART_OF, sales_team, sales)
    link(Rel.PART_OF, platform, eng)

    people = {
        "priya": ("Priya", "priya@weave-demo.example", 1, "Sales", "U0PRIYA", "priya-k", "notion-priya", sales_team),
        "rohan": ("Rohan", "rohan@weave-demo.example", 3, "Sales", "U0ROHAN", "rohan-m", "notion-rohan", sales_team),
        "dev": ("Dev", "dev@weave-demo.example", 1, "Engineering", "U0DEV", "dev-s", "notion-dev", platform),
        "maya": ("Maya", "maya@weave-demo.example", 2, "Engineering", "U0MAYA", "maya-r", "notion-maya", platform),
        "sam": ("Sam", "sam@weave-demo.example", 0, "Engineering", "U0SAM", "sam-intern", "notion-sam", platform),
    }
    pid = {}
    for key, (name, email, level, dept, slack, gh, notion, team) in people.items():
        pid[key] = person_id(tenant, email)
        fx.ids[key] = pid[key]
        add(Node((Label.PERSON,), {
            Prop.ID: pid[key], Prop.TENANT_ID: tenant, Prop.NAME: name, Prop.EMAIL: email,
            Prop.LEVEL: level, Prop.DEPT: dept, Prop.SLACK_ID: slack, Prop.GITHUB_LOGIN: gh,
            Prop.NOTION_USER: notion, Prop.PROVISIONAL: False,
        }))
        link(Rel.MEMBER_OF, pid[key], team)
    link(Rel.REPORTS_TO, pid["priya"], pid["rohan"])
    link(Rel.REPORTS_TO, pid["dev"], pid["maya"])
    link(Rel.REPORTS_TO, pid["sam"], pid["maya"])

    # Subjects
    sso = entity_id(tenant, Label.TOPIC, "SSO")
    project = entity_id(tenant, Label.PROJECT, "SSO integration")
    acme = entity_id(tenant, Label.CUSTOMER, "Acme Corp")
    deal = entity_id(tenant, Label.DEAL, "Acme renewal")
    fx.ids.update(topic_sso=sso, project_sso=project, customer_acme=acme, deal_acme=deal)
    add(Node((Label.TOPIC,), {Prop.ID: sso, Prop.TENANT_ID: tenant, Prop.NAME: "SSO", Prop.ALIASES: ["single sign-on", "saml login"], Prop.PROVISIONAL: False}))
    add(Node((Label.PROJECT,), {Prop.ID: project, Prop.TENANT_ID: tenant, Prop.NAME: "SSO integration", Prop.STATUS: "in_progress", Prop.ALIASES: ["sso-service", ENG_REPO], Prop.PROVISIONAL: False}))
    add(Node((Label.CUSTOMER,), {Prop.ID: acme, Prop.TENANT_ID: tenant, Prop.NAME: "Acme Corp", Prop.PROVISIONAL: False}))
    add(Node((Label.DEAL,), {Prop.ID: deal, Prop.TENANT_ID: tenant, Prop.NAME: "Acme renewal", Prop.STAGE: "negotiation", Prop.RENEWAL_DATE: "2026-12-01", Prop.PROVISIONAL: False}))
    link(Rel.FOR, deal, acme)
    link(Rel.OWNS, pid["priya"], deal)
    link(Rel.OWNS, pid["dev"], project)
    link(Rel.OWNS, pid["dev"], sso)
    link(Rel.KNOWS_ABOUT, pid["dev"], sso, score=0.9)
    link(Rel.KNOWS_ABOUT, pid["maya"], sso, score=0.6)

    # Sources and memberships. The first five rows are the visibility matrix on
    # page 15 of the architecture doc; the rest carry the demo story.
    eng_pub = acl_group("slack", "C0ENGPUB")
    eng_repo = acl_group("github", ENG_REPO)
    eng_leads = acl_group("slack", "C0ENGLEADS")
    leaders = acl_group("slack", "C0LEADERS")
    budget = acl_group("gdrive", BUDGET_FOLDER)
    eng_sso = acl_group("slack", "C0ENGSSO")
    deal_ch = acl_group("slack", "C0DEALACME")
    notion = acl_group("notion", "sales-deals")
    roadmap = acl_group("github", ROADMAP_REPO)
    channel_labels = (Label.SOURCE, Label.CONVERSATION)
    repo_labels = (Label.SOURCE, Label.REPOSITORY)
    sources = {
        "src_eng_public": (eng_pub, channel_labels, "slack", slack_channel_id(tenant, SLACK_TEAM, "C0ENGPUB"), "C0ENGPUB", ()),
        "src_eng_repo": (eng_repo, repo_labels, "github", source_id(tenant, "github", "repo", ENG_REPO), ENG_REPO, ("sam", "dev", "maya")),
        "src_eng_leads": (eng_leads, channel_labels, "slack", slack_channel_id(tenant, SLACK_TEAM, "C0ENGLEADS"), "C0ENGLEADS", ("maya",)),
        "src_leadership": (leaders, channel_labels, "slack", slack_channel_id(tenant, SLACK_TEAM, "C0LEADERS"), "C0LEADERS", ("maya", "rohan")),
        "src_budget": (budget, (Label.SOURCE,), "gdrive", source_id(tenant, "gdrive", "folder", BUDGET_FOLDER), BUDGET_FOLDER, ("maya", "rohan")),
        "src_eng_sso": (eng_sso, channel_labels, "slack", slack_channel_id(tenant, SLACK_TEAM, "C0ENGSSO"), "C0ENGSSO", ("dev", "maya", "sam")),
        "src_deal": (deal_ch, channel_labels, "slack", slack_channel_id(tenant, SLACK_TEAM, "C0DEALACME"), "C0DEALACME", ("priya", "rohan")),
        "src_notion": (notion, (Label.SOURCE, Label.TEAMSPACE), "notion", source_id(tenant, "notion", "teamspace", NOTION_WORKSPACE, "sales-deals"), "sales-deals", ("priya", "rohan")),
        "src_roadmap": (roadmap, repo_labels, "github", source_id(tenant, "github", "repo", ROADMAP_REPO), ROADMAP_REPO, ()),
    }
    src_id = {}
    for key, (group, labels, system, node_id, external, members) in sources.items():
        policy = cfg.sources[group]
        src_id[group] = node_id
        fx.ids[key] = node_id
        add(Node(labels, {
            Prop.ID: node_id, Prop.TENANT_ID: tenant, Prop.SOURCE: system, Prop.EXTERNAL_ID: external,
            Prop.NAME: policy.name, Prop.ACL_GROUPS: source_acl(group, policy), Prop.MIN_LEVEL: policy.min_level,
            Prop.CASCADE_POLICY: policy.cascade_policy, Prop.AUTHORITY: policy.authority,
        }))
        for member in members:
            fx.memberships.append({"person_id": pid[member], "acl_group": group, "source_system": system})
        fx.source_sync.append({"source_id": node_id, "acl_group": group})

    # Items
    def item(key: str, labels: tuple[str, ...], group: str, author: str, moment: datetime, text: str, *,
             node_id: str, external: str, object_type: str, url: str, raw_id: str, version: str,
             title: str | None = None, parent: str | None = None) -> str:
        policy = cfg.sources[group]
        acl = source_acl(group, policy)
        props = {
            Prop.ID: node_id, Prop.TENANT_ID: tenant, Prop.SOURCE: parse_acl(group)[0], Prop.EXTERNAL_ID: external,
            Prop.OBJECT_TYPE: object_type, Prop.OCCURRED_AT: to_ts(moment), Prop.SOURCE_URL: url,
            Prop.EXCERPT: text[:280], Prop.CONTENT_HASH: _hash(text), Prop.VERSION: version,
            Prop.DELETED: False, Prop.RAW_ID: raw_id, Prop.ACL_GROUPS: acl, Prop.MIN_LEVEL: policy.min_level,
        }
        if title:
            props[Prop.TITLE] = title
        add(Node(labels, props))
        link(Rel.IN_SOURCE, node_id, src_id[group])
        link(Rel.AUTHORED_BY, node_id, pid[author])
        if parent:
            link(Rel.REPLIES_TO, node_id, parent)
        fx.chunks.append({"item_id": node_id, "text": text, "acl_groups": acl, "min_level": policy.min_level})
        fx.ids[key] = node_id
        return node_id

    def slack_item(key: str, group: str, author: str, moment: datetime, text: str, parent: str | None = None) -> str:
        channel = parse_acl(group)[1]
        ts = _slack_ts(moment)
        return item(
            key, (Label.ITEM, Label.MESSAGE), group, author, moment, text,
            node_id=slack_message_id(tenant, channel, ts), external=f"{channel}:{ts}", object_type="message",
            url=f"https://{SLACK_DOMAIN}.slack.com/archives/{channel}/p{ts.replace('.', '')}",
            raw_id=f"raw:slack:{channel}:{ts}", version=_slack_version(ts), parent=parent,
        )

    i_pub = slack_item("item_eng_public_update", eng_pub, "dev", _t(10, 13, 10, 0),
                       "Heads up: enterprise SSO is on track with Provider B. Progress is tracked in the roadmap issue.")
    i_eng1 = slack_item("item_eng_choose_a", eng_sso, "dev", _t(10, 3, 10, 15),
                        "Proposal for the SSO endpoint: we go with Provider A. Their SAML setup is the quickest for the Acme timeline.")
    i_eng1r = slack_item("item_eng_agree_a", eng_sso, "maya", _t(10, 3, 10, 40),
                         "Agreed, Provider A it is. Dev owns the integration.", parent=i_eng1)
    i_eng2 = slack_item("item_eng_switch_b", eng_sso, "dev", _t(10, 12, 9, 30),
                        "Update: Provider A is out. Their SAML support cannot do per-tenant certificates. We are moving SSO to Provider B.")
    slack_item("item_eng_leads_risk", eng_leads, "maya", _t(10, 12, 16, 0),
               "Eng leads: the Provider B switch adds about a week. I will flag the Acme date risk to Sales.")
    i_deal = slack_item("item_deal_promise", deal_ch, "priya", _t(10, 5, 14, 0),
                        "Confirmed with Acme on the call: SSO will be live for them by Nov 15, ahead of the renewal.")
    i_lead_budget_draft = slack_item("item_leadership_budget_draft", leaders, "rohan", _t(9, 15, 8, 0),
                                     "Draft Q4 platform budget: $200k, pending the October review.")
    i_lead_budget = slack_item("item_leadership_budget", leaders, "rohan", _t(10, 1, 8, 0),
                               "Q4 platform budget is locked at $250k. Please do not share outside this channel.")
    i_lead_priority = slack_item("item_leadership_priority", leaders, "rohan", _t(10, 2, 8, 30),
                                 "Q4 priority: SSO ships before any new feature work. Maya, please make sure the team knows.")

    pr_moment = _t(10, 12, 15, 0)
    item("item_eng_repo_pr", (Label.ITEM,), eng_repo, "dev", pr_moment,
         "Switch the SSO integration to Provider B. Replaces the Provider A SAML client.",
         node_id=item_id(tenant, "github", "pull", ENG_REPO, "7"), external=f"{ENG_REPO}#7",
         object_type="pull_request", url=f"https://github.com/{ENG_REPO}/pull/7",
         raw_id=f"raw:github:{ENG_REPO}#7", version=to_ts(pr_moment), title="Switch SSO to Provider B")

    sheet_moment = _t(10, 1, 7, 0)
    i_sheet = item("item_budget_sheet", (Label.ITEM, Label.DOCUMENT), budget, "rohan", sheet_moment,
                   "Q4 leadership budget. Platform: $250,000. Sales enablement: $80,000.",
                   node_id=document_id(tenant, "gdrive", BUDGET_FOLDER, "q4-budget-sheet"), external="q4-budget-sheet",
                   object_type="document", url="https://docs.google.com/spreadsheets/d/q4-budget-sheet",
                   raw_id="raw:gdrive:q4-budget-sheet", version=to_ts(sheet_moment), title="Q4 leadership budget")

    issue_moment = _t(10, 12, 11, 0)
    i_gh = item("item_roadmap_issue", (Label.ITEM, Label.ISSUE), roadmap, "dev", issue_moment,
                "SSO integration. Status: in progress. Provider switched from A to B on Oct 12; target date under review.",
                node_id=github_issue_id(tenant, ROADMAP_REPO, 42), external=f"{ROADMAP_REPO}#42",
                object_type="issue", url=f"https://github.com/{ROADMAP_REPO}/issues/42",
                raw_id=f"raw:github:{ROADMAP_REPO}#42", version=to_ts(issue_moment), title="SSO integration")
    link(Rel.LINKS_TO, i_gh, i_eng2)

    notion_moment = _t(10, 6, 12, 0)
    i_notion = item("item_notion_deal", (Label.ITEM, Label.DOCUMENT), notion, "priya", notion_moment,
                    "Acme Corp renewal. ARR $120,000. Renewal date Dec 1. Blocker: SSO must be live before renewal.",
                    node_id=document_id(tenant, "notion", NOTION_WORKSPACE, "acme-renewal-page"),
                    external="acme-renewal-page", object_type="document",
                    url="https://www.notion.so/acme-renewal-page", raw_id="raw:notion:acme-renewal-page",
                    version=to_ts(notion_moment), title="Acme Corp renewal")
    link(Rel.MENTIONS, i_notion, acme)

    for mentioning in (i_pub, i_eng1, i_eng2, i_gh):
        link(Rel.MENTIONS, mentioning, sso)

    # Claims
    def claim(key: str, label: str, evidence: str, statement: str, props: dict) -> str:
        node_id = claim_id(tenant, label, evidence, statement)
        base = {
            Prop.ID: node_id, Prop.TENANT_ID: tenant, Prop.EXTRACTOR_VERSION: EXTRACTOR_VERSION,
        }
        if label != Label.FIGURE:
            base[Prop.STATEMENT] = statement
        add(Node((label,), {**base, **props}))
        fx.ids[key] = node_id
        return node_id

    def evidence(claim_node: str, item: str, quote: str, confidence: float) -> None:
        text = next(c["text"] for c in fx.chunks if c["item_id"] == item)
        start = text.index(quote)
        link(Rel.EVIDENCED_BY, claim_node, item, quote=quote, span=[start, start + len(quote)], confidence=confidence)

    d1 = claim("decision_provider_a", Label.DECISION, i_eng1, "Use Provider A for SSO.", {
        Prop.STATUS: Status.SUPERSEDED, Prop.VALID_FROM: to_ts(_t(10, 3, 10, 40)),
        Prop.VALID_TO: to_ts(_t(10, 12, 9, 30)), Prop.CONFIDENCE: 0.92,
        Prop.LAST_VERIFIED_AT: to_ts(_t(10, 3, 10, 40)),
    })
    evidence(d1, i_eng1, "we go with Provider A", 0.85)
    evidence(d1, i_eng1r, "Agreed, Provider A it is.", 0.95)
    link(Rel.ABOUT, d1, sso)
    link(Rel.MADE_BY, d1, pid["dev"])

    d2 = claim("decision_provider_b", Label.DECISION, i_eng2, "Use Provider B for SSO.", {
        Prop.STATUS: Status.ACTIVE, Prop.VALID_FROM: to_ts(_t(10, 12, 9, 30)), Prop.CONFIDENCE: 0.9,
        Prop.LAST_VERIFIED_AT: to_ts(_t(10, 12, 11, 0)),
    })
    evidence(d2, i_eng2, "We are moving SSO to Provider B.", 0.93)
    evidence(d2, i_gh, "Provider switched from A to B on Oct 12", 0.8)
    link(Rel.ABOUT, d2, sso)
    link(Rel.MADE_BY, d2, pid["dev"])
    link(Rel.SUPERSEDES, d2, d1)

    c1 = claim("commitment_acme_sso", Label.COMMITMENT, i_deal, "SSO will be live for Acme by Nov 15.", {
        Prop.STATUS: Status.ACTIVE, Prop.DUE: "2026-11-15", Prop.PROGRESS: Progress.OPEN,
        Prop.VALID_FROM: to_ts(_t(10, 5, 14, 0)), Prop.CONFIDENCE: 0.88,
        Prop.LAST_VERIFIED_AT: to_ts(_t(10, 6, 12, 0)),
    })
    evidence(c1, i_deal, "SSO will be live for them by Nov 15", 0.9)
    link(Rel.DEPENDS_ON, c1, d1, evidence_ids=[i_deal])
    link(Rel.DEPENDS_ON, c1, project, evidence_ids=[i_deal])
    link(Rel.NEEDS, deal, project, evidence_ids=[i_notion])
    link(Rel.PROMISED_TO, c1, acme)
    link(Rel.OWNS, pid["priya"], c1)

    f1 = claim("figure_q4_budget", Label.FIGURE, i_lead_budget, "Q4 platform budget", {
        Prop.NAME: "Q4 platform budget", Prop.VALUE: 250000, Prop.UNIT: "USD",
        Prop.AS_OF: to_ts(_t(10, 1, 8, 0)), Prop.STATUS: Status.ACTIVE,
        Prop.VALID_FROM: to_ts(_t(10, 1, 8, 0)), Prop.CONFIDENCE: 0.95,
        Prop.LAST_VERIFIED_AT: to_ts(_t(10, 1, 8, 0)),
    })
    evidence(f1, i_lead_budget, "Q4 platform budget is locked at $250k.", 0.95)
    evidence(f1, i_sheet, "Platform: $250,000", 0.97)
    link(Rel.ABOUT, f1, project)

    f0 = claim("figure_q4_budget_draft", Label.FIGURE, i_lead_budget_draft, "Q4 platform budget", {
        Prop.NAME: "Q4 platform budget", Prop.VALUE: 200000, Prop.UNIT: "USD",
        Prop.AS_OF: to_ts(_t(9, 15, 8, 0)), Prop.STATUS: Status.SUPERSEDED,
        Prop.VALID_FROM: to_ts(_t(9, 15, 8, 0)), Prop.VALID_TO: to_ts(_t(10, 1, 8, 0)),
        Prop.CONFIDENCE: 0.8, Prop.LAST_VERIFIED_AT: to_ts(_t(9, 15, 8, 0)),
    })
    evidence(f0, i_lead_budget_draft, "$200k", 0.8)
    link(Rel.ABOUT, f0, project)
    link(Rel.SUPERSEDES, f1, f0)

    f2 = claim("figure_acme_arr", Label.FIGURE, i_notion, "Acme renewal ARR", {
        Prop.NAME: "Acme renewal ARR", Prop.VALUE: 120000, Prop.UNIT: "USD",
        Prop.AS_OF: to_ts(_t(10, 6, 12, 0)), Prop.STATUS: Status.ACTIVE,
        Prop.VALID_FROM: to_ts(_t(10, 6, 12, 0)), Prop.CONFIDENCE: 0.9,
        Prop.LAST_VERIFIED_AT: to_ts(_t(10, 6, 12, 0)),
    })
    evidence(f2, i_notion, "ARR $120,000", 0.9)
    link(Rel.ABOUT, f2, deal)

    d3 = claim("decision_q4_priority", Label.DECISION, i_lead_priority, "SSO ships before any new feature work in Q4.", {
        Prop.STATUS: Status.ACTIVE, Prop.VALID_FROM: to_ts(_t(10, 2, 8, 30)), Prop.CONFIDENCE: 0.9,
        Prop.LAST_VERIFIED_AT: to_ts(_t(10, 2, 8, 30)),
    })
    evidence(d3, i_lead_priority, "SSO ships before any new feature work", 0.92)
    link(Rel.ABOUT, d3, project)
    link(Rel.MADE_BY, d3, pid["rohan"])

    # Projections
    def projection(key: str, origin: str, audience: list[str], level_floor: int, status: str, summary: str) -> None:
        node_id = projection_id(tenant, origin, audience)
        add(Node((Label.PROJECTION,), {
            Prop.ID: node_id, Prop.TENANT_ID: tenant, Prop.SUMMARY: summary, Prop.AUDIENCE: audience,
            Prop.LEVEL_FLOOR: level_floor, Prop.ORIGIN_ID: origin, Prop.STATUS: status,
        }))
        link(Rel.SUMMARIZES, node_id, origin)
        fx.ids[key] = node_id

    projection("projection_q4_priority", d3, [ORG_WIDE], 0, ProjectionStatus.APPROVED,
               "Leadership set SSO as the top Q4 priority, ahead of new feature work.")
    projection("projection_provider_switch", d2, [deal_ch], 0, ProjectionStatus.APPROVED,
               "Engineering switched the SSO provider from A to B on Oct 12. Delivery dates are being re-checked.")
    projection("projection_budget_draft", f1, [ORG_WIDE], 0, ProjectionStatus.DRAFT,
               "Leadership has set the Q4 platform budget.")

    # Outbox
    fx.change_events.append({
        "id": f"{d1}:superseded", "fact_id": d1, "kind": "superseded", "new_fact_id": d2,
        "created_at": to_ts(_t(10, 12, 9, 31)), "processed": False,
    })
    return fx
