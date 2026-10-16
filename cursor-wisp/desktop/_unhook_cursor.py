"""Remove the pet that was injected into Cursor's own window."""

import base64
import hashlib
from pathlib import Path

html_path = Path(r"C:\Program Files\cursor\resources\app\out\vs\code\electron-sandbox\workbench\workbench.html")
product = Path(r"C:\Program Files\cursor\resources\app\product.json")
raw = html_path.read_bytes()
snippet = b'\n\t<script src="./wisp-companion.js"></script>'
if snippet in raw:
    html_path.write_bytes(raw.replace(snippet, b"", 1))

digest = hashlib.sha256(html_path.read_bytes()).digest()
checksum = base64.b64encode(digest).decode("ascii").rstrip("=")
product_bytes = product.read_bytes()
marker = b'"vs/code/electron-sandbox/workbench/workbench.html": "'
start = product_bytes.find(marker)
if start < 0:
    raise SystemExit("checksum entry missing")
start += len(marker)
end = product_bytes.find(b'"', start)
product.write_bytes(product_bytes[:start] + checksum.encode("ascii") + product_bytes[end:])
print("cursor pet removed", checksum)
