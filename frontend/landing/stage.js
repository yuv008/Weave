const canvas = document.getElementById("screen");
const slot = document.getElementById("wisp");
const live = document.getElementById("live");
const ctx = canvas.getContext("2d");
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const QUESTION = "which provider did we choose for sso?";
const QUOTE = "We are moving SSO to Provider B.";
const DURATION = 4600;

let start = performance.now();
let pupils = [];

function fit() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = Math.max(1, Math.round(w * dpr));
  canvas.height = Math.max(1, Math.round(h * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function clamp(n) {
  return Math.max(0, Math.min(1, n));
}

function span(t, a, b) {
  return clamp((t - a) / (b - a));
}

function typed(text, p) {
  return text.slice(0, Math.round(text.length * p));
}

function roundBox(x, y, w, h, r) {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
}

function node(x, y, label, status, alpha) {
  ctx.save();
  ctx.globalAlpha = alpha;
  roundBox(x, y, 196, 52, 8);
  ctx.strokeStyle = status === "active" ? "#f5f5f5" : "#3f3f46";
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.fillStyle = "#f5f5f5";
  ctx.font = "600 15px Segoe UI, Helvetica Neue, sans-serif";
  ctx.fillText(label, x + 16, y + 32);
  ctx.fillStyle = status === "active" ? "#f5f5f5" : "#71717a";
  ctx.font = "12px Cascadia Mono, Menlo, Consolas, monospace";
  ctx.fillText(status, x + 214, y + 31);
  ctx.restore();
}

function draw(p, now) {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0a0a0a";
  ctx.fillRect(0, 0, w, h);

  const q = typed(QUESTION, span(p, 0.04, 0.3));
  let promptSize = w < 760 ? 13 : 15;
  ctx.font = promptSize + "px Cascadia Mono, Menlo, Consolas, monospace";
  while (promptSize > 11 && ctx.measureText("> " + QUESTION).width > w - 48) {
    promptSize -= 1;
    ctx.font = promptSize + "px Cascadia Mono, Menlo, Consolas, monospace";
  }
  ctx.fillStyle = "#f5f5f5";
  ctx.fillText("> " + q, 28, 48);
  const caretOn = reduced || Math.floor(now / 530) % 2 === 0;
  if (caretOn && p < 0.34) {
    const width = ctx.measureText("> " + q).width;
    ctx.fillStyle = "#7c3aed";
    ctx.fillRect(28 + width + 2, 34, 8, 18);
  }

  const narrow = w < 760;
  const ax = 28;
  const ay = narrow ? 84 : 96;
  const bx = 28;
  const by = ay + 132;
  node(ax, ay, "Provider A", "superseded", span(p, 0.32, 0.44));

  const edge = span(p, 0.44, 0.6);
  if (edge > 0) {
    const x = ax + 98;
    const y1 = ay + 52;
    const y2 = y1 + (by - y1) * edge;
    ctx.strokeStyle = "#7c3aed";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x, y1);
    ctx.lineTo(x, y2);
    ctx.stroke();
    if (edge > 0.92) {
      ctx.beginPath();
      ctx.moveTo(x, by);
      ctx.lineTo(x - 5, by - 8);
      ctx.lineTo(x + 5, by - 8);
      ctx.closePath();
      ctx.fillStyle = "#7c3aed";
      ctx.fill();
    }
    ctx.globalAlpha = edge;
    ctx.fillStyle = "#a1a1aa";
    ctx.font = "11px Cascadia Mono, Menlo, Consolas, monospace";
    ctx.fillText("SUPERSEDES", x + 16, (y1 + by) / 2);
    ctx.globalAlpha = 1;
  }

  node(bx, by, "Provider B", "active", span(p, 0.6, 0.72));

  const quote = typed(QUOTE, span(p, 0.72, 0.9));
  if (quote) {
    const qx = narrow ? 28 : 430;
    const qy = narrow ? by + 96 : ay + 78;
    ctx.fillStyle = "#f5f5f5";
    ctx.font = "italic " + (narrow ? 16 : 20) + "px Palatino Linotype, Iowan Old Style, Palatino, serif";
    const shown = "\u201c" + quote + (p >= 0.9 ? "\u201d" : "");
    ctx.fillText(shown, qx, qy);
    if (p >= 0.9 && caretOn) {
      const width = ctx.measureText(shown).width;
      ctx.fillStyle = "#7c3aed";
      ctx.fillRect(qx + width + 3, qy - 16, 8, 18);
    }
    if (p >= 0.9) {
      ctx.fillStyle = "#71717a";
      ctx.font = "12px Cascadia Mono, Menlo, Consolas, monospace";
      ctx.fillText("Slack", qx, qy + 28);
    }
  }
}

function frame(now) {
  const p = reduced ? 1 : Math.min(1, (now - start) / DURATION);
  if (live) live.textContent = p >= 0.9 || reduced ? "cited" : "live";
  const root = slot.querySelector(".wisp");
  if (root) root.dataset.state = p > 0.72 || reduced ? "found" : "reading";
  draw(p, now);
  if (!reduced) requestAnimationFrame(frame);
}

async function mountPet() {
  try {
    const text = await fetch("/brand/wisp.svg").then((res) => res.text());
    slot.innerHTML = text;
    pupils = [...slot.querySelectorAll(".eye")].map((eye) => eye.children[1]).filter(Boolean);
  } catch (err) {
    slot.textContent = "";
  }
}

document.querySelector(".screen").addEventListener("pointermove", (event) => {
  const box = slot.getBoundingClientRect();
  const cx = box.left + box.width / 2;
  const cy = box.top + box.height / 2;
  const dx = Math.max(-2.2, Math.min(2.2, (event.clientX - cx) / 80));
  const dy = Math.max(-1.6, Math.min(1.6, (event.clientY - cy) / 90));
  pupils.forEach((pupil) => {
    pupil.style.transform = `translate(${dx}px, ${dy}px)`;
  });
});

fit();
window.addEventListener("resize", () => {
  fit();
  if (reduced) draw(1, 0);
});
mountPet();
requestAnimationFrame(frame);
