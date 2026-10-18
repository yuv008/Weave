"""Wake word, quiet drafts, and the submit hook for the desktop pet."""

import importlib.util
import io
import json
import queue
import sys
import threading
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[1] / "cursor-wisp" / "desktop"
HOOK_PATH = Path(__file__).resolve().parents[1] / ".cursor" / "hooks" / "wisp_prompt.py"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import bubble
import pet


def _load_hook():
    spec = importlib.util.spec_from_file_location("wisp_prompt_hook", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


class _Stdin:
    def __init__(self, raw):
        self.buffer = io.BytesIO(raw)


def _reset():
    pet.asked_questions.clear()
    with pet.suggest_state.lock:
        pet.suggest_state.seq = 0
        pet.suggest_state.pending = False
        pet.suggest_state.open = False
        pet.suggest_state.turn_ended = False
        pet.suggest_state.summary = ""
        pet.suggest_state.quote = ""
        pet.suggest_state.source = ""
        pet.suggest_state.item_id = ""
        pet.suggest_state.mail_insert = ""
        pet.suggest_state.mail_mode = ""
        pet.suggest_state.reading = False
        pet.suggest_state.ignored.clear()
    while True:
        try:
            pet.ui_queue.get_nowait()
        except queue.Empty:
            break


def _join_asks():
    for thread in threading.enumerate():
        if thread.name == "wisp-ask" and thread is not threading.current_thread():
            thread.join(timeout=2)


@pytest.fixture
def watch(monkeypatch):
    monkeypatch.setattr(pet.DraftWatch, "_loop", lambda self: None)
    monkeypatch.setattr(pet, "log", lambda message: None)
    _reset()
    watcher = pet.DraftWatch()
    yield watcher
    _join_asks()
    _reset()
    pet.draft_watch = None


def test_bubble_present():
    assert bubble.bubble_question("@bubble what's the API key used") == "what's the API key used"
    assert bubble.bubble_question("  @bubble   what's   the   API   key   used  ") == "what's the API key used"


def test_bubble_absent():
    assert bubble.bubble_question("fix the login timeout") is None
    assert bubble.bubble_question("@bubblegum is a different word") is None
    assert bubble.bubble_question("write to foo@bubble later") is None
    assert bubble.bubble_question("") is None


def test_bubble_case():
    assert bubble.bubble_question("@BUBBLE what's the API key used") == "what's the API key used"
    assert bubble.bubble_question("@BuBbLe who owns billing") == "who owns billing"


def test_bubble_token_in_the_middle():
    assert (
        bubble.bubble_question("what did the lead AI engineer discuss in teams @bubble")
        == "what did the lead AI engineer discuss in teams"
    )
    assert bubble.bubble_question("what did @bubble the lead discuss in teams") == "what did the lead discuss in teams"


def test_bubble_mention_without_question():
    assert bubble.bubble_question("@bubble") is None
    assert bubble.bubble_question("  @BUBBLE  ") is None
    assert bubble.bubble_question("@bubble @BUBBLE") is None
    assert bubble.bubble_question("bubble") is None


def test_purple_ranges_are_only_the_bubble_token():
    assert bubble.PURPLE == "#7C3AED"
    assert bubble.bubble_purple_ranges("@bubble what's the API key used") == [(0, 7)]
    assert bubble.bubble_purple_ranges("  @Bubble   who") == [(2, 9)]
    assert bubble.bubble_purple_ranges("@BUBBLE") == [(0, 7)]
    assert bubble.bubble_purple_ranges("@bubble then @BUBBLE") == [(0, 7), (13, 20)]
    assert bubble.bubble_purple_ranges("@bubblegum is a different word") == []
    assert bubble.bubble_purple_ranges("write to foo@bubble later") == []
    assert bubble.bubble_purple_ranges("ask @codebase and @files please") == []
    assert bubble.bubble_purple_ranges("bubble what's the API key used") == []
    assert bubble.bubble_purple_ranges("@bubble,") == []
    assert bubble.bubble_purple_ranges("") == []
    assert bubble.bubble_purple_ranges(None) == []
    mixed = "@codebase @bubble @docs"
    assert bubble.bubble_purple_ranges(mixed) == [(10, 17)]
    start, end = bubble.bubble_purple_ranges(mixed)[0]
    assert mixed[start:end] == "@bubble"


def test_purple_ranges_include_the_at_and_chip_marks():
    marked = "@\u200bbubble who owns billing"
    assert bubble.bubble_purple_ranges(marked) == [(0, 8)]
    assert marked[0:8].replace("\u200b", "") == "@bubble"
    trailing = "@bubble\u200b next"
    assert bubble.bubble_purple_ranges(trailing) == [(0, 7)]
    assert bubble.bubble_purple_ranges("@bubble\ufffc who") == [(0, 7)]
    text = "@Bubble who owns billing"
    assert bubble.bubble_question(text) == "who owns billing"
    assert text[slice(*bubble.bubble_purple_ranges(text)[0])].casefold() == "@bubble"


def test_js_purple_ranges_match_python():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    samples = [
        "@bubble what's the API key used",
        "  @Bubble   who",
        "@BUBBLE",
        "@bubble then @BUBBLE",
        "@bubblegum is a different word",
        "write to foo@bubble later",
        "ask @codebase and @files please",
        "bubble what's the API key used",
        "@bubble,",
        "",
        "@\u200bbubble who owns billing",
        "@bubble\u200b next",
        "@bubble\ufffc who",
        "@codebase @bubble @docs",
        "what did @bubble the lead discuss",
    ]
    script = DESKTOP / "bubble_tint.js"
    raw = subprocess.check_output(
        [
            node,
            "-e",
            "const {bubblePurpleRanges}=require(process.argv[1]);"
            "const samples=JSON.parse(process.argv[2]);"
            "process.stdout.write(JSON.stringify(samples.map(bubblePurpleRanges)));",
            str(script),
            json.dumps(samples),
        ],
        text=True,
    )
    assert json.loads(raw) == [list(map(list, bubble.bubble_purple_ranges(sample))) for sample in samples]
    pieces = subprocess.check_output(
        [
            node,
            "-e",
            "const {bubbleStylePieces}=require(process.argv[1]);"
            "const samples=JSON.parse(process.argv[2]);"
            "process.stdout.write(JSON.stringify(samples.map(bubbleStylePieces)));",
            str(script),
            json.dumps(["see @bubble now", "@BUBBLE", "no token", "@file and @bubble"]),
        ],
        text=True,
    )
    assert json.loads(pieces) == [
        [
            {"text": "see ", "purple": False},
            {"text": "@bubble", "purple": True},
            {"text": " now", "purple": False},
        ],
        [{"text": "@BUBBLE", "purple": True}],
        [{"text": "no token", "purple": False}],
        [
            {"text": "@file and ", "purple": False},
            {"text": "@bubble", "purple": True},
        ],
    ]


def test_accept_replaces_only_the_bubble_token():
    answer = "sk-weave-demo-4k7Q"
    assert bubble.replace_bubble_sentence(
        "rn we are using the openai api key can you change it to @bubble",
        answer,
    ) == "rn we are using the openai api key can you change it to " + answer
    assert bubble.replace_bubble_sentence("@bubble what's the API key used", answer) == answer + " what's the API key used"
    assert bubble.replace_bubble_sentence(
        "Fix login. @bubble what's the API key used",
        answer,
    ) == "Fix login. " + answer + " what's the API key used"
    assert bubble.replace_bubble_sentence(
        "@bubble what's the API key used. Then restart.",
        answer,
    ) == answer + " what's the API key used. Then restart."
    assert bubble.replace_bubble_sentence(
        "Keep this\nchange the key to @bubble\nAnd this",
        answer,
    ) == "Keep this\nchange the key to " + answer + "\nAnd this"
    assert bubble.replace_bubble_sentence("@bubble one. @BUBBLE two.", answer) == answer + " one. @BUBBLE two."
    assert bubble.replace_bubble_sentence("no wake word here", answer) is None
    assert bubble.replace_bubble_sentence("@bubblegum stays", answer) is None
    assert bubble.replace_bubble_sentence("write to foo@bubble later", answer) is None
    assert bubble.replace_bubble_sentence("@bubble, please", answer) is None
    assert bubble.replace_bubble_sentence("@bubble what's the API key used", "   ") is None
    assert bubble.replace_bubble_sentence("", answer) is None


def test_js_sentence_replacement_matches_python():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    samples = [
        ("rn we are using the openai api key can you change it to @bubble", "sk-weave-demo-4k7Q"),
        ("@bubble what's the API key used", "sk-weave-demo-4k7Q"),
        ("Fix login. @bubble what's the API key used", "sk-weave-demo-4k7Q"),
        ("@bubble what's the API key used. Then restart.", "sk-weave-demo-4k7Q"),
        ("Keep this\nchange the key to @bubble\nAnd this", "sk-weave-demo-4k7Q"),
        ("@bubble one. @BUBBLE two.", "sk-weave-demo-4k7Q"),
        ("no wake word here", "The API key is GROQ_API_KEY."),
        ("@bubblegum stays", "The API key is GROQ_API_KEY."),
        ("write to foo@bubble later", "The API key is GROQ_API_KEY."),
        ("@bubble, please", "The API key is GROQ_API_KEY."),
        ("@\u200bbubble who owns billing", "Sam owns billing."),
        ("@bubble what's the API key used", "   "),
    ]
    script = DESKTOP / "bubble_tint.js"
    raw = subprocess.check_output(
        [
            node,
            "-e",
            "const {replaceBubbleSentenceText}=require(process.argv[1]);"
            "const samples=JSON.parse(process.argv[2]);"
            "process.stdout.write(JSON.stringify(samples.map(([text, answer]) => replaceBubbleSentenceText(text, answer))));",
            str(script),
            json.dumps(samples),
        ],
        text=True,
    )
    assert json.loads(raw) == [bubble.replace_bubble_sentence(text, answer) for text, answer in samples]


def test_replace_sentence_uses_the_live_composer(watch, monkeypatch):
    seen = []
    monkeypatch.setattr(pet, "_composer_replace", lambda answer: seen.append(answer) or True)
    assert watch.replace_sentence("  The API key is GROQ_API_KEY.  ") is True
    assert seen == ["The API key is GROQ_API_KEY."]


def test_replace_sentence_falls_back_to_the_composer_value(watch, monkeypatch):
    monkeypatch.setattr(pet, "_composer_replace", lambda answer: False)
    monkeypatch.setattr(
        watch,
        "read_value",
        lambda: "can you change the api key used in this project to @bubble",
    )
    sent = []
    monkeypatch.setattr(watch, "_call", lambda command, timeout: sent.append((command, timeout)) or "set")
    assert watch.replace_sentence("The API key is GROQ_API_KEY.") is True
    command, timeout = sent[0]
    assert timeout == 12
    assert command.startswith("set ")
    import base64
    assert base64.b64decode(command[4:]).decode("utf-8") == (
        "can you change the api key used in this project to The API key is GROQ_API_KEY."
    )


def test_bubble_chip_forms():
    assert bubble.bubble_question("@bubble\u200b what's the API key used") == "what's the API key used"
    assert bubble.bubble_question("@\u200bbubble who owns billing") == "who owns billing"
    assert bubble.bubble_question("@bubble\ufffc who owns billing") == "who owns billing"
    assert bubble.bubble_question("bubble what's the API key used") == "what's the API key used"
    assert bubble.bubble_question("bubblegum is a different word") is None


def test_empty_composer_does_not_retire_bubble_card(watch, monkeypatch):
    def fail_retire():
        raise AssertionError("retired")

    monkeypatch.setattr(pet, "retire_suggestion", fail_retire)
    with pet.suggest_state.lock:
        pet.suggest_state.open = True
        pet.suggest_state.pending = True
        pet.suggest_state.summary = "still here"
    watch._consider("")
    watch._consider("   ")
    with pet.suggest_state.lock:
        assert pet.suggest_state.open is True
        assert pet.suggest_state.pending is True
        assert pet.suggest_state.summary == "still here"
    assert pet.ui_queue.empty()


def test_non_mention_does_not_call_answer(watch, monkeypatch):
    def fail_post(*_args, **_kwargs):
        raise AssertionError("POST /answer")

    monkeypatch.setattr(watch, "_post_json", fail_post)
    watch._consider("please refactor the login timeout handling")
    watch._consider("@bubblegum is not the wake word")
    assert pet.ui_queue.empty()


def test_bubble_draft_posts_stripped_question(watch, monkeypatch):
    calls = []

    def fake_post(url, payload, timeout):
        calls.append((url, payload, timeout))
        return {"answer": "Sam owns billing.", "claims": [], "evidence": {}}

    monkeypatch.setattr(watch, "_post_json", fake_post)
    monkeypatch.setattr(watch, "_answer_url", lambda: "http://127.0.0.1:8765/answer")
    monkeypatch.setattr(watch, "read", lambda: "@bubble who owns billing")
    assert watch.submit("@bubble who owns billing") is True
    _join_asks()
    assert calls == [("http://127.0.0.1:8765/answer", {"question": "who owns billing"}, 120)]
    kind, payload = pet.ui_queue.get_nowait()
    assert kind == "show"
    assert payload["summary"] == "Sam owns billing."
    assert payload["quote"] == ""
    assert payload["source"] == ""


def test_same_question_is_not_sent_again(watch, monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), name="", daemon=False):
            started.append(args)

        def start(self):
            return None

    monkeypatch.setattr(pet.threading, "Thread", FakeThread)
    assert watch.submit("@bubble who owns billing") is True
    assert watch.submit("who owns billing", duplicate_log=True) is False
    assert started == [("who owns billing",)]


def test_cleared_composer_still_shows_the_answer(watch, monkeypatch):
    card = {
        "answer": "They discussed the rollout.",
        "claims": [{"evidence": ["e1"]}],
        "evidence": {"e1": {"quote": "ship friday", "source": "teams"}},
    }
    monkeypatch.setattr(watch, "_post_json", lambda *_args, **_kwargs: card)
    monkeypatch.setattr(watch, "_answer_url", lambda: "http://127.0.0.1:8765/answer")
    monkeypatch.setattr(watch, "read", lambda: "")
    watch._fetch_and_show("what did the lead discuss")
    kind, payload = pet.ui_queue.get_nowait()
    assert kind == "show"
    assert payload["summary"] == "They discussed the rollout."
    assert payload["quote"] == "ship friday"
    assert payload["source"] == "teams"
    assert payload["item_id"] == "e1"


def test_plain_composer_still_shows_the_answer(watch, monkeypatch):
    card = {"answer": "Sam owns billing.", "claims": [], "evidence": {}}
    monkeypatch.setattr(watch, "_post_json", lambda *_args, **_kwargs: card)
    monkeypatch.setattr(watch, "_answer_url", lambda: "http://127.0.0.1:8765/answer")
    monkeypatch.setattr(watch, "read", lambda: "who owns billing")
    watch._fetch_and_show("who owns billing")
    kind, payload = pet.ui_queue.get_nowait()
    assert kind == "show"
    assert payload["summary"] == "Sam owns billing."


def test_changed_question_drops_the_reply(watch, monkeypatch):
    with pet.suggest_state.lock:
        pet.suggest_state.open = True
        pet.suggest_state.summary = "kept"
    monkeypatch.setattr(watch, "read", lambda: "@bubble what did the other person decide")
    monkeypatch.setattr(
        watch,
        "_post_json",
        lambda *_args, **_kwargs: {"answer": "Stale.", "claims": [], "evidence": {}},
    )
    monkeypatch.setattr(watch, "_answer_url", lambda: "http://127.0.0.1:8765/answer")
    watch._fetch_and_show("who owns billing")
    with pet.suggest_state.lock:
        assert pet.suggest_state.open is True
        assert pet.suggest_state.summary == "kept"
    assert pet.ui_queue.empty()


def test_failed_answer_shows_when_nothing_is_open(watch, monkeypatch):
    monkeypatch.setattr(watch, "_post_json", lambda *_args, **_kwargs: watch._fail("timeout"))
    monkeypatch.setattr(watch, "_answer_url", lambda: "http://127.0.0.1:8765/answer")
    monkeypatch.setattr(watch, "read", lambda: "")
    watch._fetch_and_show("who owns billing")
    kind, payload = pet.ui_queue.get_nowait()
    assert kind == "show"
    assert payload["summary"] == "Weave didn't answer in time."


def test_failed_answer_leaves_open_card(watch, monkeypatch):
    with pet.suggest_state.lock:
        pet.suggest_state.open = True
        pet.suggest_state.summary = "kept"
    monkeypatch.setattr(watch, "_post_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(watch, "_answer_url", lambda: "http://127.0.0.1:8765/answer")
    monkeypatch.setattr(watch, "read", lambda: "")
    watch._fetch_and_show("who owns billing")
    watch._fetch_and_show("who owns billing")
    monkeypatch.setattr(
        watch,
        "_post_json",
        lambda *_args, **_kwargs: {"answer": "   ", "claims": [], "evidence": {}},
    )
    watch._fetch_and_show("who owns billing")
    with pet.suggest_state.lock:
        assert pet.suggest_state.open is True
        assert pet.suggest_state.summary == "kept"
    assert pet.ui_queue.empty()


def test_answer_url_uses_the_cue_host(watch, monkeypatch):
    watch._cue_url = "http://127.0.0.1:8765/wisp/cue"
    watch._token = "present"
    monkeypatch.setattr(watch, "_load_token", lambda: watch._token)
    assert watch._answer_url() == "http://127.0.0.1:8765/answer"


def test_ask_endpoint_returns_before_submit(watch, monkeypatch):
    order = []

    class Handler:
        def _empty(self, code):
            order.append(("empty", code))

    def record(text, duplicate_log=False):
        order.append(("submit", text, duplicate_log))
        return True

    monkeypatch.setattr(watch, "submit", record)
    pet.draft_watch = watch
    body = json.dumps({"question": "who owns billing"}).encode("utf-8")
    pet.SuggestHandler._bubble_ask(Handler(), body)
    assert order == [("empty", 204), ("submit", "who owns billing", True)]


def test_normal_prompt_does_not_post(monkeypatch):
    posts = []
    notes = []
    monkeypatch.setattr(hook, "post_json", lambda *args, **kwargs: posts.append(args))
    monkeypatch.setattr(hook, "note", lambda message: notes.append(message))
    monkeypatch.setattr(hook.sys, "stdin", _Stdin(json.dumps({"prompt": "fix the login timeout"}).encode()))
    assert hook.run_prompt() == {"continue": True}
    assert posts == []
    assert notes == []


def test_bubble_prompt_forwards_without_waiting(monkeypatch):
    posts = []
    monkeypatch.setattr(hook, "pet_port", lambda: 4321)
    monkeypatch.setattr(hook, "post_json", lambda *args, **kwargs: posts.append(args) or (204, b""))
    monkeypatch.setattr(hook, "note", lambda message: None)
    prompt = "what did the lead AI engineer discuss in teams @bubble"
    monkeypatch.setattr(hook.sys, "stdin", _Stdin(json.dumps({"prompt": prompt}).encode()))
    assert hook.run_prompt() == {"continue": True}
    assert len(posts) == 1
    url, payload, timeout = posts[0]
    assert url == "http://127.0.0.1:4321/ask"
    assert payload == {"question": "what did the lead AI engineer discuss in teams"}
    assert timeout == 2
    assert "/wisp/cue" not in url
    assert "/answer" not in url


def test_bubble_mention_without_question_does_not_post(monkeypatch):
    posts = []
    notes = []
    monkeypatch.setattr(hook, "post_json", lambda *args, **kwargs: posts.append(args))
    monkeypatch.setattr(hook, "note", lambda message: notes.append(message))
    monkeypatch.setattr(hook.sys, "stdin", _Stdin(json.dumps({"prompt": "@bubble"}).encode()))
    assert hook.run_prompt() == {"continue": True}
    assert posts == []
    assert notes == []
