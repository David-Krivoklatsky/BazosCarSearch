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
    getElementById: () => elementStub(),
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
