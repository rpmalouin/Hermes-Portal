/**
 * Prove the page's controls actually work, in a real browser.
 *
 * Why this exists: two bugs shipped that every unit test passed through.  The tests assert
 * markup, headers and server behaviour, and none of that is "does the page run" --
 *
 *   1. a Content-Security-Policy of `script-src 'unsafe-inline'` does not cover an external
 *      <script src>, so the browser refused /app.js and every control died (only the search
 *      form still worked, being plain HTML);
 *   2. the <script src> sat above the markup it wires and was not deferred, so it ran when
 *      every element it looked for was null -- silently, since each guard was `if (element)`.
 *
 * Both are invisible to a header assertion and obvious to a click.  So: click.
 *
 * Usage:  node tools/click_check.js [url]
 * Needs a Chromium already listening on the CDP port (see .github/workflows/tests.yml).
 * Exits non-zero if any control does not respond.
 */
const http = require("http");

const url = process.argv[2] || "http://127.0.0.1:8087/";
const cdpPort = Number(process.env.CDP_PORT || 9222);
const cdpHost = process.env.CDP_HOST || "127.0.0.1";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function cdpList(path) {
  return new Promise((resolve, reject) => {
    http
      .get({ host: cdpHost, port: cdpPort, path }, (res) => {
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (err) {
            reject(new Error(`CDP ${path} did not return JSON: ${body.slice(0, 80)}`));
          }
        });
      })
      .on("error", reject);
  });
}

const failures = [];
function report(name, detail, ok) {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? ` -- ${detail}` : ""}`);
  if (!ok) failures.push(name);
}

(async () => {
  const targets = await cdpList("/json");
  const page = targets.find((target) => target.type === "page");
  if (!page) throw new Error("no page target on the CDP port");

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const consoleErrors = [];
  ws.addEventListener("message", (message) => {
    const msg = JSON.parse(message.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
      return;
    }
    if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
      consoleErrors.push(msg.params.entry.text.slice(0, 160));
    }
  });
  await new Promise((resolve) => ws.addEventListener("open", resolve));
  const send = (method, params = {}) =>
    new Promise((resolve) => {
      const mine = ++id;
      pending.set(mine, resolve);
      ws.send(JSON.stringify({ id: mine, method, params }));
    });

  await send("Log.enable");
  await send("Page.enable");
  await send("Page.navigate", { url });
  await sleep(3000);

  const evaluate = async (expression, awaitPromise = false) => {
    const res = await send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise,
    });
    if (res.result && res.result.exceptionDetails) {
      throw new Error(`evaluate failed: ${res.result.exceptionDetails.text}`);
    }
    return res.result.result.value;
  };

  report("the page loads", await evaluate("document.title"), (await evaluate("document.title")) === "Hermes Portal");
  report("the refresh button is in the header", "", await evaluate('!!document.getElementById("refresh")'));

  // 1. the theme toggle: its listener exists only if /app.js ran at all
  const before = await evaluate('document.documentElement.dataset.theme');
  await evaluate('document.getElementById("theme-toggle").click()');
  await sleep(300);
  const after = await evaluate('document.documentElement.dataset.theme');
  report("theme toggle responds (so /app.js ran)", `${before} -> ${after}`, before !== after);

  // 2. the refresh button: catch the toast before the reload it triggers
  const toast = await evaluate(
    `(async () => {
        document.getElementById("refresh").click();
        for (let i = 0; i < 60; i++) {
            const node = document.getElementById("toast");
            if (node && node.textContent) return node.textContent;
            await new Promise(r => setTimeout(r, 25));
        }
        return "";
    })()`,
    true,
  );
  report("refresh button reaches the server", JSON.stringify(toast), /Re-read \d+ domains/.test(toast));

  // 3. the command palette
  const opened = await evaluate(
    `(async () => {
        document.getElementById("palette-open").click();
        await new Promise(r => setTimeout(r, 200));
        return !document.getElementById("palette").hidden;
    })()`,
    true,
  );
  report("command palette opens", `hidden=${!opened}`, opened === true);

  report("no console errors", consoleErrors[0] || "", consoleErrors.length === 0);

  console.log(
    failures.length ? `\n  ${failures.length} control(s) did not respond: ${failures.join(", ")}` : "\n  all controls respond",
  );
  process.exit(failures.length ? 1 : 0);
})().catch((err) => {
  console.error(`  FAIL  ${err.message}`);
  process.exit(1);
});
