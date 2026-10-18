"""Cursor hook for Weave pet suggestions.

``prompt`` returns immediately. A normal prompt is left alone. An ``@bubble`` ask is
forwarded to the pet, which answers on its own card. ``stop`` tells the pet the turn
ended and submits one accepted note as a follow-up.

The hook always exits 0. A missing pet never blocks the prompt.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DESKTOP = REPO / "cursor-wisp" / "desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))
from bubble import bubble_question

PORT_FILE = DESKTOP / "wisp.port"
ACCEPT_FILE = DESKTOP / "wisp-accept.json"
PET_TIMEOUT = 2
MAX_PROMPT = 1500
LOG = DESKTOP / "pet.log"


def post_json(url: str, payload: dict, timeout: float, token: str = "") -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def pet_port() -> int | None:
    try:
        port = int(PORT_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def note(message: str) -> None:
    """One status line in the pet log. Never the prompt or the token."""
    try:
        from datetime import datetime
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(datetime.now().strftime("%H:%M:%S") + " " + message + "\n")
    except OSError:
        pass


def forward_ask(question: str) -> None:
    """Hand the question to the pet and return. The model call stays on the pet."""
    port = pet_port()
    if port is None:
        note("ask no-pet")
        return
    try:
        post_json(f"http://127.0.0.1:{port}/ask", {"question": question}, PET_TIMEOUT)
    except Exception:
        note("ask pet-down")
        return
    note("ask forwarded")


def turn_ended() -> None:
    port = pet_port()
    if port is None:
        return
    try:
        post_json(f"http://127.0.0.1:{port}/turn-ended", {}, PET_TIMEOUT)
    except Exception:
        return


def read_accept_text() -> str:
    """Read an accepted note. The file stays until the follow-up is printed."""
    if not ACCEPT_FILE.is_file():
        return ""
    try:
        parsed = json.loads(ACCEPT_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        clear_accept_file()
        return ""
    text = parsed.get("text") if isinstance(parsed, dict) else ""
    if not isinstance(text, str):
        clear_accept_file()
        return ""
    cleaned = text.replace("\ufeff", "").strip()
    if not cleaned:
        clear_accept_file()
    return cleaned


def clear_accept_file() -> None:
    try:
        ACCEPT_FILE.unlink()
    except OSError:
        pass


def _decode_stdin(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if b"\x00" in raw[:8]:
        try:
            return raw.decode("utf-16-le")
        except UnicodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def run_prompt() -> dict:
    raw = sys.stdin.buffer.read()
    text = _decode_stdin(raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {"continue": True}
    prompt = data.get("prompt") if isinstance(data, dict) else None
    if isinstance(prompt, str):
        question = bubble_question(prompt)
        if question and len(question) <= MAX_PROMPT:
            forward_ask(question)
    return {"continue": True}


def run_stop() -> dict:
    turn_ended()
    text = read_accept_text()
    if text:
        return {"followup_message": text}
    return {}


def emit(payload: dict) -> None:
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"
    try:
        payload = run_stop() if mode == "stop" else run_prompt()
    except BaseException:
        payload = {} if mode == "stop" else {"continue": True}
    emit(payload)
    if mode == "stop" and payload.get("followup_message"):
        clear_accept_file()
    raise SystemExit(0)


if __name__ == "__main__":
    main()
