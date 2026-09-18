/**
 * Which element is making the page tall?  Measure it, do not guess.
 *
 * The README shot wants the same framing as the published image (~1263 CSS px tall).  When a
 * demo rebuild doubles that, the cause is one collection rendering more rows than it should --
 * the taxonomy tiles were the first instance -- so this prints every card's height.
 *
 * Usage: node heights.js <url>
 */
const http = require("http");
const get = (p) =>
  new Promise((res, rej) =>
    http
      .get({ host: "127.0.0.1", port: 9222, path: p }, (r) => {
        let b = "";
        r.on("data", (c) => (b += c));
        r.on("end", () => res(JSON.parse(b)));
      })
      .on("error", rej),
  );

const url = process.argv[2];

(async () => {
  const target = (await get("/json")).find((x) => x.type === "page");
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
  });
  await new Promise((r) => ws.addEventListener("open", r));
  const send = (method, params = {}) =>
    new Promise((res) => {
      const i = ++id;
      pending.set(i, res);
      ws.send(JSON.stringify({ id: i, method, params }));
    });

  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1413, height: 900, deviceScaleFactor: 1, mobile: false,
  });
  await send("Page.navigate", { url });
  await new Promise((r) => setTimeout(r, 3000));
  const evaluate = async (expression) =>
    (await send("Runtime.evaluate", { expression, returnByValue: true })).result.result.value;

  console.log(`  page height: ${await evaluate("document.documentElement.scrollHeight")} css px`);
  const rows = await evaluate(`
    Array.from(document.querySelectorAll("body *"))
      .map((el) => {
        const box = el.getBoundingClientRect();
        const name = el.tagName.toLowerCase() + "." +
          String(el.className || "").split(" ").filter(Boolean).slice(0, 2).join(".");
        return [Math.round(box.height), Math.round(box.width), name];
      })
      .filter(([height]) => height > 240)
      .sort((a, b) => b[0] - a[0])
      .slice(0, 22)
  `);
  for (const [height, width, label] of rows) {
    console.log(`    h=${String(height).padStart(5)} w=${String(width).padStart(5)}  ${label}`);
  }
  process.exit(0);
})().catch((e) => {
  console.error("  FAILED:", e.message);
  process.exit(1);
});
