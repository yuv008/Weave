"use strict";

// Paints the @bubble token Weave purple inside the Cursor composer.
// Painting leaves the draft alone. replaceWeaveBubbleSentence swaps the
// @bubble sentence for an accepted answer.

const WEAVE_PURPLE = "#7C3AED";
const HIGHLIGHT_NAME = "weave-bubble";
const WORD = "bubble";

function isFormat(char) {
  return char === "\ufffc" || /\p{Cf}/u.test(char);
}

function isSpace(char) {
  const code = char.charCodeAt(0);
  if (code === 0x20 || code === 0x09 || code === 0x0a || code === 0x0b || code === 0x0c || code === 0x0d) {
    return true;
  }
  if (code >= 0x1c && code <= 0x1f) return true;
  if (code === 0x85 || code === 0xa0 || code === 0x1680) return true;
  if (code >= 0x2000 && code <= 0x200a) return true;
  return code === 0x2028 || code === 0x2029 || code === 0x202f || code === 0x205f || code === 0x3000;
}

function bubblePurpleRanges(text) {
  if (typeof text !== "string" || !text) return [];
  const ranges = [];
  const length = text.length;
  const word = WORD.length;
  let index = 0;
  while (index < length) {
    if (text[index] !== "@" || (index > 0 && !isSpace(text[index - 1]))) {
      index += 1;
      continue;
    }
    let cursor = index + 1;
    while (cursor < length && isFormat(text[cursor])) cursor += 1;
    const end = cursor + word;
    if (end <= length && text.slice(cursor, end).toLowerCase() === WORD) {
      let after = end;
      while (after < length && isFormat(text[after])) after += 1;
      if (after === length || isSpace(text[after])) {
        ranges.push([index, end]);
        index = after;
        continue;
      }
    }
    index += 1;
  }
  return ranges;
}

function replaceSpanSentence(text, tokenStart, tokenEnd, answer) {
  const replacement = String(answer || "").replace(/\s+/g, " ").trim();
  if (!replacement || tokenStart < 0 || tokenEnd <= tokenStart || tokenEnd > text.length) return null;
  let start = tokenStart;
  while (start > 0) {
    const prev = text[start - 1];
    if (prev === "\n" || prev === "\r" || prev === "." || prev === "!" || prev === "?") break;
    start -= 1;
  }
  while (start < tokenStart && (text[start] === " " || text[start] === "\t")) start += 1;
  let end = tokenEnd;
  while (end < text.length) {
    const ch = text[end];
    if (ch === "\n" || ch === "\r") break;
    end += 1;
    if (ch === "." || ch === "!" || ch === "?") {
      while (end < text.length && (text[end] === "." || text[end] === "!" || text[end] === "?")) end += 1;
      break;
    }
  }
  return text.slice(0, start) + replacement + text.slice(end);
}

function replaceBubbleSentenceText(text, answer) {
  const replacement = String(answer || "").replace(/\s+/g, " ").trim();
  if (!replacement || typeof text !== "string" || !text) return null;
  const ranges = bubblePurpleRanges(text);
  if (!ranges.length) return null;
  let end = ranges[0][1];
  while (end < text.length && isFormat(text[end])) end += 1;
  return text.slice(0, ranges[0][0]) + replacement + text.slice(end);
}

function visibleToken(text) {
  let out = "";
  for (const char of text) {
    if (isFormat(char)) continue;
    out += char;
  }
  return out;
}

function composerRoots() {
  return Array.from(document.querySelectorAll(".aislash-editor-input")).filter((el) => {
    if (el.classList.contains("aislash-editor-input-readonly")) return false;
    return el.closest(".aislash-editor-input-readonly") === null;
  });
}

function textParts(root) {
  const parts = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (parent && parent.closest("[data-ghost='true'], .composer-ghost-text")) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  let offset = 0;
  let node = walker.nextNode();
  while (node) {
    const text = node.nodeValue || "";
    parts.push({ node, text, start: offset });
    offset += text.length;
    node = walker.nextNode();
  }
  return parts;
}

function rangesFor(parts, start, end) {
  const painted = [];
  for (const part of parts) {
    const partEnd = part.start + part.text.length;
    if (partEnd <= start || part.start >= end) continue;
    const from = Math.max(start, part.start) - part.start;
    const to = Math.min(end, partEnd) - part.start;
    if (from >= to) continue;
    const range = new Range();
    range.setStart(part.node, from);
    range.setEnd(part.node, to);
    painted.push(range);
  }
  return painted;
}

function ensureStyle() {
  let style = document.getElementById("weave-bubble-style");
  if (!style) {
    style = document.createElement("style");
    style.id = "weave-bubble-style";
    style.textContent = "::highlight(" + HIGHLIGHT_NAME + ") { color: " + WEAVE_PURPLE + "; }";
    (document.head || document.documentElement).appendChild(style);
  }
  let rule = "";
  try {
    rule = style.sheet && style.sheet.cssRules[0] ? style.sheet.cssRules[0].style.color : "";
  } catch (error) {
    rule = "";
  }
  return rule || WEAVE_PURPLE;
}

const COLOR_STYLE = "color: #7C3AED";

function styleIsOurs(style) {
  const value = String(style || "");
  return /#7c3aed/i.test(value) || /rgb\(\s*124\s*,\s*58\s*,\s*237\s*\)/i.test(value);
}

function clearOurs(style) {
  return String(style || "")
    .replace(/color:\s*#7C3AED;?/ig, "")
    .replace(/color:\s*rgb\(\s*124\s*,\s*58\s*,\s*237\s*\);?/ig, "")
    .replace(/^\s*;\s*|\s*;\s*$/g, "")
    .trim();
}

function bubbleStylePieces(text) {
  const ranges = bubblePurpleRanges(text);
  if (!ranges.length) return [{ text: text, purple: false }];
  const pieces = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (cursor < start) pieces.push({ text: text.slice(cursor, start), purple: false });
    pieces.push({ text: text.slice(start, end), purple: true });
    cursor = end;
  }
  if (cursor < text.length) pieces.push({ text: text.slice(cursor), purple: false });
  return pieces;
}

function needsLexicalTint(root) {
  const editor = root.__lexicalEditor;
  if (!editor || typeof editor.getEditorState !== "function") return false;
  const map = editor.getEditorState()._nodeMap;
  if (!map) return false;
  for (const node of map.values()) {
    if (!node || typeof node.getType !== "function" || node.getType() !== "text") continue;
    const text = node.getTextContent();
    const ranges = bubblePurpleRanges(text);
    const ours = styleIsOurs(node.getStyle && node.getStyle());
    const whole = ranges.length === 1 && ranges[0][0] === 0 && ranges[0][1] === text.length;
    if (whole !== ours || (ranges.length && !whole)) return true;
  }
  return false;
}

function tintLexical(root) {
  const editor = root.__lexicalEditor;
  if (!editor || !needsLexicalTint(root)) return;
  editor.update(() => {
    const map = editor.getEditorState()._nodeMap;
    const nodes = [];
    for (const node of map.values()) {
      if (node && typeof node.getType === "function" && node.getType() === "text") nodes.push(node);
    }
    for (const node of nodes) {
      const text = node.getTextContent();
      const ranges = bubblePurpleRanges(text);
      const ours = styleIsOurs(node.getStyle());
      const whole = ranges.length === 1 && ranges[0][0] === 0 && ranges[0][1] === text.length;
      if (whole) {
        if (!ours) node.setStyle(COLOR_STYLE);
        continue;
      }
      if (!ranges.length) {
        if (ours) node.setStyle(clearOurs(node.getStyle()));
        continue;
      }
      const points = [];
      for (const [start, end] of ranges) {
        if (start > 0) points.push(start);
        if (end < text.length) points.push(end);
      }
      const offsets = [...new Set(points)].sort((a, b) => a - b);
      const pieces = offsets.length ? node.splitText(...offsets) : [node];
      let cursor = 0;
      for (const piece of pieces) {
        const pieceText = piece.getTextContent();
        const pieceEnd = cursor + pieceText.length;
        const covered = ranges.some(([start, end]) => start === cursor && end === pieceEnd);
        if (covered) {
          if (!styleIsOurs(piece.getStyle())) piece.setStyle(COLOR_STYLE);
        } else if (styleIsOurs(piece.getStyle())) {
          piece.setStyle(clearOurs(piece.getStyle()));
        }
        cursor = pieceEnd;
      }
    }
  }, { tag: "history-merge" });
}

function scheduleLexicalTint() {
  const state = globalThis.__weaveBubbleTint;
  if (!state || state.lexicalTimer) return;
  const attempt = (state.lexicalAttempts || 0) + 1;
  state.lexicalAttempts = attempt;
  state.lexicalTimer = setTimeout(() => {
    state.lexicalTimer = 0;
    let failed = false;
    for (const root of composerRoots()) {
      try { tintLexical(root); } catch (error) { failed = true; }
    }
    if (failed && attempt < 4) scheduleLexicalTint();
    else state.lexicalAttempts = 0;
  }, attempt === 1 ? 0 : 40);
}

function paintBubbleTint() {
  if (!window.CSS || !CSS.highlights || typeof Highlight !== "function") {
    return { ok: false, error: "no-highlight" };
  }
  const rule = ensureStyle();
  const highlight = new Highlight();
  if ("priority" in highlight) highlight.priority = 10;
  const tokens = [];
  let inComposer = true;
  for (const root of composerRoots()) {
    const parts = textParts(root);
    const full = parts.map((part) => part.text).join("");
    for (const [start, end] of bubblePurpleRanges(full)) {
      for (const range of rangesFor(parts, start, end)) {
        if (!root.contains(range.startContainer)) inComposer = false;
        highlight.add(range);
        const token = visibleToken(range.toString());
        if (/^@bubble$/i.test(token)) tokens.push(token);
      }
    }
  }
  CSS.highlights.set(HIGHLIGHT_NAME, highlight);
  scheduleLexicalTint();
  return {
    ok: true,
    color: WEAVE_PURPLE,
    rule: rule,
    count: highlight.size || tokens.length,
    tokens: tokens,
    inComposer: inComposer,
  };
}

function installWeaveBubbleTint() {
  const state = globalThis.__weaveBubbleTint || { watched: new WeakSet() };
  state.paint = paintBubbleTint;
  if (!globalThis.__weaveBubbleTint) {
    globalThis.__weaveBubbleTint = state;
    const tick = () => {
      const current = globalThis.__weaveBubbleTint;
      if (!current || !current.paint) return;
      try {
        for (const root of composerRoots()) {
          if (current.watched.has(root)) continue;
          current.watched.add(root);
          const observer = new MutationObserver(() => {
            try { current.paint(); } catch (error) { /* the next tick retries */ }
          });
          observer.observe(root, { subtree: true, childList: true, characterData: true });
        }
        current.paint();
      } catch (error) { /* composer not ready yet */ }
    };
    document.addEventListener("input", tick, true);
    state.timer = setInterval(tick, 200);
  }
  try {
    return state.paint();
  } catch (error) {
    return { ok: false, error: "paint" };
  }
}

function isGhost(node) {
  return !!(node && typeof node.getType === "function" && node.getType() === "composer-ghost-text");
}

function isBubbleMention(node) {
  if (!node || typeof node.getType !== "function" || typeof node.getTextContent !== "function") return false;
  const type = node.getType();
  if (type === "text" || type === "paragraph" || type === "root" || type === "linebreak" || type === "composer-ghost-text") {
    return false;
  }
  return /^@?bubble$/i.test(visibleToken(node.getTextContent()).trim());
}

function blockText(block) {
  const children = typeof block.getChildren === "function" ? block.getChildren() : [];
  let text = "";
  for (const child of children) {
    if (isGhost(child) || typeof child.getTextContent !== "function") continue;
    text += child.getTextContent();
  }
  return text;
}

function plannedText(block, answer) {
  const text = blockText(block);
  const viaAt = replaceBubbleSentenceText(text, answer);
  if (viaAt !== null) return viaAt === text ? null : viaAt;
  const children = typeof block.getChildren === "function" ? block.getChildren() : [];
  let offset = 0;
  for (const child of children) {
    if (isGhost(child) || typeof child.getTextContent !== "function") continue;
    const part = child.getTextContent();
    if (isBubbleMention(child)) {
      const replacement = String(answer || "").replace(/\s+/g, " ").trim();
      if (!replacement) return null;
      const next = text.slice(0, offset) + replacement + text.slice(offset + part.length);
      return next === text ? null : next;
    }
    offset += part.length;
  }
  return null;
}

function textKlass(editor, block) {
  const children = typeof block.getChildren === "function" ? block.getChildren() : [];
  for (const child of children) {
    if (child && typeof child.getType === "function" && child.getType() === "text") return child.constructor;
  }
  const registered = editor._nodes && typeof editor._nodes.get === "function" ? editor._nodes.get("text") : null;
  return registered && registered.klass ? registered.klass : null;
}

function rewriteBlock(editor, block, next) {
  const children = typeof block.getChildren === "function" ? block.getChildren().slice() : [];
  const real = children.filter((child) => !isGhost(child));
  const Ctor = textKlass(editor, block);
  if (!Ctor) return false;
  for (const child of real) {
    if (typeof child.remove === "function") child.remove();
  }
  block.append(new Ctor(next));
  return true;
}

function paragraphNodes(node, found) {
  if (!node || typeof node.getType !== "function") return;
  if (node.getType() === "paragraph") {
    found.push(node);
    return;
  }
  const children = typeof node.getChildren === "function" ? node.getChildren() : [];
  for (const child of children) paragraphNodes(child, found);
}

function pendingRoot(editor) {
  let root = null;
  editor.getEditorState()._nodeMap.forEach((node) => {
    if (node && typeof node.getType === "function" && node.getType() === "root") root = node;
  });
  return root;
}

function replaceWeaveBubbleSentence(answer) {
  const clean = String(answer || "").replace(/\s+/g, " ").trim();
  if (!clean) return { ok: false, error: "empty" };
  let replaced = 0;
  let error = "";
  for (const root of composerRoots()) {
    const editor = root.__lexicalEditor;
    if (!editor || typeof editor.update !== "function") continue;
    const before = root.innerText || "";
    const firstLine = before.split("\n")[0];
    const bubbleAt = firstLine.search(/@bubble/i);
    const prefix = (bubbleAt > 0 ? firstLine.slice(0, bubbleAt) : bubbleAt < 0 ? firstLine : "").trim();
    const previous = editor.getEditorState();
    try {
      let wrote = 0;
      editor.update(() => {
        const rootNode = pendingRoot(editor);
        const paragraphs = [];
        paragraphNodes(rootNode, paragraphs);
        for (const block of paragraphs) {
          const next = plannedText(block, clean);
          if (next === null) continue;
          if (rewriteBlock(editor, block, next)) wrote += 1;
        }
      }, { discrete: true });
      const after = root.innerText || "";
      const probe = clean.slice(0, Math.min(24, clean.length));
      const stillThere = !prefix || after.indexOf(prefix) !== -1;
      if (wrote > 0 && after.indexOf(probe) !== -1 && stillThere) replaced += wrote;
      else if (previous) editor.setEditorState(previous);
    } catch (failure) {
      error = String(failure && failure.message ? failure.message : failure).slice(0, 180);
    }
  }
  if (replaced > 0) return { ok: true, replaced: replaced };
  return { ok: false, error: error || "missing" };
}

try {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      bubblePurpleRanges,
      bubbleStylePieces,
      replaceBubbleSentenceText,
      replaceSpanSentence,
      WEAVE_PURPLE,
    };
  }
} catch (error) { /* the composer page has no module */ }
