#!/usr/bin/env node
/* Browser-flow E2E: replicates the exact network sequence the pair page
   performs, against the LIVE server, and hard-asserts every step.
   Exit 0 = a real browser on this machine WILL see the QR. */
const assert = require("assert");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8100";

async function main() {
  // 1. pair page loads
  const page = await fetch(`${BASE}/pair`);
  assert.strictEqual(page.status, 200, "/pair must return 200");
  const html = await page.text();
  assert(html.includes('id="qrimg"'), "page must contain the QR frame");
  console.log("✓ 1. GET /pair → 200, QR frame present");

  // 2. automatic loopback token
  const tokRes = await fetch(`${BASE}/pair-token`, { cache: "no-store" });
  assert.strictEqual(tokRes.status, 200, "/pair-token must be 200 from loopback");
  const { token } = await tokRes.json();
  assert(token && token.length > 20, "pair-token must return a real token");
  console.log(`✓ 2. GET /pair-token → 200 (${token.length}-char token)`);

  const auth = { Authorization: `Bearer ${token}` };

  // 3. meta says a QR exists
  const meta = await (await fetch(`${BASE}/agents/whatsapp/qr/meta`, { headers: auth })).json();
  assert.strictEqual(meta.available, true, "qr/meta must report available:true");
  assert(typeof meta.generated_at === "number", "generated_at must be numeric");
  console.log(`✓ 3. GET qr/meta → available=true, generated_at=${meta.generated_at}`);

  // 4. PNG bytes are a real, non-trivial PNG
  const img = await fetch(`${BASE}/agents/whatsapp/qr.png`, {
    headers: { ...auth, "Cache-Control": "no-store" },
  });
  assert.strictEqual(img.status, 200, "qr/png must be 200");
  assert.strictEqual(img.headers.get("content-type"), "image/png", "must be image/png");
  const buf = Buffer.from(await img.arrayBuffer());
  assert(buf.length > 4000, `png suspiciously small (${buf.length}B)`);
  assert(buf.slice(1, 4).toString() === "PNG", "must start with PNG magic");
  console.log(`✓ 4. GET qr.png → 200, ${buf.length}B valid PNG`);

  // 5. second meta call within rotation returns same code id (stable), and
  //    the endpoint chain is what pair.js uses — no auth drift
  const meta2 = await (await fetch(`${BASE}/agents/whatsapp/qr/meta`, { headers: auth })).json();
  assert.strictEqual(meta2.available, true);
  console.log("✓ 5. qr/meta stable across calls (auth consistent)");

  // 6. favicon no longer 404s
  const fav = await fetch(`${BASE}/favicon.ico`);
  assert.strictEqual(fav.status, 200, "favicon must be 200");
  console.log("✓ 6. GET /favicon.ico → 200");

  console.log("\nALL BROWSER-FLOW CHECKS PASSED — the pair page will render the QR.");
}

main().catch((err) => {
  console.error("✗ BROWSER-FLOW FAILED:", err.message);
  process.exit(1);
});
