"""Frozen graph contract for ``co:{tenant}`` and ``u:{tenant}:{user}``.

Queries never spell a label, edge type, property or status by hand; they
import these constants. Changes to this file need both builders.

Middle-ground rules agreed with the connector code in ``app/``:
- Node ids keep the existing readable format (see ``shared.ids``).
- Source-specific labels stay. A Slack message is ``:Message:Item``, a
  Notion or Drive page is ``:Document:Item``, a Slack channel is
  ``:Conversation:Source``. Agents query only the contract labels.
- Existing property names are kept: ``source`` (the system), ``source_url``,
  ``occurred_at``, ``excerpt``, ``content_hash``, ``version``, ``deleted``.
- Timestamps are strings from ``shared.timefmt.to_ts``.
- Claims share one lifecycle ``status``. A Commitment's delivery state is
  ``progress``, so "current" is always ``status = 'active'``.
"""


class Label:
    PERSON = "Person"
    TEAM = "Team"
    DEPARTMENT = "Department"
    TOPIC = "Topic"
    PROJECT = "Project"
    CUSTOMER = "Customer"
    DEAL = "Deal"
    DECISION = "Decision"
    COMMITMENT = "Commitment"
    FIGURE = "Figure"
    PROJECTION = "Projection"
    ITEM = "Item"
    SOURCE = "Source"
    # Source-specific labels carried alongside Item and Source.
    MESSAGE = "Message"
    DOCUMENT = "Document"
    ISSUE = "Issue"
    CONVERSATION = "Conversation"
    REPOSITORY = "Repository"
    TEAMSPACE = "Teamspace"


ENTITY_LABELS = (
    Label.PERSON,
    Label.TEAM,
    Label.DEPARTMENT,
    Label.TOPIC,
    Label.PROJECT,
    Label.CUSTOMER,
    Label.DEAL,
)
CLAIM_LABELS = (Label.DECISION, Label.COMMITMENT, Label.FIGURE)
EVIDENCE_LABELS = (Label.ITEM, Label.SOURCE)
CONTRACT_LABELS = ENTITY_LABELS + CLAIM_LABELS + (Label.PROJECTION,) + EVIDENCE_LABELS
SOURCE_SPECIFIC_LABELS = (
    Label.MESSAGE,
    Label.DOCUMENT,
    Label.ISSUE,
    Label.CONVERSATION,
    Label.REPOSITORY,
    Label.TEAMSPACE,
)


class Rel:
    MEMBER_OF = "MEMBER_OF"
    PART_OF = "PART_OF"
    REPORTS_TO = "REPORTS_TO"
    OWNS = "OWNS"
    KNOWS_ABOUT = "KNOWS_ABOUT"
    ABOUT = "ABOUT"
    MADE_BY = "MADE_BY"
    SUPERSEDES = "SUPERSEDES"
    NEEDS = "NEEDS"
    DEPENDS_ON = "DEPENDS_ON"
    FOR = "FOR"
    PROMISED_TO = "PROMISED_TO"
    BLOCKED_BY = "BLOCKED_BY"
    EVIDENCED_BY = "EVIDENCED_BY"
    IN_SOURCE = "IN_SOURCE"
    AUTHORED_BY = "AUTHORED_BY"
    MENTIONS = "MENTIONS"
    LINKS_TO = "LINKS_TO"
    REPLIES_TO = "REPLIES_TO"
    SUMMARIZES = "SUMMARIZES"


class Prop:
    ID = "id"
    TENANT_ID = "tenant_id"
    NAME = "name"
    PROVISIONAL = "provisional"

    # Person
    EMAIL = "email"
    LEVEL = "level"
    DEPT = "dept"
    SLACK_ID = "slack_id"
    GITHUB_LOGIN = "github_login"
    NOTION_USER = "notion_user"

    # Org units, projects, topics, sales
    KIND = "kind"
    STATUS = "status"
    ALIASES = "aliases"
    STAGE = "stage"
    RENEWAL_DATE = "renewal_date"

    # Claims
    STATEMENT = "statement"
    VALID_FROM = "valid_from"
    VALID_TO = "valid_to"
    CONFIDENCE = "confidence"
    LAST_VERIFIED_AT = "last_verified_at"
    EXTRACTOR_VERSION = "extractor_version"
    DUE = "due"
    PROGRESS = "progress"
    VALUE = "value"
    UNIT = "unit"
    AS_OF = "as_of"

    # Projection
    SUMMARY = "summary"
    AUDIENCE = "audience"
    LEVEL_FLOOR = "level_floor"
    ORIGIN_ID = "origin_id"

    # Item and Source (names match app/graph/upsert*.py)
    SOURCE = "source"
    EXTERNAL_ID = "external_id"
    OBJECT_TYPE = "object_type"
    TITLE = "title"
    OCCURRED_AT = "occurred_at"
    SOURCE_URL = "source_url"
    EXCERPT = "excerpt"
    CONTENT_HASH = "content_hash"
    VERSION = "version"
    DELETED = "deleted"
    RAW_ID = "raw_id"
    ACL_GROUPS = "acl_groups"
    MIN_LEVEL = "min_level"
    CASCADE_POLICY = "cascade_policy"
    AUTHORITY = "authority"

    # Edge properties
    QUOTE = "quote"
    SPAN = "span"
    SCORE = "score"
    EVIDENCE_IDS = "evidence_ids"


class Status:
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


CLAIM_STATUSES = (Status.ACTIVE, Status.SUPERSEDED, Status.RETRACTED)


class Progress:
    OPEN = "open"
    AT_RISK = "at_risk"
    DONE = "done"


COMMITMENT_PROGRESS = (Progress.OPEN, Progress.AT_RISK, Progress.DONE)


class ProjectionStatus:
    DRAFT = "draft"
    APPROVED = "approved"


PROJECTION_STATUSES = (ProjectionStatus.DRAFT, ProjectionStatus.APPROVED)


class CascadePolicy:
    BROADCAST = "broadcast"
    MANUAL = "manual"


CASCADE_POLICIES = (CascadePolicy.BROADCAST, CascadePolicy.MANUAL)

LEVELS = (0, 1, 2, 3)
"""0 intern, 1 individual contributor, 2 manager, 3 director."""


_CLAIM_COMMON = (
    Prop.STATUS,
    Prop.VALID_FROM,
    Prop.CONFIDENCE,
    Prop.LAST_VERIFIED_AT,
    Prop.EXTRACTOR_VERSION,
)

REQUIRED_PROPS: dict[str, tuple[str, ...]] = {
    Label.PERSON: (Prop.NAME, Prop.EMAIL),
    Label.TEAM: (Prop.NAME, Prop.KIND),
    Label.DEPARTMENT: (Prop.NAME, Prop.KIND),
    Label.TOPIC: (Prop.NAME, Prop.ALIASES),
    Label.PROJECT: (Prop.NAME, Prop.STATUS, Prop.ALIASES),
    Label.CUSTOMER: (Prop.NAME,),
    Label.DEAL: (Prop.NAME, Prop.STAGE, Prop.RENEWAL_DATE),
    Label.DECISION: (Prop.STATEMENT,) + _CLAIM_COMMON,
    Label.COMMITMENT: (Prop.STATEMENT, Prop.DUE, Prop.PROGRESS) + _CLAIM_COMMON,
    Label.FIGURE: (Prop.NAME, Prop.VALUE, Prop.UNIT, Prop.AS_OF) + _CLAIM_COMMON,
    Label.PROJECTION: (
        Prop.SUMMARY,
        Prop.AUDIENCE,
        Prop.LEVEL_FLOOR,
        Prop.ORIGIN_ID,
        Prop.STATUS,
    ),
    Label.ITEM: (
        Prop.SOURCE,
        Prop.EXTERNAL_ID,
        Prop.OBJECT_TYPE,
        Prop.OCCURRED_AT,
        Prop.SOURCE_URL,
        Prop.CONTENT_HASH,
        Prop.DELETED,
        Prop.ACL_GROUPS,
        Prop.MIN_LEVEL,
    ),
    Label.SOURCE: (
        Prop.SOURCE,
        Prop.EXTERNAL_ID,
        Prop.NAME,
        Prop.ACL_GROUPS,
        Prop.MIN_LEVEL,
        Prop.CASCADE_POLICY,
        Prop.AUTHORITY,
    ),
}

TIMESTAMP_PROPS = (
    Prop.VALID_FROM,
    Prop.VALID_TO,
    Prop.LAST_VERIFIED_AT,
    Prop.OCCURRED_AT,
    Prop.AS_OF,
)

_CLAIMS = set(CLAIM_LABELS)
_ORG_SUBJECTS = {Label.TOPIC, Label.PROJECT, Label.CUSTOMER}

EDGE_ENDPOINTS: dict[str, tuple[set[str], set[str]]] = {
    Rel.MEMBER_OF: ({Label.PERSON}, {Label.TEAM}),
    Rel.PART_OF: ({Label.TEAM}, {Label.DEPARTMENT}),
    Rel.REPORTS_TO: ({Label.PERSON}, {Label.PERSON}),
    Rel.OWNS: ({Label.PERSON}, {Label.PROJECT, Label.COMMITMENT, Label.TOPIC, Label.DEAL, Label.CUSTOMER}),
    Rel.KNOWS_ABOUT: ({Label.PERSON}, {Label.TOPIC, Label.PROJECT}),
    Rel.ABOUT: ({Label.DECISION, Label.FIGURE}, {Label.TOPIC, Label.PROJECT, Label.DEAL}),
    Rel.MADE_BY: ({Label.DECISION}, {Label.PERSON}),
    Rel.SUPERSEDES: (
        {Label.DECISION, Label.FIGURE, Label.COMMITMENT},
        {Label.DECISION, Label.FIGURE, Label.COMMITMENT},
    ),
    Rel.NEEDS: ({Label.DEAL}, {Label.PROJECT}),
    Rel.DEPENDS_ON: ({Label.COMMITMENT}, {Label.DECISION, Label.PROJECT, Label.COMMITMENT}),
    Rel.FOR: ({Label.DEAL}, {Label.CUSTOMER}),
    Rel.PROMISED_TO: ({Label.COMMITMENT}, {Label.CUSTOMER}),
    Rel.BLOCKED_BY: ({Label.PROJECT}, {Label.PROJECT, Label.DECISION}),
    Rel.EVIDENCED_BY: (_CLAIMS, {Label.ITEM}),
    Rel.IN_SOURCE: ({Label.ITEM}, {Label.SOURCE}),
    Rel.AUTHORED_BY: ({Label.ITEM}, {Label.PERSON}),
    Rel.MENTIONS: ({Label.ITEM}, {Label.PERSON} | _ORG_SUBJECTS),
    Rel.LINKS_TO: ({Label.ITEM}, {Label.ITEM}),
    Rel.REPLIES_TO: ({Label.ITEM}, {Label.ITEM}),
    Rel.SUMMARIZES: ({Label.PROJECTION}, _CLAIMS),
}

DEPENDENCY_RELS = (Rel.NEEDS, Rel.DEPENDS_ON, Rel.BLOCKED_BY)
"""Edges between work items. Each carries ``evidence_ids``: the Items that justify it."""

REQUIRED_EDGE_PROPS: dict[str, tuple[str, ...]] = {
    Rel.EVIDENCED_BY: (Prop.QUOTE, Prop.SPAN, Prop.CONFIDENCE),
    Rel.KNOWS_ABOUT: (Prop.SCORE,),
    **{rel: (Prop.EVIDENCE_IDS,) for rel in DEPENDENCY_RELS},
}


def company_graph(tenant: str) -> str:
    return f"co:{tenant}"


def private_graph(tenant: str, user: str) -> str:
    return f"u:{tenant}:{user}"
