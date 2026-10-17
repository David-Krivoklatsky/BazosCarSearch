// Runtime tests for the Mini App SPA (dashboard/index.html).
//
// The SPA lives inline in the HTML and is otherwise covered only by
// text-level contract tests (test_spa_contract.py). Those cannot catch a
// syntax error or a broken formatter — exactly the class of bug that once
// shipped a whole page dead. This executes the real shipped script inside a
// stubbed DOM (node:vm) and asserts on the pure rendering helpers.
//
// Run: node --test tests/js   (also invoked by tests/unit/test_spa_runtime.py)

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const HTML = readFileSync(join(HERE, "..", "..", "dashboard", "index.html"), "utf8");

function elementStub() {
  return {
    innerHTML: "",
    textContent: "",
    value: "",
    style: { setProperty() {}, display: "" },
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
    focus() {},
    getAttribute() { return null; },
    setAttribute() {},
  };
}

function buildSandbox() {
  const elements = {};
  const el = id => (elements[id] ??= elementStub());
  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    URLSearchParams,
    alert() {},
    // A 401 makes the async bootstrap short-circuit to showErr() instead of
    // rendering pages, so the trailing load("listings") never needs real rows.
    fetch: () => Promise.resolve({ status: 401, ok: false, json: async () => ({}) }),
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.document = {
    getElementById: id => el(id),
    querySelectorAll: () => [],
    addEventListener() {},
    documentElement: { style: { setProperty() {} } },
  };
  return sandbox;
}

let cached = null;
function spa() {
  if (cached) return cached;
  const scripts = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  assert.ok(scripts.length, "no inline <script> found in dashboard/index.html");
  const code = scripts.sort((a, b) => b.length - a.length)[0];

  const sandbox = buildSandbox();
  vm.createContext(sandbox);
  // Throws on a syntax error (e.g. an unbalanced paren) — the real guard.
  vm.runInContext(code, sandbox, { filename: "dashboard/index.html <script>" });
  cached = {
    ctx: sandbox,
    fn: name => sandbox[name],
    eval: expr => vm.runInContext(expr, sandbox),
  };
  return cached;
}

test("shipped SPA script parses and executes", () => {
  const s = spa();
  assert.equal(typeof s.fn("esc"), "function");
  assert.equal(typeof s.fn("load"), "function");
  assert.equal(typeof s.fn("renderListings"), "function");
});

test("esc neutralises HTML metacharacters", () => {
  const esc = spa().fn("esc");
  assert.equal(esc(null), "");
  assert.equal(esc(0), "0");
  assert.equal(esc('<a href="x">&'), "&lt;a href=&quot;x&quot;&gt;&amp;");
  assert.equal(esc("it's a car"), "it&#39;s a car");  // single quote closes no attribute
});

test("eur renders Ask for unknown prices and euros otherwise", () => {
  const eur = spa().fn("eur");
  assert.equal(eur(null), "Ask");
  assert.equal(eur(""), "Ask");
  assert.ok(eur(12999).endsWith("€"), "price should be euro-denominated");
  assert.ok(!eur(12999).includes("Ask"));
});

test("km renders a placeholder for unknown mileage", () => {
  const km = spa().fn("km");
  assert.equal(km(null), "? km");
  assert.ok(km(150000).endsWith("km"));
});

test("extUrl only trusts http(s)", () => {
  const extUrl = spa().fn("extUrl");
  assert.equal(extUrl("https://auto.bazos.sk/inzerat/1.php"), true);
  assert.equal(extUrl("http://example.com"), true);
  assert.equal(extUrl("javascript:alert(1)"), false);
  assert.equal(extUrl("data:text/html,x"), false);
  assert.equal(extUrl(null), false);
  assert.equal(extUrl(5), false);
});

test("scoreBadge distinguishes unscored, part and scored", () => {
  const scoreBadge = spa().fn("scoreBadge");
  assert.ok(scoreBadge(null, true).includes("unscored"));
  assert.ok(scoreBadge(80, false).includes("part"));
  const scored = scoreBadge(80, true);
  assert.ok(scored.includes(">80<"), `expected score in ${scored}`);
  assert.ok(!scored.includes("part"));
});

test("badge forwards score and is_car", () => {
  const badge = spa().fn("badge");
  assert.ok(badge({ score: 87, is_car: true }).includes(">87<"));
  assert.ok(badge({ score: null }).includes("unscored"));
  assert.ok(badge({ score: 50, is_car: false }).includes("part"));
});

test("ago buckets timestamps instead of dumping raw days", () => {
  const ago = spa().fn("ago");
  const daysAgo = d => new Date(Date.now() - (d * 86400 + 60) * 1000).toISOString();
  assert.equal(ago(null), "never");
  assert.equal(ago(new Date().toISOString()), "just now");
  assert.equal(ago(daysAgo(3)), "3 d ago");
  assert.equal(ago(daysAgo(40)), "1 mo ago");
  assert.equal(ago(daysAgo(400)), "1 y ago");
});

test("staleBadge flags listings unseen for over a week", () => {
  const staleBadge = spa().fn("staleBadge");
  const daysAgo = d => new Date(Date.now() - (d * 86400 + 60) * 1000).toISOString();
  assert.equal(staleBadge(null), "");
  assert.equal(staleBadge(daysAgo(1)), "");
  assert.ok(staleBadge(daysAgo(10)).includes("unseen"));
});

test("spark and bars render placeholders and tracks", () => {
  const s = spa();
  assert.ok(s.fn("spark")([]).includes("no data"));
  assert.ok(s.fn("spark")([1, 2, 3]).includes("spark"));
  assert.ok(s.fn("bars")([{ k: "A", n: 3 }], "k", "n").includes("track"));
});

test("fmtDateDM renders Slovak short dates", () => {
  const fmtDateDM = spa().fn("fmtDateDM");
  assert.equal(fmtDateDM("2026-10-03"), "03.10.");
  assert.equal(fmtDateDM("2026-10-03T12:00:00+00:00"), "03.10.");
  assert.equal(fmtDateDM(null), "");
  assert.equal(fmtDateDM(""), "");
  assert.equal(fmtDateDM("garbage"), "");
});

test("fmtKmK compacts mileage to xK", () => {
  const fmtKmK = spa().fn("fmtKmK");
  assert.equal(fmtKmK(null), "?");
  assert.equal(fmtKmK(850), "850 km");
  assert.equal(fmtKmK(150000), "150K km");
  assert.equal(fmtKmK(199500), "200K km");
});

test("cardHtml puts the title full-width above photo, specs and price", () => {
  const s = spa();
  const html = s.fn("cardHtml")({
    ad_id: 7, title: "Skoda Octavia 1.9 TDI", url: "https://auto.bazos.sk/inzerat/7/x.php",
    price_eur: 5000, year: 2019, mileage_km: 150000, city: "BA",
    published_date: "2026-10-03", last_seen_at: "2026-10-16T00:00:00Z",
    image_urls: ["https://example.com/1.jpg"], score: 82, is_car: true,
  });
  const top = html.indexOf("toprow"), bot = html.indexOf("botrow");
  const title = html.indexOf('class="title"'), side = html.indexOf('class="side"');
  assert.ok(top !== -1 && bot !== -1 && top < bot, "title row must precede the body row");
  assert.ok(title !== -1 && side !== -1 && title < side, "title must precede price/buttons");
  assert.ok(html.includes("03.10."), "card date in dd.mm.");
  assert.ok(html.includes("150K km"));
  assert.ok(html.includes("1.jpg"), "photo rendered");
  assert.ok(html.includes(">82<"), "score badge rendered");
});

test("digest drops link to the in-app detail, not straight to Bazoš", () => {
  const html = spa().fn("digestHtml")({
    days: 7, new_total: 1, trend: [], queue_pending: 0,
    drops: [{ ad_id: 7, title: "Skoda", url: "https://auto.bazos.sk/inzerat/7/x.php",
              prev: 6000, now_price: 5000, city: "BA", pct: 16.7 }],
    top: [],
  });
  assert.ok(html.includes("openListing(7"), "drop title must open the in-app detail");
  assert.ok(!html.includes('href="https://auto.bazos.sk'), "drop title must not link Bazoš directly");
});

test("renderSaved reuses the shared card with photo and note", () => {
  const s = spa();
  s.eval(`savedIds = new Set([1])`);
  s.fn("renderSaved")({ items: [{
    ad_id: 1, title: "Skoda", url: "https://x/1", note: "call me",
    current_price: 4000, drop_pct: 20.0, last_seen_at: "2026-10-01T00:00:00Z",
    alert_below: 3000, image_urls: ["https://example.com/1.jpg"],
    year: 2019, mileage_km: 150000, city: "BA", score: 80, is_car: true,
  }], saved_ids: [1] });
  const html = s.eval(`document.getElementById("content").innerHTML`);
  assert.ok(html.includes("toprow"), "saved card uses the shared component");
  assert.ok(html.includes("1.jpg"), "saved card shows the photo");
  assert.ok(html.includes("Note: call me"));
  assert.ok(html.includes("watching below"));
  assert.ok(html.includes("unsave(1)"), "saved card offers Remove");
});

test("scopeLabel summarizes filter sets", () => {
  const scopeLabel = spa().fn("scopeLabel");
  assert.equal(scopeLabel(null), "defaults");
  assert.equal(scopeLabel({}), "whole catalog");
  assert.equal(scopeLabel({ query: "octavia", min_price: 1000, max_price: 9000 }),
    '"octavia" · €1000–9000');
  assert.equal(scopeLabel({ psc: "81101", distance_km: 100 }), "81101 +100 km");
  assert.equal(scopeLabel({ query: "", min_price: "" }), "whole catalog");
});

test("ratedBy shows which AI produced the score (short name, full id in title)", () => {
  const s = spa();
  assert.equal(s.fn("ratedBy")({ eval_model: null }), "");
  assert.equal(s.fn("ratedBy")({}), "");
  const html = s.fn("ratedBy")({ eval_model: "dots-studio/dots-3-note-preview:free" });
  assert.ok(html.includes("AI: dots-3-note-preview"), html);
  assert.ok(html.includes('title="dots-studio/dots-3-note-preview:free"'));
});

test("removedBadge flags sold or unlisted cars", () => {
  const s = spa();
  assert.equal(s.fn("removedBadge")({ removed: false }), "");
  assert.ok(s.fn("removedBadge")({ removed: true }).includes("removed from Bazoš"));
});

test("cards carry the rated-by badge and the removed flag", () => {
  const html = spa().fn("cardHtml")({
    ad_id: 9, title: "Skoda", url: "https://x/9", price_eur: 1000, year: 2010,
    mileage_km: 100000, city: "BA", published_date: "2026-10-03",
    image_urls: [], score: 70, eval_model: "a/b-model", removed: true,
  });
  assert.ok(html.includes("AI: b-model"));
  assert.ok(html.includes("removed from Bazoš"));
});

test("view aliases keep legacy deep-links working", () => {
  const aliases = JSON.parse(spa().eval("JSON.stringify(VIEW_ALIASES)"));
  assert.equal(aliases.overview, "insights");
  assert.equal(aliases.stats, "insights");
  assert.equal(aliases.queue, "insights");
  assert.equal(aliases.inventory, "listings");
  assert.equal(aliases.profiles, "profile");
  const known = JSON.parse(spa().eval("JSON.stringify(KNOWN_VIEWS)"));
  assert.deepEqual(known.sort(), ["insights", "listings", "profile", "saved"]);
});
