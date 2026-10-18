/* Posts the compose box to the pet and polls the one-shot Accept insert.
   This does not call Groq. The email body is not logged. */

const MAIL_URL = "http://127.0.0.1:8766/mail";
const INSERT_URL = "http://127.0.0.1:8766/mail-insert";

let held = null;
let fetching = false;

function empty() {
  return { insert: "", mode: "" };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message !== "object") return;
  if (message.type === "mail") {
    postMail(message.payload).then(sendResponse);
    return true;
  }
  if (message.type === "mail-insert") {
    pollInsert(!!message.canApply).then(sendResponse);
    return true;
  }
  if (message.type === "mail-hold") {
    const insert = typeof message.insert === "string" ? message.insert : "";
    if (insert) {
      held = { insert: insert, mode: message.mode === "draft" ? "draft" : "sentence" };
    }
    sendResponse(empty());
    return;
  }
});

async function postMail(payload) {
  const body = {
    to: String((payload && payload.to) || ""),
    subject: String((payload && payload.subject) || ""),
    body: String((payload && payload.body) || ""),
  };
  try {
    const response = await fetch(MAIL_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return { ok: response.ok };
  } catch (error) {
    return { ok: false };
  }
}

async function fetchInsert() {
  const response = await fetch(INSERT_URL, { cache: "no-store" });
  if (response.status === 204) return empty();
  if (!response.ok) return empty();
  const data = await response.json();
  const insert = data && typeof data.insert === "string" ? data.insert : "";
  if (!insert) return empty();
  return { insert: insert, mode: data.mode === "draft" ? "draft" : "sentence" };
}

async function pollInsert(canApply) {
  if (held) {
    if (!canApply) return empty();
    const item = held;
    held = null;
    return item;
  }
  if (fetching) return empty();
  fetching = true;
  try {
    const item = await fetchInsert();
    if (item.insert) held = item;
  } catch (error) {
    held = null;
  } finally {
    fetching = false;
  }
  if (held && canApply) {
    const item = held;
    held = null;
    return item;
  }
  return empty();
}
