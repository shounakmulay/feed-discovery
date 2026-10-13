import assert from "node:assert/strict";
import test from "node:test";
import worker from "../cloudflare/_worker.js";

const key = "test-only-not-a-production-secret";
const collection = "/collections/engineering/" + "a".repeat(64) + ".json";

test("Missing and incorrect keys cannot read any route or invoke assets", async () => {
  for (const path of ["/", "/master.json", collection, "/404.html", "/missing", "/_worker.js"]) {
    for (const method of ["GET", "HEAD", "OPTIONS", "POST"]) {
      for (const supplied of [undefined, "", "wrong", key + "-suffix"]) {
        const headers = supplied === undefined ? {} : { "X-API-Key": supplied };
        const response = await worker.fetch(new Request("https://catalog.test" + path, { method, headers }), {
          FEED_DISCOVERY_API_KEY: key,
          ASSETS: { fetch: () => assert.fail("Unauthorized request reached assets") },
        });
        assert.equal(response.status, 401);
        assert.equal(response.headers.get("Cache-Control"), "no-store");
      }
    }
  }
});

test("Missing or empty configured secret fails closed", async () => {
  for (const configured of [undefined, ""]) {
    const response = await worker.fetch(new Request("https://catalog.test/master.json", {
      headers: { "X-API-Key": key },
    }), {
      FEED_DISCOVERY_API_KEY: configured,
      ASSETS: { fetch: () => assert.fail("Unconfigured secret reached assets") },
    });
    assert.equal(response.status, 401);
  }
});

test("Authorized master and root preserve the body and ETag with private revalidation", async () => {
  for (const path of ["/", "/master.json"]) {
    const response = await worker.fetch(new Request("https://catalog.test" + path, {
      headers: { "x-api-key": key },
    }), {
      FEED_DISCOVERY_API_KEY: key,
      ASSETS: { fetch: async request => {
        assert.equal(new URL(request.url).pathname, "/master.json");
        return new Response('{"collections":[]}', { headers: { ETag: '"catalog-v1"' } });
      } },
    });
    assert.equal(response.status, 200);
    assert.equal(await response.text(), '{"collections":[]}');
    assert.equal(response.headers.get("ETag"), '"catalog-v1"');
    assert.equal(response.headers.get("Cache-Control"), "private, max-age=0, must-revalidate");
    assert.match(response.headers.get("Vary"), /X-API-Key/i);
    assert.equal(response.headers.get("CDN-Cache-Control"), "no-store");
  }
});

test("Authorized conditional requests preserve 304 and conditional headers", async () => {
  const response = await worker.fetch(new Request("https://catalog.test/master.json", {
    headers: { "X-API-Key": key, "If-None-Match": '"catalog-v1"' },
  }), {
    FEED_DISCOVERY_API_KEY: key,
    ASSETS: { fetch: async request => {
      assert.equal(request.headers.get("If-None-Match"), '"catalog-v1"');
      return new Response(null, { status: 304, headers: { ETag: '"catalog-v1"' } });
    } },
  });
  assert.equal(response.status, 304);
  assert.equal(await response.text(), "");
  assert.equal(response.headers.get("ETag"), '"catalog-v1"');
});

test("Authorized collections remain immutable only in private caches", async () => {
  const response = await worker.fetch(new Request("https://catalog.test" + collection, {
    headers: { "X-API-Key": key },
  }), {
    FEED_DISCOVERY_API_KEY: key,
    ASSETS: { fetch: async () => new Response("{}", { headers: { "Cache-Control": "public, max-age=31536000" } }) },
  });
  assert.equal(response.headers.get("Cache-Control"), "private, max-age=31536000, immutable");
  assert.equal(response.headers.get("Cloudflare-CDN-Cache-Control"), "no-store");
});
