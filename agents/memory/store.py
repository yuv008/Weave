"""Agent memory: episodes, conversations, playbooks and tasks (``migrations/005_agent_memory.sql``).

The store only persists. Gating happens in ``agents.memory.recall`` and the
memory tools: every id read back is re-checked before anyone sees it.
"""

import json
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol

from shared.pg import use_tenant


def _json(value) -> str:
    return json.dumps(value, default=str)


def _forget_playbooks() -> None:
    from agents.answer.planner import clear_playbook_cache

    clear_playbook_cache()


class ConversationNotFound(LookupError):
    """No such conversation for this caller. Same error whether it exists or not."""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass
class Episode:
    tenant: str
    person_id: str
    agent: str
    question: str = ""
    note: str = ""
    node_ids: list[str] = field(default_factory=list)
    kind: str = "answer"
    intent: str | None = None
    tools: list[dict] = field(default_factory=list)
    outcome: str = "ok"
    conversation_id: str | None = None
    feedback: dict | None = None
    audience: str = "person"
    id: str = field(default_factory=lambda: new_id("ep"))
    created_at: datetime | None = None


@dataclass
class Conversation:
    id: str
    tenant: str
    person_id: str
    surface: str
    title: str = ""
    summary: str = ""


@dataclass
class Turn:
    role: str
    text: str
    node_ids: list[str] = field(default_factory=list)
    at: datetime | None = None


@dataclass
class Task:
    tenant: str
    kind: str
    state: str
    target_agent: str | None = None
    node_ids: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)
    created_by: str | None = None
    episode_id: str | None = None
    parent_id: str | None = None
    id: str = field(default_factory=lambda: new_id("task"))
    created_at: datetime | None = None
    claimed_at: datetime | None = None
    claimed_by: str | None = None


class MemoryStore(Protocol):
    def add_episode(self, episode: Episode) -> str: ...
    def get_episode(self, episode_id: str) -> Episode | None: ...
    def search_episodes(self, tenant: str, person_id: str, agent: str, query: str, limit: int) -> list[Episode]: ...
    def set_feedback(self, episode_id: str, feedback: dict) -> None: ...

    def create_conversation(self, tenant: str, person_id: str, surface: str, title: str,
                            conversation_id: str | None = None) -> str: ...
    def get_conversation(self, conversation_id: str) -> Conversation | None: ...
    def add_turn(self, conversation_id: str, role: str, text: str, node_ids: list[str]) -> None: ...
    def turns(self, conversation_id: str, limit: int) -> list[Turn]: ...

    def playbook(self, tenant: str, role: str, intent: str) -> list[str] | None: ...
    def put_playbook(self, tenant: str | None, role: str, intent: str, steps: list[str], approved_by: str | None) -> int: ...

    def add_task(self, task: Task, *, if_absent: bool = False) -> str | None:
        """The task id, or ``None`` when ``if_absent`` and a task with that id already exists."""
        ...
    def get_task(self, task_id: str) -> Task | None: ...
    def update_task(self, task_id: str, *, state: str, payload: dict | None = None) -> None: ...
    def claim_task(self, tenant: str, kind: str, agent: str, worker: str) -> Task | None: ...


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "be", "it", "we",
    "i", "me", "my", "our", "what", "which", "who", "why", "how", "do", "does", "did", "should", "with",
    "about", "this", "that", "from", "at", "by", "as", "us", "you", "your", "can", "will", "would",
}


def terms(text: str) -> list[str]:
    """Search terms: lowercase words, stop words removed, order kept, no repeats."""

    seen: list[str] = []
    for word in _WORD.findall((text or "").lower()):
        if word not in _STOP and word not in seen:
            seen.append(word)
    return seen


class InMemoryMemoryStore:
    """For offline tests. Same semantics as the Postgres store."""

    def __init__(self, clock=lambda: datetime.now(UTC)):
        self._clock = clock
        self._lock = threading.Lock()
        self.episodes: dict[str, Episode] = {}
        self.conversations: dict[str, Conversation] = {}
        self._turns: dict[str, list[Turn]] = {}
        self.playbooks: list[dict] = []
        self.tasks: dict[str, Task] = {}

    def add_episode(self, episode: Episode) -> str:
        with self._lock:
            self.episodes[episode.id] = replace(episode, created_at=episode.created_at or self._clock())
        return episode.id

    def get_episode(self, episode_id: str) -> Episode | None:
        found = self.episodes.get(episode_id)
        return replace(found) if found else None

    def search_episodes(self, tenant, person_id, agent, query, limit):
        wanted = set(terms(query))
        scored = []
        for ep in self.episodes.values():
            if (ep.tenant, ep.person_id, ep.agent) != (tenant, person_id, agent) or ep.audience != "person":
                continue
            score = len(wanted & set(terms(f"{ep.question} {ep.note}"))) if wanted else 1
            if score:
                scored.append((score, ep.created_at, ep))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [replace(ep) for _, _, ep in scored[:limit]]

    def set_feedback(self, episode_id, feedback):
        with self._lock:
            if episode_id in self.episodes:
                self.episodes[episode_id].feedback = dict(feedback)

    def create_conversation(self, tenant, person_id, surface, title, conversation_id=None):
        conv_id = conversation_id or new_id("conv")
        with self._lock:
            existing = self.conversations.get(conv_id)
            if existing is not None:
                if (existing.tenant, existing.person_id) != (tenant, person_id):
                    raise ConversationNotFound(conv_id)
                return conv_id
            self.conversations[conv_id] = Conversation(conv_id, tenant, person_id, surface, title[:200])
            self._turns[conv_id] = []
        return conv_id

    def get_conversation(self, conversation_id):
        found = self.conversations.get(conversation_id)
        return replace(found) if found else None

    def add_turn(self, conversation_id, role, text, node_ids):
        with self._lock:
            self._turns[conversation_id].append(Turn(role, text, list(node_ids), self._clock()))

    def turns(self, conversation_id, limit):
        return [replace(t) for t in self._turns.get(conversation_id, [])[-limit:]]

    def playbook(self, tenant, role, intent):
        for scope in (tenant, None):
            rows = [p for p in self.playbooks
                    if p["tenant"] == scope and p["role"] == role and p["intent"] == intent and p["active"]]
            if rows:
                return list(max(rows, key=lambda p: p["version"])["steps"])
        return None

    def put_playbook(self, tenant, role, intent, steps, approved_by):
        with self._lock:
            same = [p for p in self.playbooks if (p["tenant"], p["role"], p["intent"]) == (tenant, role, intent)]
            version = max((p["version"] for p in same), default=0) + 1
            self.playbooks.append({"tenant": tenant, "role": role, "intent": intent, "version": version,
                                   "steps": list(steps), "approved_by": approved_by, "active": True})
        _forget_playbooks()
        return version

    def add_task(self, task, *, if_absent=False):
        with self._lock:
            if if_absent and task.id in self.tasks:
                return None
            self.tasks[task.id] = replace(task, created_at=task.created_at or self._clock())
        return task.id

    def get_task(self, task_id):
        found = self.tasks.get(task_id)
        return replace(found, payload=dict(found.payload)) if found else None

    def update_task(self, task_id, *, state, payload=None):
        with self._lock:
            task = self.tasks[task_id]
            task.state = state
            if payload is not None:
                task.payload = {**task.payload, **payload}

    def claim_task(self, tenant, kind, agent, worker):
        with self._lock:
            ready = sorted(
                (t for t in self.tasks.values()
                 if (t.tenant, t.kind, t.target_agent, t.state) == (tenant, kind, agent, "ready")),
                key=lambda t: t.created_at,
            )
            if not ready:
                return None
            task = ready[0]
            task.state, task.claimed_at, task.claimed_by = "claimed", self._clock(), worker
            return replace(task)


_EPISODE_COLS = (
    "id, tenant_id, person_id, agent, question, note, node_ids, kind, intent, tools, outcome, "
    "conversation_id, feedback, audience, created_at"
)
_TASK_COLS = (
    "id, tenant_id, kind, state, target_agent, node_ids, payload, created_by, episode_id, parent_id, "
    "created_at, claimed_at, claimed_by"
)


def _episode(row) -> Episode:
    (id_, tenant, person, agent, question, note, node_ids, kind, intent, tools, outcome, conv, feedback,
     audience, at) = row
    return Episode(tenant, person, agent, question, note, list(node_ids or []), kind, intent, list(tools or []),
                   outcome, conv, feedback, audience or "person", id_, at)


def _task(row) -> Task:
    (id_, tenant, kind, state, agent, node_ids, payload, created_by, episode, parent, at, claimed_at, claimed_by) = row
    return Task(tenant, kind, state, agent, list(node_ids or []), dict(payload or {}), created_by, episode, parent,
                id_, at, claimed_at, claimed_by)


def _is_closed(conn) -> bool:
    """True when ``conn`` cannot be reused. Connections without ``closed`` are one-shot."""

    flag = getattr(conn, "closed", None)
    if flag is None:
        return True
    return bool(flag)


class PostgresMemoryStore:
    def __init__(self, connect):
        self._connect = connect
        self._lock = threading.Lock()
        self._conn = None

    def warm(self) -> None:
        """Open the kept connection. The first cue then does not wait on a new one."""

        self._run("SELECT 1")

    def _borrow(self):
        conn = self._conn
        if conn is not None and not _is_closed(conn):
            return conn, True
        conn = self._connect()
        keep = not _is_closed(conn)
        if keep:
            self._conn = conn
        return conn, keep

    def _drop(self, conn) -> None:
        if self._conn is conn:
            self._conn = None
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    def _run(self, sql: str, params=(), *, fetch: str | None = None, tenant: str | None = None):
        with self._lock:
            conn, keep = self._borrow()
            try:
                use_tenant(conn, tenant)
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    out = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
                conn.commit()
                return out
            except Exception:
                self._drop(conn)
                keep = False
                raise
            finally:
                if not keep:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def add_episode(self, episode: Episode) -> str:
        self._run(
            "INSERT INTO episodes (id, tenant_id, person_id, agent, question, note, node_ids, kind, intent, tools,"
            " outcome, conversation_id, feedback, audience)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s::text[],%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s)",
            (episode.id, episode.tenant, episode.person_id, episode.agent, episode.question, episode.note,
             list(episode.node_ids), episode.kind, episode.intent, _json(episode.tools), episode.outcome,
             episode.conversation_id, _json(episode.feedback) if episode.feedback is not None else None,
             episode.audience),
            tenant=episode.tenant,
        )
        return episode.id

    def get_episode(self, episode_id):
        row = self._run(f"SELECT {_EPISODE_COLS} FROM episodes WHERE id = %s", (episode_id,), fetch="one")
        return _episode(row) if row else None

    def search_episodes(self, tenant, person_id, agent, query, limit):
        words = terms(query)
        if not words:
            rows = self._run(
                f"SELECT {_EPISODE_COLS} FROM episodes WHERE tenant_id=%s AND person_id=%s AND agent=%s"
                " AND audience = 'person' ORDER BY created_at DESC LIMIT %s",
                (tenant, person_id, agent, limit), fetch="all", tenant=tenant,
            )
        else:
            rows = self._run(
                f"SELECT {_EPISODE_COLS} FROM episodes, to_tsquery('english', %s) q"
                " WHERE tenant_id=%s AND person_id=%s AND agent=%s AND audience = 'person' AND tsv @@ q"
                " ORDER BY ts_rank(tsv, q) DESC, created_at DESC LIMIT %s",
                (" | ".join(words), tenant, person_id, agent, limit), fetch="all", tenant=tenant,
            )
        return [_episode(r) for r in rows]

    def set_feedback(self, episode_id, feedback):
        self._run("UPDATE episodes SET feedback = %s::jsonb WHERE id = %s", (_json(feedback), episode_id))

    def create_conversation(self, tenant, person_id, surface, title, conversation_id=None):
        conv_id = conversation_id or new_id("conv")
        row = self._run(
            "INSERT INTO conversations (id, tenant_id, person_id, surface, title) VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (id) DO NOTHING RETURNING id",
            (conv_id, tenant, person_id, surface, title[:200]),
            fetch="one", tenant=tenant,
        )
        if row:
            return row[0]
        existing = self.get_conversation(conv_id)
        if existing and (existing.tenant, existing.person_id) == (tenant, person_id):
            return conv_id
        raise ConversationNotFound(conv_id)

    def get_conversation(self, conversation_id):
        row = self._run(
            "SELECT id, tenant_id, person_id, surface, title, summary FROM conversations WHERE id = %s",
            (conversation_id,), fetch="one",
        )
        return Conversation(*row) if row else None

    def add_turn(self, conversation_id, role, text, node_ids):
        self._run(
            "INSERT INTO conversation_turns (conversation_id, role, text, node_ids, tenant_id)"
            " VALUES (%s, %s, %s, %s::text[], (SELECT tenant_id FROM conversations WHERE id = %s))",
            (conversation_id, role, text, list(node_ids), conversation_id),
        )

    def turns(self, conversation_id, limit):
        rows = self._run(
            "SELECT role, text, node_ids, at FROM (SELECT * FROM conversation_turns WHERE conversation_id = %s"
            " ORDER BY id DESC LIMIT %s) t ORDER BY id",
            (conversation_id, limit), fetch="all",
        )
        return [Turn(role, text, list(ids or []), at) for role, text, ids, at in rows]

    def playbook(self, tenant, role, intent):
        row = self._run(
            "SELECT steps FROM playbooks WHERE (tenant_id = %s OR tenant_id IS NULL) AND role = %s AND intent = %s"
            " AND active ORDER BY (tenant_id IS NULL), version DESC LIMIT 1",
            (tenant, role, intent), fetch="one", tenant=tenant,
        )
        return list(row[0]) if row else None

    def put_playbook(self, tenant, role, intent, steps, approved_by):
        row = self._run(
            "INSERT INTO playbooks (tenant_id, role, intent, version, steps, approved_by)"
            " SELECT %s, %s, %s, coalesce(max(version), 0) + 1, %s::jsonb, %s FROM playbooks"
            " WHERE coalesce(tenant_id, '') = coalesce(%s, '') AND role = %s AND intent = %s RETURNING version",
            (tenant, role, intent, _json(list(steps)), approved_by, tenant, role, intent),
            fetch="one", tenant=tenant,
        )
        _forget_playbooks()
        return row[0]

    def add_task(self, task, *, if_absent=False):
        row = self._run(
            "INSERT INTO tasks (id, tenant_id, kind, state, target_agent, node_ids, payload, created_by, episode_id,"
            " parent_id) VALUES (%s,%s,%s,%s,%s,%s::text[],%s::jsonb,%s,%s,%s)"
            + (" ON CONFLICT (id) DO NOTHING" if if_absent else "") + " RETURNING id",
            (task.id, task.tenant, task.kind, task.state, task.target_agent, list(task.node_ids),
             _json(task.payload), task.created_by, task.episode_id, task.parent_id),
            fetch="one", tenant=task.tenant,
        )
        return row[0] if row else None

    def get_task(self, task_id):
        row = self._run(f"SELECT {_TASK_COLS} FROM tasks WHERE id = %s", (task_id,), fetch="one")
        return _task(row) if row else None

    def update_task(self, task_id, *, state, payload=None):
        self._run(
            "UPDATE tasks SET state = %s, payload = payload || %s::jsonb, updated_at = now() WHERE id = %s",
            (state, _json(payload or {}), task_id),
        )

    def claim_task(self, tenant, kind, agent, worker):
        row = self._run(
            f"UPDATE tasks SET state = 'claimed', claimed_at = now(), claimed_by = %s, updated_at = now()"
            f" WHERE id = (SELECT id FROM tasks WHERE tenant_id = %s AND kind = %s AND target_agent = %s"
            f" AND state = 'ready' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)"
            f" RETURNING {_TASK_COLS}",
            (worker, tenant, kind, agent), fetch="one", tenant=tenant,
        )
        return _task(row) if row else None
