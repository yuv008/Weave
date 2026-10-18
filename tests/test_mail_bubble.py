"""Which tokens in a Gmail compose body turn Weave purple."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[1] / "cursor-wisp" / "desktop"
GMAIL = Path(__file__).resolve().parents[1] / "cursor-wisp" / "gmail"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import bubble


SAMPLES = [
    "@bubble email the sales team this quarter's profit margin",
    "our margin this quarter is @bubble",
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


def _mail_tint():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    raw = subprocess.check_output(
        [
            node,
            "-e",
            "const api=require(process.argv[1]);"
            "const samples=JSON.parse(process.argv[2]);"
            "process.stdout.write(JSON.stringify({"
            "purple:api.WEAVE_PURPLE,"
            "ranges:samples.map(api.bubblePurpleRanges),"
            "tokens:samples.map(api.mailPurpleTokens)"
            "}));",
            str(GMAIL / "mail_tint.js"),
            json.dumps(SAMPLES),
        ],
        text=True,
    )
    return json.loads(raw)


def test_mail_purple_tokens_match_the_wake_word():
    painted = _mail_tint()
    assert painted["purple"] == "#7C3AED"
    assert bubble.PURPLE == "#7C3AED"
    expected = [list(map(list, bubble.bubble_purple_ranges(sample))) for sample in SAMPLES]
    assert painted["ranges"] == expected
    for sample, tokens, ranges in zip(SAMPLES, painted["tokens"], expected):
        assert tokens == [sample[start:end].replace("\u200b", "").replace("\ufffc", "") for start, end in ranges]
        for token in tokens:
            assert token.casefold() == "@bubble"
    assert bubble.bubble_question(SAMPLES[0]) == "email the sales team this quarter's profit margin"
    assert bubble.bubble_question(SAMPLES[1]) == "our margin this quarter is"


def test_mail_stylesheet_uses_weave_purple():
    css = (GMAIL / "mail.css").read_text(encoding="utf-8")
    manifest = json.loads((GMAIL / "manifest.json").read_text(encoding="utf-8"))
    script = manifest["content_scripts"][0]
    assert "#7C3AED" in css
    assert "weave-mail-bubble" in css
    assert script["js"][:2] == ["mail_tint.js", "content.js"]
    assert "mail.css" in script["css"]
    content = (GMAIL / "content.js").read_text(encoding="utf-8")
    assert "paintMailBubble" in content
