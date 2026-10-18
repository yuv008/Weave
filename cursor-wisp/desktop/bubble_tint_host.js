"use strict";

// Installs bubble_tint.js into the live Cursor composer.
// Uses the main process's own Node inspector, then leaves that port open
// so a later pass can reinstall after the window reloads. "close" stops it.

const http = require("http");
const fs = require("fs");

const pid = Number(process.argv[2]);
const scriptPath = process.argv[3];
const mode = process.argv[4] || "";
const closing = mode === "close";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getText(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let body = "";
      res.on("data", (chunk) => {
        body += chunk;
      });
      res.on("end", () => {
        if (res.statusCode !== 200) reject(new Error("http " + res.statusCode));
        else resolve(body);
      });
    });
    req.on("error", reject);
    req.setTimeout(700, () => {
      req.destroy();
      reject(new Error("timeout"));
    });
  });
}

function connect(wsurl) {
  const ws = new WebSocket(wsurl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (error) { return; }
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
  });
  const ready = new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve);
    ws.addEventListener("error", () => reject(new Error("ws")));
  });
  function call(method, params, timeoutMs) {
    const mine = ++id;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(mine);
        reject(new Error("timeout"));
      }, timeoutMs || 8000);
      pending.set(mine, (msg) => {
        clearTimeout(timer);
        resolve(msg);
      });
      ws.send(JSON.stringify({ id: mine, method, params }));
    });
  }
  return { ws, ready, call };
}

async function evalValue(call, expression) {
  const msg = await call("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  const details = msg.result && msg.result.exceptionDetails;
  if (details) {
    const text = details.exception && details.exception.description;
    throw new Error(text ? String(text).split("\n")[0].slice(0, 180) : "eval");
  }
  const result = msg.result && msg.result.result;
  if (!result) throw new Error("empty");
  return result.value;
}

async function inspectorFor(targetPid) {
  for (let port = 9229; port <= 9236; port += 1) {
    let list;
    try {
      list = JSON.parse(await getText("http://127.0.0.1:" + port + "/json/list"));
    } catch (error) {
      continue;
    }
    if (!Array.isArray(list)) continue;
    const nodeTarget = list.find((item) => item.type === "node" && item.webSocketDebuggerUrl);
    if (!nodeTarget) continue;
    const session = connect(nodeTarget.webSocketDebuggerUrl);
    try {
      await session.ready;
      const seen = await evalValue(session.call, "process.pid");
      session.ws.close();
      if (seen === targetPid) return nodeTarget.webSocketDebuggerUrl;
    } catch (error) {
      try { session.ws.close(); } catch (ignore) { /* already closed */ }
    }
  }
  return "";
}

async function openInspector(targetPid) {
  let wsurl = await inspectorFor(targetPid);
  if (wsurl) return wsurl;
  process._debugProcess(targetPid);
  for (let attempt = 0; attempt < 25; attempt += 1) {
    await sleep(120);
    wsurl = await inspectorFor(targetPid);
    if (wsurl) return wsurl;
  }
  return "";
}

function report(payload) {
  process.stdout.write(JSON.stringify(payload) + "\n");
}

async function main() {
  if (!pid || !scriptPath) {
    report({ ok: false, error: "args" });
    return;
  }
  const wsurl = await openInspector(pid);
  if (!wsurl) {
    report({ ok: false, error: "no-inspector" });
    return;
  }
  const session = connect(wsurl);
  await session.ready;
  if (closing) {
    try {
      await evalValue(session.call, "typeof process._debugEnd === 'function' && process._debugEnd()");
    } catch (error) { /* the port drops as it closes */ }
    try { session.ws.close(); } catch (error) { /* already closed */ }
    report({ ok: true, closed: true });
    return;
  }
  const source = fs.readFileSync(scriptPath, "utf8");
  let invoke = "return installWeaveBubbleTint();";
  if (mode === "replace") {
    const answer = Buffer.from(process.argv[5] || "", "base64").toString("utf8");
    invoke = "return replaceWeaveBubbleSentence(" + JSON.stringify(answer) + ");";
  }
  const wrapped = "(function(module, exports){\n" + source + "\n" + invoke + "\n})(undefined, undefined);";
  const expression = `
(async () => {
  const electron = process.mainModule.require("electron");
  const page = ${JSON.stringify(wrapped)};
  const targets = electron.webContents.getAllWebContents().filter((wc) => {
    if (wc.isDestroyed()) return false;
    return String(wc.getURL() || "").indexOf("workbench.html") !== -1;
  });
  const results = [];
  for (const wc of targets) {
    results.push(await wc.executeJavaScript(page, true));
  }
  return results;
})()`;
  const results = await evalValue(session.call, expression);
  session.ws.close();
  const list = Array.isArray(results) ? results : [];
  const first = (mode === "replace" ? list.find((item) => item && item.ok) : null) || list[0] || null;
  if (!first || !first.ok) {
    report({ ok: false, error: (first && first.error) || "no-window" });
    return;
  }
  report({
    ok: true,
    color: first.color,
    rule: first.rule,
    count: first.count,
    tokens: first.tokens,
    inComposer: first.inComposer,
    replaced: first.replaced,
  });
}

main().catch((error) => {
  report({ ok: false, error: String(error && error.message ? error.message : error).slice(0, 180) });
  process.exitCode = 1;
});
