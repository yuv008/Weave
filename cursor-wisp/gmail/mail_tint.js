"use strict";

// Paints the @bubble token Weave purple inside a Gmail compose body.
// The message text is not rewritten, so the mail ask still parses.

const WEAVE_PURPLE = "#7C3AED";
const HIGHLIGHT_NAME = "weave-mail-bubble";
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

function visibleToken(text) {
  let out = "";
  for (const char of text) {
    if (isFormat(char)) continue;
    out += char;
  }
  return out;
}

function mailPurpleTokens(text) {
  if (typeof text !== "string" || !text) return [];
  return bubblePurpleRanges(text).map(([start, end]) => visibleToken(text.slice(start, end)));
}

function textParts(root) {
  const parts = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
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
    try {
      const range = new Range();
      range.setStart(part.node, from);
      range.setEnd(part.node, to);
      painted.push(range);
    } catch (error) {
      /* Gmail can drop a text node while the walk is in progress. */
    }
  }
  return painted;
}

function ensureStyle() {
  let style = document.getElementById("weave-mail-bubble-style");
  if (!style) {
    style = document.createElement("style");
    style.id = "weave-mail-bubble-style";
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

function paintMailBubble(roots) {
  if (!window.CSS || !CSS.highlights || typeof Highlight !== "function") {
    return { ok: false, error: "no-highlight" };
  }
  const rule = ensureStyle();
  const highlight = new Highlight();
  if ("priority" in highlight) highlight.priority = 10;
  const tokens = [];
  const list = roots && typeof roots.length === "number" ? roots : [];
  for (let i = 0; i < list.length; i += 1) {
    const root = list[i];
    if (!root || !root.isConnected) continue;
    const parts = textParts(root);
    const full = parts.map((part) => part.text).join("");
    const ranges = bubblePurpleRanges(full);
    for (let r = 0; r < ranges.length; r += 1) {
      const start = ranges[r][0];
      const end = ranges[r][1];
      const pieces = rangesFor(parts, start, end);
      for (let p = 0; p < pieces.length; p += 1) {
        const range = pieces[p];
        highlight.add(range);
        const token = visibleToken(range.toString());
        if (/^@bubble$/i.test(token)) tokens.push(token);
      }
    }
  }
  CSS.highlights.set(HIGHLIGHT_NAME, highlight);
  return {
    ok: true,
    color: WEAVE_PURPLE,
    rule: rule,
    count: highlight.size || tokens.length,
    tokens: tokens,
  };
}

globalThis.paintMailBubble = paintMailBubble;

try {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { bubblePurpleRanges, mailPurpleTokens, paintMailBubble, WEAVE_PURPLE };
  }
} catch (error) { /* the Gmail page has no module */ }
