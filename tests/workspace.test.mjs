import test from "node:test";
import assert from "node:assert/strict";
import { queueRows } from "../web/queue-view.js";
import {
  createWorkspaceStore,
  pickWorkspace,
  VIEW_FIELDS,
  DATA_FIELDS,
  startupMapTarget,
  successfulSearchBounds,
  mergeSearchLocation,
  searchAreaKey,
  slotUsesArea,
  migrateSharedWorkspace,
  workspaceForms,
} from "../web/workspace-state.js";

test("Old browser forms cannot overwrite the server-owned basemap provider", () => {
  const old = { "satellite-provider": "tianditu", product: "RSLC" };
  assert.deepEqual(workspaceForms(old), { product: "RSLC" });
  assert.equal(old["satellite-provider"], "tianditu");
});

const importedArea = {
  type: "Polygon",
  coordinates: [
    [
      [86, 43],
      [87, 43],
      [87, 44],
      [86, 44],
      [86, 43],
    ],
    [
      [86.2, 43.2],
      [86.2, 43.4],
      [86.4, 43.4],
      [86.4, 43.2],
      [86.2, 43.2],
    ],
  ],
};
test("Sensor results can be restored only for the current shared full geometry", () => {
  const bounds = [86, 43, 87, 44],
    slot = {
      aoiKey: searchAreaKey(bounds, importedArea),
      results: [{ id: "scene" }],
    };
  assert.equal(slotUsesArea(slot, bounds, structuredClone(importedArea)), true);
  assert.equal(slotUsesArea(slot, bounds, null), false);
  assert.equal(slotUsesArea(slot, [86.1, 43, 87, 44], importedArea), false);
  assert.equal(
    slotUsesArea({ bounds, geometry: importedArea }, bounds, importedArea),
    true,
  );
});
test("Upgrade recovers an imported sensor AOI when another sensor shows the initial rectangle", () => {
  const snapshot = {
    dataset: "NISAR",
    bounds: [102.5, 29, 106, 32.5],
    geometry: null,
    results: [{ id: "old-nisar" }],
    selected: ["old-nisar"],
    forms: {
      west: "102.5",
      "include-precise": true,
      "include-restituted": true,
    },
    searchSlots: {
      sentinel: {
        bounds: [86, 43, 87, 44],
        geometry: importedArea,
        forms: { "include-precise": true, "include-restituted": false },
      },
    },
  };
  const migrated = migrateSharedWorkspace(snapshot);
  assert.deepEqual(migrated.bounds, [86, 43, 87, 44]);
  assert.deepEqual(migrated.geometry, importedArea);
  assert.equal(migrated.forms.west, "86");
  assert.equal(migrated.sharedAOI, true);
  assert.deepEqual(migrated.results, []);
  assert.deepEqual(migrated.selected, []);
  assert.equal(migrated.forms["include-orbit"], true);
  assert.equal(migrated.forms["include-restituted"], undefined);
  assert.equal(migrated.searchSlots.sentinel.forms["include-orbit"], true);
  assert.deepEqual(snapshot.results, [{ id: "old-nisar" }]);
});
test("Shared AOI never reverts to an older imported shape and preserves the no-orbit choice", () => {
  const snapshot = {
    sharedAOI: true,
    bounds: [1, 2, 3, 4],
    geometry: null,
    forms: { "include-precise": false, "include-restituted": false },
    searchSlots: {
      sentinel: { bounds: [86, 43, 87, 44], geometry: importedArea },
    },
  };
  const migrated = migrateSharedWorkspace(snapshot);
  assert.deepEqual(migrated.bounds, [1, 2, 3, 4]);
  assert.equal(migrated.geometry, null);
  assert.equal(migrated.forms["include-orbit"], false);
  assert.equal(
    slotUsesArea(
      migrated.searchSlots.sentinel,
      migrated.bounds,
      migrated.geometry,
    ),
    false,
  );
});

test("Real DEM parameters never classify a runnable task as a legacy preview", () => {
  const dem = {
    id: "dem:fixture",
    kind: "dem-process",
    state: "queued",
    plan: { height: "ellipsoid", format: "GTiff" },
  };
  const rows = queueRows(
    [dem],
    [
      { ...dem, kind: "dem" },
      { id: "old", kind: "dem" },
    ],
  );
  assert.equal(rows.length, 2);
  assert.equal(rows[0].legacyPlan, false);
  assert.deepEqual(rows[0].plan, dem.plan);
  assert.equal(rows[1].legacyPlan, true);
});

test("New tabs focus the last successful search while reload preserves a manual map view", () => {
  const snapshot = {
    bounds: [102, 29, 106, 33],
    lastSearchBounds: [85, 42, 88, 45],
    mapView: { center: [10, 20], zoom: 8 },
    searchPhase: "error",
  };
  assert.deepEqual(startupMapTarget(snapshot), {
    kind: "bounds",
    bounds: [85, 42, 88, 45],
  });
  assert.deepEqual(startupMapTarget(snapshot, true), {
    kind: "view",
    view: snapshot.mapView,
  });
  assert.deepEqual(
    successfulSearchBounds({
      searchPhase: "done",
      results: [{ bounds: [85, 42, 86, 43] }, { bounds: [86, 43, 88, 45] }],
    }),
    [85, 42, 88, 45],
  );
  assert.deepEqual(startupMapTarget({ bounds: [1, 2, 3, 4] }), {
    kind: "bounds",
    bounds: [1, 2, 3, 4],
  });
  assert.equal(startupMapTarget(null), null);
});

test("A fresh browser restores the local server location without replacing cached filters", () => {
  const location = {
    bounds: [85, 42, 88, 45],
    aoiBounds: [86, 43, 87, 44],
    updatedAt: 200,
  };
  assert.deepEqual(mergeSearchLocation(null, location), {
    bounds: location.aoiBounds,
    lastSearchBounds: location.bounds,
    lastSearchAt: 200,
  });
  const cached = {
    bounds: [1, 2, 3, 4],
    forms: { dataset: "NISAR" },
    lastSearchAt: 100,
  };
  assert.deepEqual(mergeSearchLocation(cached, location).forms, cached.forms);
  assert.deepEqual(mergeSearchLocation(cached, location).bounds, cached.bounds);
  assert.equal(
    mergeSearchLocation({ ...cached, lastSearchAt: 300 }, location)
      .lastSearchAt,
    300,
  );
});

test("Workspace persistence only accepts working data, excluding account and map credentials", () => {
  const state = {
    dataset: "NISAR",
    results: [{ id: "scene" }],
    geometry: { type: "Polygon" },
    selected: ["scene"],
    mapKey: "private-map-key",
    password: "private-password",
    token: "private-token",
    auth: { username: "private-user" },
  };
  const saved = {
    ...pickWorkspace(state, VIEW_FIELDS),
    ...pickWorkspace(state, DATA_FIELDS),
  };
  assert.equal(saved.dataset, "NISAR");
  assert.deepEqual(saved.selected, ["scene"]);
  assert.deepEqual(saved.results, state.results);
  assert.deepEqual(saved.geometry, state.geometry);
  for (const key of ["mapKey", "password", "token", "auth"])
    assert.equal(saved[key], undefined);
});

test("Large scene caches survive reload via IndexedDB while a newer selection stays in the journal", async () => {
  const records = new Map(),
    preferences = new Map();
  const previousDB = globalThis.indexedDB,
    previousStorage = globalThis.localStorage;
  globalThis.localStorage = {
    getItem: (key) => preferences.get(key) || null,
    setItem: (key, value) => preferences.set(key, value),
    removeItem: (key) => preferences.delete(key),
  };
  globalThis.indexedDB = {
    open() {
      const request = {};
      queueMicrotask(() => {
        request.result = {
          createObjectStore() {},
          transaction() {
            return {
              objectStore() {
                return {
                  put(value, key) {
                    records.set(key, structuredClone(value));
                  },
                  get(key) {
                    const result = {};
                    queueMicrotask(() => {
                      result.result = structuredClone(records.get(key));
                      result.onsuccess();
                    });
                    return result;
                  },
                };
              },
            };
          },
          close() {},
        };
        request.onupgradeneeded?.();
        request.onsuccess();
      });
      return request;
    },
  };
  try {
    const first = createWorkspaceStore((message) => assert.fail(message));
    await first.load();
    const results = Array.from({ length: 1000 }, (_, i) => ({
      id: `scene-${i}`,
      description: "x".repeat(1000),
    }));
    const snapshot = {
      dataset: "SENTINEL-1",
      results,
      bounds: [86, 43, 87, 44],
      selected: [],
      forms: { "path-filter": "114" },
    };
    first.save(snapshot);
    assert.equal(preferences.has("sar-downloader-workspace-v1-data"), false);
    first.save({ ...snapshot, selected: ["scene-42"], tab: "dem" });
    const restored = await createWorkspaceStore((message) =>
      assert.fail(message),
    ).load();
    assert.equal(restored.results.length, 1000);
    assert.deepEqual(restored.selected, ["scene-42"]);
    assert.equal(restored.forms["path-filter"], "114");
    assert.equal(restored.tab, "dem");
    assert.ok(preferences.get("sar-downloader-workspace-v1").length < 2000);
    const constrained = createWorkspaceStore(() => {});
    const current = await constrained.load();
    globalThis.localStorage.setItem = () => {
      throw new Error("quota exceeded");
    };
    constrained.save({ ...current, selected: ["scene-43"] });
    const fallback = await createWorkspaceStore(() => {}).load();
    assert.deepEqual(fallback.selected, ["scene-43"]);
  } finally {
    globalThis.indexedDB = previousDB;
    globalThis.localStorage = previousStorage;
  }
});
