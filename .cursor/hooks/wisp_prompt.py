"""Cursor hook for Weave pet suggestions.

``prompt`` asks the local agent for a cue and, on a found hit, shows it on the pet.
``stop`` tells the pet the turn ended and submits one accepted note as a follow-up.

The hook always exits 0. A missing token, a down agent, or a missing pet never blocks the prompt.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PORT_FILE = REPO / "cursor-wisp" / "desktop" / "wisp.port"
ACCEPT_FILE = REPO / "cursor-wisp" / "desktop" / "wisp-accept.json"
DEFAULT_AGENT_URL = "http://127.0.0.1:8765"
CUE_TIMEOUT = 18
PET_TIMEOUT = 2
MAX_PROMPT = 1500
LOG = REPO / "cursor-wisp" / "desktop" / "pet.log"


def load_env_file(path: Path) -> None:
    """Load one env file. Existing process values win, matching ``shared.env.load_env``."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_repo_env() -> None:
    """``.env.local`` then ``.env``, then ``.env.tokens`` the way ``scripts.tokens`` does."""
    for name in (".env.local", ".env", ".env.tokens"):
        load_env_file(REPO / name)


def viewer_token() -> str:
    """Bearer for ``POST /answer``. ``WEAVE_VIEWER_TOKEN`` wins; otherwise the ask.py default."""
    load_repo_env()
    for key in ("WEAVE_VIEWER_TOKEN", "WEAVE_TOKEN_SAM"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def agent_url() -> str:
    configured = os.environ.get("WEAVE_AGENT_URL", "").strip()
    return (configured or DEFAULT_AGENT_URL).rstrip("/")


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


def suggest(prompt: str) -> None:
    token = viewer_token()
    if not token:
        note("cue no-token")
        return
    try:
        status, body = post_json(
            f"{agent_url()}/wisp/cue",
            {"prompt": prompt[:MAX_PROMPT]},
            CUE_TIMEOUT,
            token,
        )
    except Exception:
        note("cue down")
        return
    if status != 200:
        note(f"cue http {status}")
        return
    try:
        cue = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        note("cue bad-json")
        return
    if not isinstance(cue, dict) or cue.get("state") != "found":
        note("cue unsure")
        return
    port = pet_port()
    if port is None:
        note("cue no-pet")
        return
    try:
        post_json(f"http://127.0.0.1:{port}/suggestion", cue, PET_TIMEOUT)
    except Exception:
        note("cue pet-down")
        return
    note("cue shown")


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
    note(f"cue stdin {len(raw)}")
    text = _decode_stdin(raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        note("cue bad-stdin")
        return {"continue": True}
    prompt = data.get("prompt") if isinstance(data, dict) else None
    if isinstance(prompt, str) and prompt.strip():
        suggest(prompt)
    else:
        note("cue no-prompt")
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
