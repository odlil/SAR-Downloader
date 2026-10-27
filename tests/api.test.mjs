import test from "node:test";
import assert from "node:assert/strict";
import { api, ApiError } from "../web/api.js";
import { normalizeBasemap } from "../web/preferences.js";

test("API encodes mutations and preserves server validation errors", async (t) => {
  let sent;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    sent = { url, options };
    return {
      ok: false,
      status: 400,
      json: async () => ({ error: "invalid fixture" }),
    };
  });
  await assert.rejects(
    api("/api/basemap", { provider: "esri" }),
    (error) =>
      error instanceof ApiError &&
      error.status === 400 &&
      error.message === "invalid fixture",
  );
  assert.equal(sent.options.method, "POST");
  assert.deepEqual(JSON.parse(sent.options.body), { provider: "esri" });
});

test("API timeout releases a hanging request and permits a later retry", async (t) => {
  t.mock.method(
    globalThis,
    "fetch",
    (url, { signal }) =>
      new Promise((resolve, reject) => {
        signal.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      }),
  );
  await assert.rejects(
    api("/api/downloads", undefined, { timeout: 5 }),
    /超时/,
  );
  globalThis.fetch.mock.mockImplementation(async () => ({
    ok: true,
    json: async () => ({ tasks: [] }),
  }));
  assert.deepEqual(await api("/api/downloads"), { tasks: [] });
});

test("API preserves user cancellation and gives explicit upgrade or format errors", async (t) => {
  t.mock.method(globalThis, "fetch", async (url, { signal }) => {
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    return { ok: false, status: 501 };
  });
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(api("/api/search", {}, { signal: controller.signal }), {
    name: "AbortError",
  });
  await assert.rejects(api("/api/account"), /新版接口/);
  globalThis.fetch.mock.mockImplementation(async () => ({
    ok: true,
    status: 200,
    json: async () => {
      throw new SyntaxError("fixture");
    },
  }));
  await assert.rejects(api("/api/account"), /格式异常/);
});

test("Basemap recovery keeps the provider and matching key together", () => {
  assert.deepEqual(
    normalizeBasemap({
      provider: "esri",
      kind: "satellite",
      keys: { esri: "fixture-esri", tianditu: "fixture-tdt" },
    }),
    {
      provider: "esri",
      kind: "satellite",
      keys: { esri: "fixture-esri", tianditu: "fixture-tdt" },
    },
  );
  assert.equal(
    normalizeBasemap({
      provider: "esri",
      kind: "satellite",
      keys: { tianditu: "fixture-tdt" },
    }).kind,
    "osm",
  );
  assert.equal(normalizeBasemap(null).provider, "tianditu");
});
