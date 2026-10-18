/* Reads the open Gmail compose box. It does not call Groq.
   The message body is a contenteditable. To and Subject are the fields beside it, when Gmail is showing them. */

const STABLE_MS = 400;
const POLL_MS = 400;
const BUBBLE = /@bubble/i;
const states = new WeakMap();

function readBody(el) {
  let text = "";
  try {
    text = el.innerText || "";
  } catch (error) {
    text = el.textContent || "";
  }
  return text.replace(/\u00a0/g, " ").replace(/\r\n/g, "\n");
}

function isComposeBody(el) {
  if (!el || el.nodeType !== 1 || el.getAttribute("contenteditable") !== "true") return false;
  if (el.getAttribute("role") === "combobox") return false;
  const label = el.getAttribute("aria-label") || "";
  if (/chat/i.test(label)) return false;
  if (el.closest && el.closest('[role="log"]')) return false;
  const message = /message body/i.test(label);
  const classic = el.classList.contains("Am") && (el.getAttribute("g_editable") === "true" || el.classList.contains("editable"));
  const textbox = el.getAttribute("role") === "textbox" && el.getAttribute("g_editable") === "true";
  const nearFields = textbox && !!ancestorWith(el, 'input[name="subjectbox"], textarea[name="to"], input[name="to"]');
  if (!message && !classic && !nearFields) return false;
  return el.getClientRects().length > 0;
}

function findComposeBodies() {
  const found = [];
  document.querySelectorAll('div[contenteditable="true"]').forEach((el) => {
    if (isComposeBody(el)) found.push(el);
  });
  return found;
}

function ancestorWith(body, selector) {
  let scope = body.parentElement;
  for (let depth = 0; scope && depth < 12; depth += 1, scope = scope.parentElement) {
    const found = scope.querySelector(selector);
    if (found && !body.contains(found)) return scope;
  }
  return null;
}

function readSubject(body) {
  const scope = ancestorWith(body, 'input[name="subjectbox"]');
  if (!scope) return "";
  const input = scope.querySelector('input[name="subjectbox"]');
  return input && !body.contains(input) ? input.value || "" : "";
}

function readTo(body) {
  const scope = ancestorWith(body, '[email], textarea[name="to"], input[name="to"]');
  if (!scope) return "";
  const seen = [];
  const push = (value) => {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (!text || seen.indexOf(text) !== -1) return;
    seen.push(text);
  };
  scope.querySelectorAll("[email]").forEach((chip) => {
    if (body.contains(chip)) return;
    const email = chip.getAttribute("email") || "";
    if (email.indexOf("@") !== -1) push(email);
  });
  const box = scope.querySelector('textarea[name="to"], input[name="to"]');
  if (box && !body.contains(box) && box.value) push(box.value);
  return seen.join(", ");
}

function fieldsOf(body, text) {
  return {
    to: readTo(body),
    subject: readSubject(body),
    body: text,
  };
}

function ask(message) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(message, (response) => {
        if (chrome.runtime.lastError) resolve(null);
        else resolve(response || null);
      });
    } catch (error) {
      resolve(null);
    }
  });
}

function noteChange(body) {
  const text = readBody(body);
  let state = states.get(body);
  if (!state) {
    state = { text: null, timer: 0, retry: 0, lastPosted: "", posting: false };
    states.set(body, state);
  }
  if (state.text === text) return;
  state.text = text;
  clearTimeout(state.timer);
  clearTimeout(state.retry);
  state.timer = setTimeout(() => onStable(body), STABLE_MS);
}

async function onStable(body) {
  const state = states.get(body);
  if (!state || state.posting) return;
  const text = readBody(body);
  state.text = text;
  if (!BUBBLE.test(text)) return;
  const payload = fieldsOf(body, text);
  const key = JSON.stringify(payload);
  if (key === state.lastPosted) return;
  state.posting = true;
  const response = await ask({ type: "mail", payload: payload });
  state.posting = false;
  if (readBody(body) !== text) return;
  if (response && response.ok) {
    state.lastPosted = key;
    return;
  }
  state.retry = setTimeout(() => onStable(body), 4000);
}

function findBubbleText(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    if (BUBBLE.test(node.nodeValue || "")) return node;
    node = walker.nextNode();
  }
  return null;
}

function lineElement(node, root) {
  let el = node.parentElement;
  while (el && el !== root) {
    if ((el.tagName === "DIV" || el.tagName === "P") && !el.querySelector("div, p")) return el;
    el = el.parentElement;
  }
  return null;
}

function isSingleLine(el) {
  return readBody(el).indexOf("\n") === -1;
}

function replaceToken(node, insert) {
  const value = node.nodeValue || "";
  const match = BUBBLE.exec(value);
  if (!match) return;
  node.nodeValue = value.slice(0, match.index) + insert + value.slice(match.index + match[0].length);
}

function replaceDraftLine(text, insert) {
  const idx = text.search(BUBBLE);
  if (idx < 0) return text;
  const start = text.lastIndexOf("\n", idx - 1) + 1;
  let end = text.indexOf("\n", idx);
  if (end < 0) end = text.length;
  return text.slice(0, start) + insert + text.slice(end);
}

function lineNode(text) {
  const div = document.createElement("div");
  if (text) div.textContent = text;
  else div.appendChild(document.createElement("br"));
  return div;
}

function swapLine(line, insert) {
  const parent = line.parentNode;
  if (!parent) return;
  const parts = String(insert).replace(/\r\n/g, "\n").split("\n");
  const fragment = document.createDocumentFragment();
  parts.forEach((part) => fragment.appendChild(lineNode(part)));
  parent.insertBefore(fragment, line);
  parent.removeChild(line);
}

function writeLines(root, text) {
  const parts = String(text).replace(/\r\n/g, "\n").split("\n");
  while (root.firstChild) root.removeChild(root.firstChild);
  parts.forEach((part) => root.appendChild(lineNode(part)));
}

function notify(root, data) {
  try {
    root.focus();
  } catch (error) {
    /* The compose box can be detached between poll and write. */
  }
  const init = { bubbles: true, cancelable: true, inputType: "insertText", data: data || "" };
  root.dispatchEvent(new InputEvent("beforeinput", init));
  root.dispatchEvent(new InputEvent("input", init));
}

function remember(body) {
  const state = states.get(body) || { text: "", timer: 0, retry: 0, lastPosted: "", posting: false };
  const text = readBody(body);
  state.text = text;
  state.lastPosted = JSON.stringify(fieldsOf(body, text));
  clearTimeout(state.timer);
  clearTimeout(state.retry);
  states.set(body, state);
}

function applyInsert(root, insert, mode) {
  if (!root || !insert) return false;
  const draft = mode === "draft";
  const textNode = findBubbleText(root);
  if (!draft && textNode) {
    replaceToken(textNode, insert);
    notify(root, insert);
    remember(root);
    return true;
  }
  const line = textNode ? lineElement(textNode, root) : null;
  if (line && line !== root && isSingleLine(line)) {
    if (draft) swapLine(line, insert);
    else line.textContent = (line.textContent || "").replace(BUBBLE, () => insert);
    notify(root, insert);
    remember(root);
    return true;
  }
  const current = readBody(root);
  if (!BUBBLE.test(current)) return false;
  writeLines(root, draft ? replaceDraftLine(current, insert) : current.replace(BUBBLE, () => insert));
  notify(root, insert);
  remember(root);
  return true;
}

function tintCompose(bodies) {
  const paint = globalThis.paintMailBubble;
  if (typeof paint !== "function") return;
  try {
    paint(bodies || findComposeBodies());
  } catch (error) {
    /* The compose box can be rebuilt while the ranges are collected. */
  }
}

function bubbleBody(bodies) {
  const active = document.activeElement && document.activeElement.closest
    ? document.activeElement.closest('div[contenteditable="true"]')
    : null;
  if (active && bodies.indexOf(active) !== -1 && BUBBLE.test(readBody(active))) return active;
  for (let i = 0; i < bodies.length; i += 1) {
    if (BUBBLE.test(readBody(bodies[i]))) return bodies[i];
  }
  return null;
}

document.addEventListener("input", (event) => {
  const target = event.target;
  if (!target || !target.closest) return;
  const body = target.closest('div[contenteditable="true"]');
  if (isComposeBody(body)) {
    noteChange(body);
    tintCompose();
  }
}, true);

setInterval(() => {
  const bodies = findComposeBodies();
  bodies.forEach(noteChange);
  tintCompose(bodies);
}, 200);

setInterval(() => {
  const bodies = findComposeBodies();
  if (!bodies.length) return;
  const target = bubbleBody(bodies);
  ask({ type: "mail-insert", canApply: !!target }).then((response) => {
    if (!response || !response.insert) return;
    const current = bubbleBody(findComposeBodies());
    if (!current) {
      ask({ type: "mail-hold", insert: response.insert, mode: response.mode });
      return;
    }
    const applied = applyInsert(current, response.insert, response.mode === "draft" ? "draft" : "sentence");
    if (!applied) ask({ type: "mail-hold", insert: response.insert, mode: response.mode });
  });
}, POLL_MS);
