/**
 * Full-page screenshot of a local page over CDP.
 *
 * Chrome's `--screenshot` flag captures only the viewport; the README wants the whole
 * page.  This drives Page.captureScreenshot with captureBeyondViewport and sizes the clip
 * to the page.
 *
 * Pass the width the committed image used (2826 CSS px, dpr 1).  The index's card grid is
 * `auto-fill`, so a narrower emulated viewport re-flows it -- at 1413 CSS px the same page
 * is two columns and twice as tall.
 *
 * Careful with the two scale knobs: `clip.scale` MULTIPLIES `deviceScaleFactor`, so putting
 * the same value in both gives a 4x image.  The device pixel ratio goes in the metrics
 * override and the clip scale stays 1.
 *
 * Usage: node shot.js <url> <out.png> [widthCss] [dpr]
 */
const url = process.argv[2];
const out = process.argv[3];
const width = Number(process.argv[4] || 1413);
const dpr = Number(process.argv[5] || 2);

const https = require("http");
const fs = require("fs");

function get(path) {
  return new Promise((resolve, reject) => {
    https.get({ host: "127.0.0.1", port: 9222, path }, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => resolve(JSON.parse(body)));
    }).on("error", reject);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const targets = await get("/json");
  const page = targets.find((t) => t.type === "page");
  if (!page) throw new Error("no page target on 9222");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const events = [];
  ws.addEventListener("message", (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    } else if (msg.method) {
      events.push(msg.method);
    }
  });
  await new Promise((r) => ws.addEventListener("open", r));
  const send = (method, params = {}) =>
    new Promise((resolve) => {
      const myId = ++id;
      pending.set(myId, resolve);
      ws.send(JSON.stringify({ id: myId, method, params }));
    });

  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width, height: 900, deviceScaleFactor: dpr, mobile: false,
  });
  const loaded = [];
  const onLoad = (m) => {
    if (JSON.parse(m.data).method === "Page.loadEventFired") loaded.push(1);
  };
  ws.addEventListener("message", onLoad);
  await send("Page.navigate", { url });
  for (let i = 0; i < 60 && !loaded.length; i++) await sleep(100);
  await sleep(2500);                       // the star script fetches /favorites.json

  const measured = await send("Runtime.evaluate", {
    expression: "document.documentElement.scrollHeight",
    returnByValue: true,
  });
  const height = measured.result.result.value;
  console.log(`  page height: ${height} css px at ${width} wide`);
  const shot = await send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: true,
    clip: { x: 0, y: 0, width, height, scale: 1 },
  });
  fs.writeFileSync(out, Buffer.from(shot.result.data, "base64"));
  console.log(`  wrote ${out}: ${fs.statSync(out).size.toLocaleString()} bytes`);
  process.exit(0);
})().catch((err) => {
  console.error("  FAILED:", err.message);
  process.exit(1);
});
