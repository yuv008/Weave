"""Check that the running events app accepts a signed Slack challenge."""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.ingest.worker import slack_client_from_env


def load_env() -> None:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def main() -> None:
    load_env()
    secret = os.environ["SLACK_SIGNING_SECRET"]
    body = json.dumps({"type": "url_verification", "challenge": "weave-challenge"}).encode()
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        secret.encode(),
        f"v0:{timestamp}:".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        "http://127.0.0.1:8000/slack/events",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode())
            print(f"signed_status={response.status}")
            print(f"challenge_ok={payload.get('challenge') == 'weave-challenge'}")
    except urllib.error.HTTPError as exc:
        print(f"signed_status={exc.code}")
    channels = slack_client_from_env().list_public_channels().get("channels") or []
    names = [f"{channel.get('name')}:{channel.get('id')}" for channel in channels]
    print("channels=" + ",".join(names))


if __name__ == "__main__":
    main()
