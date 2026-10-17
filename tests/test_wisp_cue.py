"""POST /wisp/cue offline: company chunk, personal note, validation, auth, no writes.

Search runs through the real ``search_context`` path. The chunk table is an
in-memory stand-in over the fixture, and item citations come from the fixture
graph, so the test does not need Postgres, FalkorDB, or Groq.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from agents.gate.rules import item_is_visible
from agents.graph import ReadOnlyGraph
from agents.memory.store import Episode, terms
from agents.mcp.server import build_app
from agents.wisp.cue import _source_from_id
from shared.acl import ORG_WIDE
from shared.schema import Label, Prop, Rel
from tests.answer_support import SubjectGraph, offline_runtime
from tests.gate_support import NOW

TOKEN = "tok-sam"
AUTH = {"authorization": f"Bearer {TOKEN}"}
COMPANY_PROMPT = "enterprise SSO is on track"
PERSONAL_PROMPT = "offsite lunch"
UNRELATED_PROMPT = "xylophone recital"
QUESTION_MARKER = "STORED_QUESTION_MARKER"
NOTE_MARKER = "PERSONAL_NOTE_MARKER"
PERSONAL_NOTE = "bring the vegetarian tray to the offsite lunch"
PERSONAL_ID = "ep_offsite"


class CueGraph(SubjectGraph):
    """Subject matching, plus the item-citation query ``search_context`` asks."""

    def __init__(self, fx):
        super().__init__(fx)
        self.fx = fx

    def ro_query(self, query, params=None):
        params = params or {}
        if "i.excerpt" in query and "UNWIND $ids AS iid" in query:
            return SimpleNamespace(result_set=self._item_citations(params))
        return super().ro_query(query, params)

    def _item_citations(self, params):
        wanted = set(params.get("ids") or [])
        rows = []
        for node in self.fx.nodes:
            if node.id not in wanted or Label.ITEM not in node.labels:
                continue
            if not item_is_visible(node.props, params["gate_level"], params["gate_groups"], params["gate_fresh"]):
                continue
            props = node.props
            rows.append([
                node.id,
                props.get(Prop.EXCERPT),
                _name(self.fx, _dst(self.fx, node.id, Rel.AUTHORED_BY)),
                props.get(Prop.OCCURRED_AT),
                _name(self.fx, _dst(self.fx, node.id, Rel.IN_SOURCE)),
                props.get(Prop.SOURCE_URL),
            ])
        return rows


class ChunkConnection:
    """Keyword half of ``chunk`` for one request. No writes."""

    def __init__(self, fx):
        self.fx = fx
        self.closed = False

    def cursor(self):
        return ChunkCursor(self.fx)

    def close(self):
        self.closed = True


class ChunkCursor:
    def __init__(self, fx):
        self.fx = fx
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        if "plainto_tsquery" not in compact:
            raise AssertionError("cue search issued an unexpected statement")
        limit = 1 if "LIMIT 1" in compact else int((params or {}).get("limit") or 8)
        self._rows = _keyword_hits(self.fx, params or {}, limit=limit)

    def fetchall(self):
        return list(self._rows)

    def close(self):
        return None


def _dst(fx, src, rel):
    for edge in fx.edges:
        if edge.src == src and edge.rel == rel:
            return edge.dst
    return None


def _name(fx, node_id):
    if not node_id:
        return None
    return fx.node(node_id).props.get(Prop.NAME)


def _chunk_visible(chunk, params):
    if chunk["min_level"] > params["gate_level"]:
        return False
    acl = set(chunk["acl_groups"])
    groups = set(params["gate_groups"])
    allowed = set(params["gate_fresh"]) | {ORG_WIDE}
    prefix = str(params["prefix"]).replace("%", "")
    item_id = chunk["item_id"]
    return bool(acl) and acl <= allowed and bool(acl & groups) and item_id.startswith(prefix)


def _keyword_hits(fx, params, limit=8):
    wanted = set(terms(params.get("query") or ""))
    scored = []
    for chunk in fx.chunks:
        if not wanted or not _chunk_visible(chunk, params):
            continue
        score = len(wanted & set(terms(chunk["text"])))
        if score:
            scored.append((score, chunk["item_id"], chunk["text"]))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [(item_id, text) for _, item_id, text in scored[:limit]]


def _add_episode(env, episode_id, *, note, question, person="sam", audience="person", created_at=None):
    env.runtime.memory.add_episode(Episode(
        env.fx.tenant, env.fx.ids[person], "answer",
        question=question, note=note, audience=audience, id=episode_id, created_at=created_at,
    ))


def _size(env):
    memory = env.runtime.memory
    turns = sum(len(rows) for rows in memory._turns.values())
    return (len(memory.episodes), len(memory.conversations), turns, len(memory.tasks))


def _client(env):
    return TestClient(build_app(env.runtime), base_url="http://127.0.0.1:8765")


def _post(client, prompt, headers=AUTH):
    return client.post("/wisp/cue", json={"prompt": prompt}, headers=headers)


@pytest.fixture()
def env():
    env = offline_runtime()
    env.runtime.pg_connect = lambda: ChunkConnection(env.fx)
    env.runtime.open_graph = lambda tenant: ReadOnlyGraph(CueGraph(env.fx), f"co:{tenant}")
    env.store.add_token(TOKEN, env.fx.ids["sam"])
    return env


def _company_item(env):
    return env.fx.ids["item_eng_public_update"]


def _source_name(env, item_id):
    return _name(env.fx, _dst(env.fx, item_id, Rel.IN_SOURCE))


def test_cue_returns_the_best_chunk_when_several_match(env):
    fx = env.fx
    template = next(c for c in fx.chunks if c["item_id"] == fx.ids["item_eng_public_update"])
    weak_id = f"{fx.tenant}:slack:message:CWEAK:1.0"
    strong_id = f"{fx.tenant}:gdrive:document:DSTRONG:1"
    fx.chunks.append({**template, "item_id": weak_id, "text": "Weave pilot notes"})
    fx.chunks.append({
        **template,
        "item_id": strong_id,
        "text": "Weave Mock Acme pilot kickoff notes Drive Notion ingestion",
    })
    prompt = "Weave Mock Acme pilot kickoff notes"
    with _client(env) as client:
        response = _post(client, prompt)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "found"
    assert body["item_id"] == strong_id
    assert "kickoff" in body["quote"]
    assert body["source"] == "gdrive"


def test_a_visible_company_chunk_beats_a_personal_note(env):
    _add_episode(
        env, "ep_company_note",
        question=f"{QUESTION_MARKER} {COMPANY_PROMPT}",
        note=f"{NOTE_MARKER} {COMPANY_PROMPT}",
    )
    before = _size(env)
    with _client(env) as client:
        response = _post(client, COMPANY_PROMPT)
    assert response.status_code == 200
    body = response.json()
    item_id = _company_item(env)
    source = _source_from_id(item_id)
    chunk = next(c["text"] for c in env.fx.chunks if c["item_id"] == item_id)
    assert set(body) == {"state", "summary", "quote", "source", "item_id"}
    assert body["state"] == "found"
    assert body["item_id"] == item_id
    assert body["source"] == source and source
    assert body["summary"] == f"This came up in {source}"
    assert body["quote"] and " ".join(chunk.split()).startswith(body["quote"])
    assert NOTE_MARKER not in body["quote"] and QUESTION_MARKER not in body["quote"]
    assert body["source"] != "Personal note" and body["item_id"] != "ep_company_note"
    assert _size(env) == before


def test_an_unrelated_prompt_is_unsure(env):
    _add_episode(env, PERSONAL_ID, question=f"{QUESTION_MARKER} {PERSONAL_PROMPT}", note=PERSONAL_NOTE)
    before = _size(env)
    with _client(env) as client:
        response = _post(client, UNRELATED_PROMPT)
    assert response.status_code == 200
    assert response.json() == {"state": "unsure", "summary": "", "quote": "", "source": "", "item_id": ""}
    assert _size(env) == before


def test_a_personal_note_is_used_when_search_finds_nothing(env):
    _add_episode(
        env, "ep_empty_question",
        question=f"{QUESTION_MARKER} {PERSONAL_PROMPT}",
        note="",
        created_at=NOW,
    )
    _add_episode(
        env, "ep_channel",
        question=PERSONAL_PROMPT,
        note="channel copy of the offsite lunch",
        audience="slack:C0ENGPUB",
        created_at=NOW,
    )
    _add_episode(
        env, "ep_priya",
        question=PERSONAL_PROMPT,
        note="priya private offsite lunch",
        person="priya",
        created_at=NOW,
    )
    _add_episode(
        env, PERSONAL_ID,
        question=f"{QUESTION_MARKER} where is the {PERSONAL_PROMPT}",
        note=PERSONAL_NOTE,
        created_at=NOW - timedelta(seconds=1),
    )
    before = _size(env)
    with _client(env) as client:
        response = _post(client, PERSONAL_PROMPT)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"state", "summary", "quote", "source", "item_id"}
    assert body["state"] == "found"
    assert body["source"] == "Personal note"
    assert body["item_id"] == PERSONAL_ID
    assert body["summary"] == "This came up in a personal note"
    assert body["quote"] == PERSONAL_NOTE
    assert QUESTION_MARKER not in body["quote"]
    assert _size(env) == before


def test_empty_and_overlong_prompts_are_rejected(env):
    before = _size(env)
    with _client(env) as client:
        rejected = [
            client.post("/wisp/cue", json={"prompt": ""}, headers=AUTH),
            client.post("/wisp/cue", json={"prompt": "   "}, headers=AUTH),
            client.post("/wisp/cue", json={}, headers=AUTH),
            client.post("/wisp/cue", json={"prompt": "a" * 1501}, headers=AUTH),
        ]
        accepted = _post(client, "z" * 1500)
    for response in rejected:
        assert response.status_code == 400
        assert response.text == "prompt must be 1-1500 characters"
    assert accepted.status_code == 200
    assert accepted.json()["state"] == "unsure"
    assert _size(env) == before


def test_cue_does_not_write_an_episode_or_turn(env):
    _add_episode(
        env, PERSONAL_ID,
        question=f"{QUESTION_MARKER} where is the {PERSONAL_PROMPT}",
        note=PERSONAL_NOTE,
    )
    before = _size(env)
    with _client(env) as client:
        found = _post(client, COMPANY_PROMPT)
        personal = _post(client, PERSONAL_PROMPT)
        unsure = _post(client, UNRELATED_PROMPT)
    assert found.status_code == 200 and found.json()["state"] == "found"
    assert personal.status_code == 200 and personal.json()["item_id"] == PERSONAL_ID
    assert unsure.status_code == 200 and unsure.json()["state"] == "unsure"
    assert _size(env) == before


def test_missing_bearer_token_matches_answer(env):
    before = _size(env)
    with _client(env) as client:
        cue = client.post("/wisp/cue", json={"prompt": COMPANY_PROMPT})
        answer = client.post("/answer", json={"question": "x"})
    assert cue.status_code == answer.status_code == 401
    assert cue.text == answer.text == "not authorized"
    assert cue.headers["www-authenticate"] == answer.headers["www-authenticate"] == "Bearer"
    assert _size(env) == before
