import base64
import hashlib
import zipfile
from pathlib import Path

root = Path(r"c:\Users\yuvra\OneDrive\Desktop\Weave")
overlay = root / "cursor-wisp" / "overlay"
cast = [
    ("wisp", root / "frontend" / "wisp.svg"),
    ("knot", root / "frontend" / "wisp" / "knot.svg"),
    ("loaf", root / "frontend" / "wisp" / "loaf.svg"),
    ("pebble", root / "frontend" / "wisp" / "pebble.svg"),
    ("spark", root / "frontend" / "wisp" / "spark.svg"),
    ("spindle", root / "frontend" / "wisp" / "spindle.svg"),
]

pets = []
for pet_id, path in cast:
    svg = path.read_text(encoding="utf-8").strip()
    if "</script" in svg.lower() or "</div" in svg.lower():
        raise SystemExit(f"unsafe markup in {path}")
    hidden = "" if pet_id == "wisp" else " hidden"
    if 'preserveAspectRatio' not in svg:
        svg = svg.replace("<svg ", '<svg preserveAspectRatio="xMidYMid meet" ', 1)
    if ' width=' not in svg.split(">", 1)[0]:
        svg = svg.replace('viewBox="0 0 ', 'width="{w}" height="{h}" viewBox="0 0 ', 1)
        width, height = svg.split('viewBox="0 0 ', 1)[1].split('"', 1)[0].split()
        svg = svg.replace('width="{w}" height="{h}"', f'width="{width}" height="{height}"', 1)
    pets.append(f'<div class="pet" data-id="{pet_id}"{hidden}>{svg}</div>')

frame = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';">
<style>
  html, body { margin: 0; width: 100%; height: 100%; background: transparent; overflow: hidden; }
  body { height: 100%; }
  .pet {
    box-sizing: border-box;
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }
  .pet[hidden] { display: none !important; }
  svg {
    display: block;
    width: auto;
    height: auto;
    max-width: 92%;
    max-height: 92%;
  }
  .gaze {
    transform: translate(calc(var(--gx, 0) * 22px), calc(var(--gy, 0) * 14px));
    transform-box: fill-box;
    transform-origin: center;
  }
  .pet .wisp-root {
    transform-origin: 50% 72%;
    transform: rotate(calc(var(--gx, 0) * -20deg));
    transition: transform 0.16s ease-out;
  }
  .pet.reading .wisp-root {
    transform: rotate(calc(var(--gx, 0) * -24deg)) translateY(calc(var(--gy, 0) * -2px));
  }
  .pet.mood-sad .gaze {
    transform: translate(calc(var(--gx, 0) * 20px), calc(8px + var(--gy, 0) * 6px)) scaleY(0.72);
  }
  .pet.mood-sad .wisp-root {
    transform: translateY(3px) rotate(calc(var(--gx, 0) * -18deg));
  }
  .pet.mood-peek .wisp-root {
    transform: rotate(calc(var(--gx, 0) * -32deg)) translateY(-2px);
  }
  .pet.mood-peek .gaze {
    transform: translate(calc(var(--gx, 0) * 26px), calc(var(--gy, 0) * 12px));
  }
  .pet.poking .wisp-root {
    animation: poke-body 0.7s ease-out;
  }
  .pet.poking .eye {
    animation: poke-squint 0.7s ease-out !important;
  }
  @keyframes poke-body {
    0% { transform: rotate(0deg) scale(1, 1); }
    18% { transform: rotate(-6deg) scale(1.04, 0.92); }
    42% { transform: rotate(4deg) scale(0.98, 1.02); }
    100% { transform: rotate(0deg) scale(1, 1); }
  }
  @keyframes poke-squint {
    0%, 100% { transform: scaleY(1); }
    12%, 55% { transform: scaleY(0.08); }
  }
  @media (prefers-reduced-motion: reduce) {
    .pet .wisp-root,
    .pet.reading .wisp-root,
    .pet.mood-peek .wisp-root,
    .pet.mood-sad .wisp-root { transform: none; }
    .pet.mood-sad .gaze { transform: translateY(5px) scaleY(0.75); }
    .pet.poking .wisp-root { animation: none; transform: scale(0.97, 0.97); }
    .pet.poking .eye { animation: poke-squint-reduced 0.5s ease-out !important; }
    @keyframes poke-squint-reduced {
      0%, 100% { transform: scaleY(1); }
      30%, 70% { transform: scaleY(0.08); }
    }
  }
</style>
</head>
<body title="Drag with left click. Right-click nudge. Double right-click next companion">
""" + "\n".join(pets) + """
<script>
  const order = ["wisp", "knot", "loaf", "pebble", "spark", "spindle"];
  let current = "wisp";
  let pokeTimer = 0;
  function visiblePet() {
    return document.querySelector(".pet:not([hidden])");
  }
  function wrapEyes() {
    document.querySelectorAll(".eye").forEach((eye) => {
      if (eye.parentElement && eye.parentElement.classList.contains("gaze")) return;
      const wrap = document.createElementNS("http://www.w3.org/2000/svg", "g");
      wrap.classList.add("gaze");
      eye.parentNode.insertBefore(wrap, eye);
      wrap.appendChild(eye);
    });
    document.querySelectorAll("svg > g.wisp").forEach((node) => {
      node.classList.add("wisp-root");
    });
  }
  function show(id) {
    current = id;
    try { localStorage.setItem("weave-wisp", id); } catch (e) {}
    document.querySelectorAll(".pet").forEach((pet) => { pet.hidden = pet.dataset.id !== id; });
    reportInk();
  }
  function weaveNext() {
    show(order[(order.indexOf(current) + 1) % order.length]);
  }
  function weaveLook(gx, gy) {
    const pet = visiblePet();
    if (!pet) return;
    pet.style.setProperty("--gx", String(gx));
    pet.style.setProperty("--gy", String(gy));
  }
  function weaveReading(on) {
    const pet = visiblePet();
    if (!pet) return;
    pet.classList.toggle("reading", !!on);
  }
  function weaveMood(mood, flip) {
    const pet = visiblePet();
    if (!pet) return;
    pet.classList.remove("mood-sad", "mood-peek");
    if (mood === "sad") pet.classList.add("mood-sad");
    if (mood === "peek") pet.classList.add("mood-peek");
    pet.style.setProperty("--peek-flip", flip < 0 ? "-1" : "1");
  }
  function weavePoke() {
    const pet = visiblePet();
    if (!pet) return;
    pet.classList.remove("poking");
    void pet.offsetWidth;
    pet.classList.add("poking");
    clearTimeout(pokeTimer);
    pokeTimer = setTimeout(() => pet.classList.remove("poking"), 720);
  }
  function reportInk() {
    const pet = visiblePet();
    const svg = pet ? pet.querySelector("svg") : null;
    if (!svg || !window.chrome || !chrome.webview) return;
    const r = svg.getBoundingClientRect();
    chrome.webview.postMessage({
      type: "ink",
      left: r.left,
      top: r.top,
      right: r.right,
      bottom: r.bottom,
    });
  }
  window.weaveNext = weaveNext;
  window.weaveLook = weaveLook;
  window.weaveReading = weaveReading;
  window.weaveMood = weaveMood;
  window.weavePoke = weavePoke;
  window.reportInk = reportInk;
  document.querySelectorAll("svg").forEach((svg) => {
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  });
  wrapEyes();
  show(current);
  setInterval(reportInk, 120);
  window.addEventListener("resize", reportInk);
</script>
</body>
</html>
"""
frame_path = overlay / "wisp-frame.html"
frame_path.write_text(frame, encoding="utf-8", newline="\n")
print("frame", frame_path.stat().st_size)

workbench = Path(r"C:\Program Files\cursor\resources\app\out\vs\code\electron-sandbox\workbench")
product = Path(r"C:\Program Files\cursor\resources\app\product.json")
html_path = workbench / "workbench.html"


def checksum(path):
    digest = hashlib.sha256(path.read_bytes()).digest()
    return base64.b64encode(digest).decode("ascii").rstrip("=")


product_text = product.read_text(encoding="utf-8")
marker = '"vs/code/electron-sandbox/workbench/workbench.html": "'
start = product_text.find(marker)
if start < 0:
    raise SystemExit("checksum entry missing")
start += len(marker)
end = product_text.find('"', start)
recorded = product_text[start:end]
actual = checksum(html_path)
print("recorded", recorded)
print("actual  ", actual)
if recorded != actual and "./wisp-companion.js" not in html_path.read_text(encoding="utf-8"):
    raise SystemExit("checksum algorithm does not match; not patching")

(workbench / "wisp-companion.js").write_bytes((overlay / "wisp-companion.js").read_bytes())
(workbench / "wisp-frame.html").write_bytes(frame_path.read_bytes())

html_bytes = html_path.read_bytes()
anchor = b'<script src="./workbench.js" type="module"></script>'
if b"./wisp-companion.js" not in html_bytes:
    if anchor not in html_bytes:
        raise SystemExit("workbench script tag missing")
    html_path.write_bytes(html_bytes.replace(anchor, anchor + b'\n\t<script src="./wisp-companion.js"></script>', 1))

updated = checksum(html_path)
raw = product.read_bytes()
old = (marker + recorded).encode("ascii")
new = (marker + updated).encode("ascii")
if old not in raw:
    raise SystemExit("could not update checksum")
product.write_bytes(raw.replace(old, new, 1))
print("patched", updated)
(overlay / "install.log").write_text("patched " + updated + "\n", encoding="utf-8")

ext = root / "cursor-wisp"
vsix = Path(r"c:\Users\yuvra\AppData\Local\Temp\wisp-0.3.0.vsix")
manifest = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="wisp" Version="0.3.0" Publisher="weave" />
    <DisplayName>Wisp</DisplayName>
    <Description xml:space="preserve">Closes the old editor companion.</Description>
    <Tags>wisp</Tags>
    <Categories>Other</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.80.0" />
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true" />
  </Assets>
</PackageManifest>
"""
types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""
with zipfile.ZipFile(vsix, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
    zipped.writestr("[Content_Types].xml", types)
    zipped.writestr("extension.vsixmanifest", manifest)
    zipped.write(ext / "package.json", "extension/package.json")
    zipped.write(ext / "extension.js", "extension/extension.js")
print(vsix)
