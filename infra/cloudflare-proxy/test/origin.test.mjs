// Regression tests for the origin-escape bug (open proxy via a leading "//" path).
// Run: node --test infra/cloudflare-proxy/test/origin.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "../src/index.js";

const ORIGIN = "https://oss-radar-dashboard-wzpckox4zq-uc.a.run.app";
const env = { ORIGIN };

async function upstreamFor(path, init = {}) {
  const calls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url) => { calls.push(new URL(String(url))); return new Response("ok"); };
  try {
    const res = await worker.fetch(new Request(`https://radar.miladblog.com${path}`, init), env);
    return { res, upstream: calls[0] };
  } finally {
    globalThis.fetch = realFetch;
  }
}

for (const path of ["//attacker.example/leak", "///attacker.example/leak", "/\\attacker.example/x",
                    "//attacker.example", "/%2F%2Fattacker.example/x"]) {
  test(`never leaves the configured origin: ${path}`, async () => {
    const { upstream } = await upstreamFor(path);
    assert.ok(upstream, "request should still be served from the dashboard origin");
    assert.equal(upstream.origin, ORIGIN);
  });
}

test("normal paths and query strings are preserved", async () => {
  const { upstream } = await upstreamFor("/api/package/vllm?a=1&b=2");
  assert.equal(upstream.href, `${ORIGIN}/api/package/vllm?a=1&b=2`);
});

test("only GET/HEAD and POST /api/audit are allowed", async () => {
  assert.equal((await upstreamFor("/api/overview", { method: "POST" })).res.status, 405);
  assert.equal((await upstreamFor("/api/audit", { method: "POST", body: "{}" })).res.status, 200);
  assert.equal((await upstreamFor("/x", { method: "DELETE" })).res.status, 405);
});
