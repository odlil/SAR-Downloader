import test from "node:test";
import assert from "node:assert/strict";
import { createSearchController } from "../web/search-controller.js";
import { pollingDelay } from "../web/queue-view.js";

test("A late search cannot overwrite a newer search after cancellation", async (t) => {
  const elements = new Map();
  t.mock.method(
    globalThis,
    "fetch",
    () => new Promise((resolve) => replies.push(resolve)),
  );
  const replies = [];
  const previousDocument = globalThis.document;
  t.after(() => {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  });
  globalThis.document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, { setAttribute() {} });
      return elements.get(id);
    },
  };
  const state = {
    bounds: [86, 43, 87, 44],
    geometry: null,
    results: [],
    selected: new Set(),
  };
  const noop = () => {};
  const controller = createSearchController({
    state,
    getMap: () => ({ fitScenes: noop }),
    readFilters: () => ({
      dataset: "SENTINEL-1",
      bounds: state.bounds,
      product: "SLC",
    }),
    updateBounds: noop,
    setAOIMode: noop,
    clearActiveScene: noop,
    invalidateDEM: noop,
    renderResults: noop,
    persist: noop,
    setError: noop,
    setStatus: noop,
  });
  const earlier = controller.search(),
    later = controller.search();
  replies[1]({
    ok: true,
    json: async () => ({ scenes: [{ id: "latest", bounds: state.bounds }] }),
  });
  await later;
  replies[0]({
    ok: true,
    json: async () => ({ scenes: [{ id: "stale", bounds: state.bounds }] }),
  });
  await earlier;
  assert.equal(state.results[0].id, "latest");
  assert.equal(elements.get("search-button").disabled, false);
});

test("Idle and hidden queues poll less often while active downloads stay responsive", () => {
  assert.equal(pollingDelay([]), 5000);
  assert.equal(pollingDelay([{ state: "processing" }]), 1500);
  assert.equal(pollingDelay([{ state: "queued", busy: true }]), 1500);
  assert.equal(pollingDelay([{ state: "processing" }], true), 15000);
});

test("ALOS rejects an old local backend before catalog search and accepts supported options on a new backend", async (t) => {
  const previousDocument = globalThis.document;
  t.after(() => {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  });
  const elements = new Map();
  globalThis.document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { setAttribute() {} });
    return elements.get(id);
  } };
  let modern = false;
  const calls = [];
  t.mock.method(globalThis, "fetch", async (path, options) => {
    calls.push(path);
    return { ok: true, json: async () => path === "/api/health"
      ? { version: modern ? "1.0" : "0.6.7", capabilities: { alos: modern } }
      : { scenes: [], warnings: [] } };
  });
  const state = { bounds: [80.39279, 42.11068, 80.74108, 42.29353], geometry: null, results: [], selected: new Set() };
  let dataset, product;
  const noop = () => {};
  const controller = createSearchController({ state, getMap: () => null,
    readFilters: () => ({ dataset, product, bounds: state.bounds }),
    updateBounds: noop, setAOIMode: noop, clearActiveScene: noop, invalidateDEM: noop,
    renderResults: noop, persist: noop, setError: noop, setStatus: noop });
  for ([dataset, product] of [["ALOS-2", "L1.1"], ["ALOS", "L1.0"]]) {
    calls.length = 0;
    await controller.search();
    assert.deepEqual(calls, ["/api/health"]);
    assert.equal(state.searchPhase, "error");
    assert.match(state.searchMessage, /v0\.6\.7.*重新运行新版/);
    assert.equal(elements.get("search-button").disabled, false);
  }
  modern = true;
  calls.length = 0;
  await controller.search();
  assert.deepEqual(calls, ["/api/health", "/api/search"]);
  assert.equal(state.searchPhase, "done");
});

test("cancelled ALOS capability checks cannot start a stale catalog request", async (t) => {
  const previousDocument = globalThis.document;
  t.after(() => {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  });
  const elements = new Map();
  globalThis.document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { setAttribute() {} });
    return elements.get(id);
  } };
  let reply;
  t.mock.method(globalThis, "fetch", () => new Promise((resolve) => { reply = resolve; }));
  const state = { bounds: [80, 42, 81, 43], geometry: null, results: [], selected: new Set() };
  const noop = () => {};
  const controller = createSearchController({ state, getMap: () => null,
    readFilters: () => ({ dataset: "ALOS-2", product: "L1.1", bounds: state.bounds }),
    updateBounds: noop, setAOIMode: noop, clearActiveScene: noop, invalidateDEM: noop,
    renderResults: noop, persist: noop, setError: noop, setStatus: noop });
  const pending = controller.search();
  controller.cancelSearch();
  reply({ ok: true, json: async () => ({ capabilities: { alos: true } }) });
  await pending;
  assert.equal(globalThis.fetch.mock.callCount(), 1);
  assert.equal(elements.get("search-button").disabled, false);
});
