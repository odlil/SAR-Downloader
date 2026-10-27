import { geometryBounds, unionBounds, validateBounds } from "./core.js";
// Native browser storage keeps large scene/geometry caches out of localStorage.
const KEY = "sar-downloader-workspace-v1";
export const VIEW_FIELDS = [
  "tab",
  "dataset",
  "bounds",
  "forms",
  "selected",
  "activeGroup",
  "activeScene",
  "searchPhase",
  "searchMessage",
  "warnings",
  "aoiMode",
  "aoiText",
  "aoiHidden",
  "advanced",
  "orbitLabel",
  "orbitStatus",
  "mapView",
  "showFootprints",
  "scroll",
  "taskFilter",
  "lastSearchBounds",
  "lastSearchAt",
  "auxiliaryTab",
  "sharedAOI",
];
export const DATA_FIELDS = [
  "geometry",
  "results",
  "orbits",
  "orbitAssets",
  "dem",
  "lastRequest",
  "searchSlots",
];
export function pickWorkspace(snapshot, fields) {
  return Object.fromEntries(
    fields
      .filter((key) => snapshot[key] !== undefined)
      .map((key) => [key, snapshot[key]]),
  );
}

// Provider/key ownership belongs to the backend, including pre-upgrade caches.
export function workspaceForms(forms = {}) {
  const { ["satellite-provider"]: ignored, ...working } = forms ?? {};
  return working;
}

function validBounds(value) {
  try {
    return validateBounds(value);
  } catch {
    return null;
  }
}
export function searchAreaKey(bounds, geometry = null) {
  return JSON.stringify([bounds, geometry]);
}
export function slotUsesArea(slot, bounds, geometry = null) {
  if (!slot) return false;
  const previous =
    slot.aoiKey ||
    (validBounds(slot.bounds)
      ? searchAreaKey(slot.bounds, slot.geometry || null)
      : null);
  return previous === searchAreaKey(bounds, geometry);
}
export function migrateSharedWorkspace(cached) {
  if (!cached) return cached;
  function orbitForms(previous) {
    const forms = { ...previous };
    if (forms["include-orbit"] === undefined) {
      const old = ["include-precise", "include-restituted"].filter(
        (key) => forms[key] !== undefined,
      );
      if (old.length)
        forms["include-orbit"] = old.some((key) => Boolean(forms[key]));
    }
    delete forms["include-precise"];
    delete forms["include-restituted"];
    return forms;
  }
  const forms = orbitForms(cached.forms);
  const searchSlots = Object.fromEntries(
    Object.entries(cached.searchSlots || {})
      .filter(([, slot]) => slot && typeof slot === "object")
      .map(([key, slot]) => [key, { ...slot, forms: orbitForms(slot.forms) }]),
  );
  cached = { ...cached, searchSlots };
  if (cached.sharedAOI) return { ...cached, forms };
  // Earlier versions could leave an imported shape in a sensor slot while the
  // active module displayed the initial rectangle. Recover that imported AOI.
  const initial =
    JSON.stringify(cached.bounds) === JSON.stringify([102.5, 29, 106, 32.5]);
  const imported =
    !cached.geometry && initial
      ? Object.values(cached.searchSlots || {}).find((slot) => {
          try {
            return (
              slot?.geometry &&
              validBounds(slot.bounds) &&
              geometryBounds(slot.geometry)
            );
          } catch {
            return false;
          }
        })
      : null;
  if (!imported) return { ...cached, forms, sharedAOI: true };
  const bounds = geometryBounds(imported.geometry);
  ["west", "south", "east", "north"].forEach(
    (id, index) => (forms[id] = String(Number(bounds[index].toFixed(5)))),
  );
  return {
    ...cached,
    forms,
    sharedAOI: true,
    bounds,
    geometry: imported.geometry,
    aoiMode: "import",
    aoiText: "已恢复另一卫星模块中导入的研究区，所有模块共用此范围。",
    aoiHidden: false,
    results: [],
    selected: [],
    activeGroup: "",
    activeScene: null,
    searchPhase: "idle",
    searchMessage: "研究区已同步，请按当前卫星重新检索。",
    warnings: [],
    lastRequest: null,
    lastSearchBounds: null,
    lastSearchAt: Date.now(),
    mapView: null,
    dem: null,
  };
}
export function successfulSearchBounds(snapshot) {
  if (validBounds(snapshot?.lastSearchBounds)) return snapshot.lastSearchBounds;
  if (snapshot?.searchPhase === "done") {
    try {
      if (snapshot.results?.length)
        return validBounds(
          unionBounds(snapshot.results.map((scene) => scene.bounds)),
        );
      if (snapshot.lastRequest?.aoi)
        return geometryBounds(snapshot.lastRequest.aoi);
    } catch {}
  }
  return null;
}
export function startupMapTarget(snapshot, reload = false) {
  const view = snapshot?.mapView;
  const validView =
    Array.isArray(view?.center) &&
    view.center.length === 2 &&
    view.center.every(Number.isFinite) &&
    Number.isFinite(view.zoom);
  if (reload && validView) return { kind: "view", view };
  const bounds =
    successfulSearchBounds(snapshot) || validBounds(snapshot?.bounds);
  if (bounds) return { kind: "bounds", bounds };
  return validView ? { kind: "view", view } : null;
}
export function mergeSearchLocation(cached, location) {
  if (
    !validBounds(location?.bounds) ||
    !validBounds(location?.aoiBounds) ||
    !Number.isFinite(location?.updatedAt)
  )
    return cached;
  if (cached?.lastSearchAt >= location.updatedAt) return cached;
  return {
    ...(cached || { bounds: location.aoiBounds }),
    lastSearchBounds: location.bounds,
    lastSearchAt: location.updatedAt,
  };
}

export function createWorkspaceStore(onError = () => {}) {
  let database,
    unavailable = false,
    lastData = [],
    generation = "",
    pending = null,
    revision = 0;
  const opening = new Promise((resolve) => {
    try {
      const request = indexedDB.open("sar-downloader", 1);
      request.onupgradeneeded = () =>
        request.result.createObjectStore("workspace");
      request.onsuccess = () => {
        database = request.result;
        database.onversionchange = () => database.close();
        resolve(database);
      };
      request.onerror = request.onblocked = () => {
        unavailable = true;
        resolve(null);
      };
    } catch {
      unavailable = true;
      resolve(null);
    }
  });
  function write(data) {
    pending = data;
    if (!database) return;
    try {
      const transaction = database.transaction("workspace", "readwrite");
      transaction.objectStore("workspace").put(data, "current");
      transaction.onerror = () =>
        onError("工作状态保存失败，请检查浏览器可用存储空间。");
      pending = null;
    } catch {
      onError("工作状态未能保存，请保留页面或导出任务配置。");
    }
  }
  opening.then(() => {
    if (pending) write(pending);
  });
  return {
    async load() {
      let journal;
      try {
        journal = JSON.parse(localStorage.getItem(KEY) || "null");
      } catch {}
      const db = await opening;
      let cached;
      if (db)
        cached = await new Promise((resolve) => {
          try {
            const request = db
              .transaction("workspace")
              .objectStore("workspace")
              .get("current");
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => resolve(null);
          } catch {
            resolve(null);
          }
        });
      // The journal is newer for form edits, selections and map movement.
      const current =
        !journal || (cached?.journal?.updatedAt || 0) > (journal.updatedAt || 0)
          ? cached?.journal
          : journal;
      if (!current || current.version !== 1) return null;
      let fallback;
      try {
        fallback = JSON.parse(localStorage.getItem(KEY + "-data") || "null");
      } catch {}
      const data =
        cached?.generation === current.generation
          ? cached.data
          : fallback?.generation === current.generation
            ? fallback.data
            : null;
      generation = current.generation;
      revision = Number(current.updatedAt) || 0;
      lastData = DATA_FIELDS.map((key) => data?.[key]);
      return { ...current.view, ...data };
    },
    save(snapshot) {
      const refs = DATA_FIELDS.map((key) => snapshot[key]);
      const changed =
        !generation || refs.some((value, index) => value !== lastData[index]);
      if (changed) {
        generation =
          Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
        lastData = refs;
      }
      revision = Math.max(Date.now(), revision + 1);
      const journal = {
        version: 1,
        generation,
        updatedAt: revision,
        view: pickWorkspace(snapshot, VIEW_FIELDS),
      };
      const data = pickWorkspace(snapshot, DATA_FIELDS);
      let journalSaved = true;
      // Small workspaces also have a synchronous fallback for an immediate F5,
      // private browsing or unavailable IndexedDB. Large results use IndexedDB.
      try {
        if (changed) {
          const json = JSON.stringify({ generation, data });
          if (json.length < 750000) localStorage.setItem(KEY + "-data", json);
          else {
            localStorage.removeItem(KEY + "-data");
            if (unavailable)
              onError("浏览器数据库不可用，大量检索结果暂时无法保存。");
          }
        }
        localStorage.setItem(KEY, JSON.stringify(journal));
      } catch {
        journalSaved = false;
        onError("浏览器偏好存储不可用，工作状态将尝试保存到本机数据库。");
      }
      if (changed || !journalSaved) write({ generation, data, journal });
    },
  };
}
