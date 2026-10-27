import { createAuxiliaryController } from "./auxiliary-controller.js";
import { createSearchController } from "./search-controller.js";
import {
  DATASETS,
  sensorForDataset,
  parseNumberFilter,
  validateBounds,
  frameValue,
  sceneDownloadUrls,
  geometryBounds,
  formatSize,
  escapeHtml as escape,
} from "./core.js";
import { createWorkbenchMap } from "./map.js";
import { createMapSplit } from "./split-pane.js";
import { createDownloadsUI } from "./downloads-ui.js";
import { api } from "./api.js";
import { createPreferences } from "./preferences.js";
import { createUpdatesUI } from "./updates-ui.js";
import { createSubsetDialog } from "./subset-dialog.js";
import { groupScenes, sceneGroupKey, nextGroupAtPoint } from "./scene-view.js";
import {
  createWorkspaceStore,
  startupMapTarget,
  successfulSearchBounds,
  mergeSearchLocation,
  searchAreaKey,
  slotUsesArea,
  migrateSharedWorkspace,
  workspaceForms,
} from "./workspace-state.js";

const $ = (id) => document.getElementById(id);

const icon = (name) =>
  `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const STORE = "sar-downloader-v1";
function loadSaved() {
  try {
    const value = JSON.parse(localStorage.getItem(STORE) || "{}");
    return value && typeof value === "object" && !Array.isArray(value)
      ? value
      : {};
  } catch {
    return {};
  }
}
const saved = loadSaved();
const state = {
  tab: "search",
  dataset: DATASETS[saved.dataset] ? saved.dataset : "SENTINEL-1",
  bounds: null,
  geometry: null,
  results: [],
  selected: new Set(),
  orbits: [],
  orbitDemo: false,
  dem: null,
  taskFilter: "all",
  tasks: Array.isArray(saved.tasks)
    ? saved.tasks
        .filter(
          (t) =>
            t &&
            typeof t.id === "string" &&
            typeof t.title === "string" &&
            ["sar", "orbit", "dem"].includes(t.kind) &&
            ["queued", "paused"].includes(t.state),
        )
        .slice(0, 1000)
    : [],
  mapKey: "",
  satelliteProvider: "tianditu",
  basemap: "osm",
  detailScene: null,
};
state.searchSlots = {};
state.auxiliaryTab = "dem";
state.activeGroup = "";
state.activeScene = null;
state.searchPhase = "idle";
state.searchMessage = "设置研究区和日期后，点击检索影像。";
state.warnings = [];
state.lastSearchBounds = null;
state.lastSearchAt = 0;
let workbenchMap, mapSplit, downloadsUI, subsetDialog, toastTimeout;
let workspaceReady = false,
  saveTimer;
const workspace = createWorkspaceStore((message) => setStatus(message));
const FORM_IDS = [
  "dataset",
  "product",
  "direction",
  "polarization",
  "west",
  "south",
  "east",
  "north",
  "start-date",
  "end-date",
  "path-filter",
  "frame-filter",
  "frame-scheme",
  "burst-id",
  "result-limit",
  "dem-extent",
  "dem-source",
  "dem-buffer",
  "dem-height",
  "dem-format",
  "orbit-strategy",
  "include-burst-xml",
  "nisar-mode",
  "nisar-include-dem",
  "alos-include-dem",
  "include-orbit",
  "include-dem",
  "companion-dem-extent",
  "companion-dem-buffer",
  "companion-dem-format",
  "companion-dem-height",
  "subset-frequency",
  "subset-polarization",
  "subset-connections",
  "subset-min-height",
  "subset-max-height",
  "task-kind",
];
function captureWorkspace() {
  const center = workbenchMap?.map.getCenter();
  return {
    sharedAOI: true,
    ...Object.fromEntries(
      [
        "tab",
        "dataset",
        "bounds",
        "geometry",
        "results",
        "orbits",
        "orbitAssets",
        "dem",
        "lastRequest",
        "activeGroup",
        "activeScene",
        "searchPhase",
        "searchMessage",
        "warnings",
        "taskFilter",
        "lastSearchBounds",
        "lastSearchAt",
        "searchSlots",
        "auxiliaryTab",
      ].map((key) => [key, state[key]]),
    ),
    selected: [...state.selected],
    forms: Object.fromEntries(
      FORM_IDS.map((id) => [
        id,
        $(id).type === "checkbox" ? $(id).checked : $(id).value,
      ]),
    ),
    aoiMode:
      document.querySelector("[data-aoi-mode].active")?.dataset.aoiMode ||
      "coordinates",
    aoiText: $("aoi-instructions").textContent,
    aoiHidden: $("aoi-instructions").hidden,
    advanced: $("advanced-filters").open,
    orbitLabel: $("orbit-folder-label").textContent,
    orbitStatus: $("orbit-match-status").textContent,
    mapView: center
      ? { center: [center.lat, center.lng], zoom: workbenchMap.map.getZoom() }
      : null,
    showFootprints: $("show-footprints").checked,
    scroll: {
      sidebar: document.querySelector(".sidebar").scrollTop,
      results: document.querySelector(".result-table-wrapper").scrollTop,
    },
  };
}
function schedulePersist() {
  if (workspaceReady) {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(persist, 120);
  }
}

const {
  renderOrbits,
  chooseOrbitFolder,
  matchOrbitFiles,
  invalidateDEM,
  renderDEM,
  previewDEM,
  syncDEMControls,
} = createAuxiliaryController({
  state,
  getMap: () => workbenchMap,
  escape,
  icon,
  toast,
  schedulePersist,
  persist,
  selectedScenes,
});
const { search, cancelSearch, clearSearch } = createSearchController({
  state,
  getMap: () => workbenchMap,
  readFilters,
  updateBounds,
  setAOIMode,
  clearActiveScene,
  invalidateDEM,
  renderResults,
  persist,
  setError,
  setStatus,
});

function toast(message, error = false) {
  $("toast").textContent = message;
  $("toast").classList.toggle("is-error", error);
  $("toast").hidden = false;
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => ($("toast").hidden = true), 4500);
  setStatus(message);
}
function setStatus(message) {
  $("status-message").textContent = message;
}
function persist() {
  if (!workspaceReady) return;
  clearTimeout(saveTimer);
  try {
    localStorage.setItem(
      STORE,
      JSON.stringify({
        dataset: state.dataset,
        tasks: state.tasks,
        settings: {
          concurrent: $("concurrent").value,
          output: $("output-folder").value,
        },
      }),
    );
  } catch {
    setStatus("浏览器存储不可用，当前操作仍可继续；建议导出任务配置。");
  }
  workspace.save(captureWorkspace());
}
function selectedScenes() {
  return state.results.filter((scene) => state.selected.has(scene.id));
}
function visibleScenes() {
  return state.activeGroup
    ? state.results.filter(
        (scene) => sceneGroupKey(scene) === state.activeGroup,
      )
    : state.results;
}
function clearActiveScene() {
  state.activeGroup = "";
  state.activeScene = null;
}
function focusGroup(key, id = null) {
  const group = groupScenes(state.results).find((group) => group.key === key);
  state.activeGroup = group?.key || "";
  state.activeScene =
    group?.scenes.find((scene) => scene.id === id)?.id ||
    group?.scenes[0]?.id ||
    null;
  renderResults();
  $("result-body").closest(".result-table-wrapper").scrollTop = 0;
  setStatus(
    group
      ? `${group.label} · ${group.scenes.length} 景；可在地图或覆盖组菜单切换。`
      : state.searchMessage,
  );
}
function focusScene(id) {
  const scene = state.results.find((scene) => scene.id === id);
  if (!scene) return;
  if (state.activeGroup !== sceneGroupKey(scene))
    return focusGroup(sceneGroupKey(scene), id);
  state.activeScene = id;
  updateSelection();
}
function focusMapScene(id, ids) {
  const group = nextGroupAtPoint(state.results, ids, state.activeGroup, id);
  if (group)
    focusGroup(
      group.key,
      group.scenes.some((scene) => scene.id === id) ? id : null,
    );
}
function moveFrame(step) {
  const groups = groupScenes(state.results);
  if (!groups.length) return;
  const current = groups.findIndex((group) => group.key === state.activeGroup);
  focusGroup(
    groups[
      current < 0
        ? step > 0
          ? 0
          : groups.length - 1
        : (current + step + groups.length) % groups.length
    ].key,
  );
}
function renderFrameNavigation() {
  const groups = groupScenes(state.results),
    group = groups.find((group) => group.key === state.activeGroup);
  $("frame-navigator").hidden = !groups.length || state.tab !== "search";
  $("fit-results").disabled = !state.results.length;
  $("map-frame").replaceChildren(
    new Option(
      `全部覆盖组 · ${groups.length} 组 / ${state.results.length} 景`,
      "",
    ),
    ...groups.map(
      (group) =>
        new Option(`${group.label} · ${group.scenes.length} 景`, group.key),
    ),
  );
  $("map-frame").value = state.activeGroup;
  $("previous-frame").disabled = !groups.length;
  $("next-frame").disabled = !groups.length;
  $("active-frame-summary").hidden = !group;
  $("active-frame-caption").textContent = group
    ? `${group.label} · 当前 ${group.scenes.length} / 共 ${state.results.length} 景`
    : "";
}
function setError(message = "") {
  $("filter-error").textContent = message;
  $("filter-error").hidden = !message;
}

function setDatasetOptions(value) {
  const nisar = value === "NISAR";
  $("dataset").replaceChildren(
    ...(nisar
      ? [["NISAR", "NISAR"]]
      : sensorForDataset(value) === "alos" ? ["ALOS", "ALOS-2"].map((dataset) => [dataset, DATASETS[dataset].label]) : [
          ["SENTINEL-1", "Sentinel-1"],
          ["SLC-BURST", "S1 Bursts"],
        ]
    ).map(([value, label]) => new Option(label, value)),
  );
  $("dataset").value = value;
}
function syncDownloadControls() {
  const nisar = state.dataset === "NISAR",
    alos = sensorForDataset(state.dataset) === "alos",
    subset = nisar && $("nisar-mode").value === "subset";
  $("search-module-title").textContent = nisar
    ? "NISAR 下载"
    : alos ? "ALOS / ALOS-2 下载" : "Sentinel-1 下载";
  $("dataset-field").hidden = nisar;
  $("nisar-mode-field").hidden = !nisar;
  $("sentinel-options").hidden = nisar || alos;
  $("nisar-options").hidden = !nisar;
  $("alos-options").hidden = !alos;
  $("product").disabled = subset;
  if (subset) $("product").value = "RSLC";
  $("companion-dem-height").disabled =
    $("companion-dem-format").value === "tiles";
  if ($("companion-dem-height").disabled)
    $("companion-dem-height").value = "egm2008";
  $("queue-scenes").textContent = subset ? "设置范围并下载" : "加入下载队列";
  $("panel-search").setAttribute(
    "aria-labelledby",
    `tab-${sensorForDataset(state.dataset)}`,
  );
}
const SEARCH_KEYS = [
  "dataset",
  "results",
  "activeGroup",
  "activeScene",
  "searchPhase",
  "searchMessage",
  "warnings",
  "lastRequest",
  "lastSearchBounds",
  "lastSearchAt",
];
const SEARCH_FORM_IDS = FORM_IDS.filter(
  (id) =>
    !id.startsWith("dem-") &&
    ![
      "west",
      "south",
      "east",
      "north",
      "orbit-strategy",
      "satellite-provider",
      "task-kind",
    ].includes(id),
);
function captureSearchSlot() {
  return {
    aoiKey: searchAreaKey(state.bounds, state.geometry),
    ...Object.fromEntries(SEARCH_KEYS.map((key) => [key, state[key]])),
    selected: [...state.selected],
    forms: Object.fromEntries(
      SEARCH_FORM_IDS.map((id) => [
        id,
        $(id).type === "checkbox" ? $(id).checked : $(id).value,
      ]),
    ),
  };
}
function applyPendingBounds(fit = false) {
  if (!fit && !state.bounds && ["west", "south", "east", "north"].every((id) => !$(id).value.trim())) return;
  const bounds = readBounds();
  if (
    !state.bounds || bounds.some(
      (value, index) => Math.abs(value - state.bounds[index]) > 0.00001,
    )
  ) {
    updateBounds(bounds, null, fit);
    setAOIMode("coordinates");
    clearSearch("研究区已更新，请重新检索影像。");
  } else if (fit) workbenchMap?.fitBounds();
}
function switchSearchSensor(sensor) {
  const current = sensorForDataset(state.dataset);
  if (current === sensor) return;
  cancelSearch();
  state.searchSlots[current] = captureSearchSlot();
  const slot = state.searchSlots[sensor],
    dataset = slot?.dataset || (sensor === "nisar" ? "NISAR" : sensor === "alos" ? "ALOS" : "SENTINEL-1");
  setDatasetOptions(dataset);
  configureDataset();
  clearSearch("设置研究区和日期后，点击检索影像。");
  if (slot) {
    for (const id of SEARCH_FORM_IDS) {
      const element = $(id),
        value = slot.forms?.[id];
      if (value === undefined || id === "dataset") continue;
      if (element.type === "checkbox") element.checked = Boolean(value);
      else if (
        element.tagName !== "SELECT" ||
        [...element.options].some((option) => option.value === value)
      )
        element.value = value;
    }
    if (slotUsesArea(slot, state.bounds, state.geometry)) {
      for (const key of SEARCH_KEYS)
        if (slot[key] !== undefined) state[key] = slot[key];
      state.selected = new Set(slot.selected || []);
      if (state.searchPhase === "loading") {
        state.searchPhase = "idle";
        state.searchMessage = "上次检索已中断，请重新检索。";
      }
    } else {
      state.lastRequest = null;
      state.lastSearchBounds = null;
      state.lastSearchAt = Date.now();
      state.searchMessage = "研究区已同步，请按当前卫星重新检索。";
    }
  } else {
    state.lastRequest = null;
    state.lastSearchBounds = null;
    state.lastSearchAt = Date.now();
  }
  syncDownloadControls();
  renderResults();
  if (state.results.length) workbenchMap?.fitScenes();
  else workbenchMap?.fitBounds();
}

function configureDataset(keepDates = false) {
  state.dataset = $("dataset").value;
  const config = DATASETS[state.dataset];
  $("product").replaceChildren(
    ...config.products.map((value) => new Option(value, value)),
  );
  $("frame-scheme").replaceChildren(
    ...config.frameSchemes.map((item) => new Option(item.label, item.value)),
  );
  $("path-label").textContent = config.pathLabel;
  $("dataset-hint").textContent = config.hint;
  $("path-filter").placeholder =
    state.dataset === "NISAR"
      ? "例如 165 或 164-166"
      : sensorForDataset(state.dataset) === "alos"
        ? state.dataset === "ALOS-2" ? "例如 86 或 85-87（1–207）" : "例如 478 或 477-480"
        : "例如 135 或 130-140";
  $("frame-filter").placeholder =
    state.dataset === "NISAR"
      ? "例如 100 或 100-102"
      : sensorForDataset(state.dataset) === "alos"
        ? "例如 590 或 590-592"
        : "例如 486 或 485-488";
  const burst = state.dataset === "SLC-BURST";
  const polarizations = burst
    ? ["VV", "VH", "HH", "HV"]
    : state.dataset === "NISAR"
      ? ["HH+HV", "VV+VH", "HH", "VV", "HH+VV", "HH+HV+VH+VV"]
      : sensorForDataset(state.dataset) === "alos"
        ? state.dataset === "ALOS-2" ? ["HH", "HH+HV", "VV", "VV+VH"] : ["HH", "HH+HV", "QUADRATURE"]
        : ["VV+VH", "HH+HV", "VV", "HH"];
  $("polarization").replaceChildren(
    new Option("全部", ""),
    ...polarizations.map((value) => new Option(value, value)),
  );
  $("burst-filters").hidden = !burst;
  $("burst-copy-options").hidden = !burst;
  $("frame-filter").disabled = burst;
  $("frame-scheme").disabled = burst;
  if (burst) $("frame-filter").placeholder = "Bursts 使用 Full Burst ID";
  if (!keepDates) {
    const today = new Date(),
      start = new Date(today);
    start.setUTCDate(start.getUTCDate() - 90);
    $("start-date").value =
      state.dataset === "ALOS"
        ? "2006-01-01"
        : state.dataset === "ALOS-2" ? "2014-08-01" : start.toISOString().slice(0, 10);
    $("end-date").value =
      state.dataset === "ALOS"
        ? "2011-04-30"
        : today.toISOString().slice(0, 10);
  }
  $("path-filter").value = "";
  $("frame-filter").value = "";
  $("burst-id").value = "";
  $("direction").value = "";
  state.selected.clear();
  syncDownloadControls();
}

function readBounds() {
  if (["west", "south", "east", "north"].every((id) => !$(id).value.trim()))
    throw new Error("请先设置研究区：填写经纬度、绘制矩形或导入文件。");
  return validateBounds(
    ["west", "south", "east", "north"].map((id) =>
      $(id).value.trim() === "" ? NaN : Number($(id).value),
    ),
  );
}
function readFilters() {
  parseNumberFilter($("path-filter").value, { min: 1 });
  parseNumberFilter($("frame-filter").value);
  return {
    dataset: state.dataset,
    product: $("product").value,
    direction: $("direction").value,
    polarization: $("polarization").value,
    start: $("start-date").value,
    end: $("end-date").value,
    path: $("path-filter").value.trim(),
    frame: $("frame-filter").value.trim(),
    frameScheme: $("frame-scheme").value,
    burstID: $("burst-id").value.trim(),
    limit: Number($("result-limit").value),
    bounds: readBounds(),
  };
}

function updateBounds(bounds, geometry = null, fit = true) {
  if (bounds === null) {
    state.bounds = null;
    state.geometry = null;
    state.lastSearchBounds = null;
    state.lastSearchAt = Date.now();
    ["west", "south", "east", "north"].forEach((id) => ($(id).value = ""));
    workbenchMap?.setBounds(null, null, fit);
    $("aoi-caption").textContent = "尚未设置研究区";
    $("fit-aoi").disabled = true;
    invalidateDEM();
    return;
  }
  validateBounds(bounds);
  if (
    searchAreaKey(bounds, geometry) !==
    searchAreaKey(state.bounds, state.geometry)
  ) {
    state.lastSearchBounds = null;
    state.lastSearchAt = Date.now();
  }
  state.bounds = bounds;
  state.geometry = geometry;
  ["west", "south", "east", "north"].forEach(
    (id, index) => ($(id).value = Number(bounds[index].toFixed(5))),
  );
  workbenchMap?.setBounds(bounds, geometry, fit);
  $("fit-aoi").disabled = false;
  $("aoi-caption").textContent =
    `${bounds[0].toFixed(2)}–${bounds[2].toFixed(2)}°E / ${bounds[1].toFixed(2)}–${bounds[3].toFixed(2)}°N`;
  if (bounds[0] < 0 || bounds[1] < 0)
    $("aoi-caption").textContent =
      `范围 ${bounds.map((n) => n.toFixed(2)).join(", ")} · WGS84`;
  invalidateDEM();
}

function renderResults() {
  const config = DATASETS[state.dataset],
    scheme = state.dataset === "NISAR" ? "nisar" : "asf",
    burst = state.dataset === "SLC-BURST";
  $("result-count").textContent = state.results.length;
  $("path-column").textContent = config.columnPath;
  $("frame-column").innerHTML =
    `Frame <span class="tiny-note">${scheme === "nisar" ? "NISAR" : sensorForDataset(state.dataset) === "alos" ? "JAXA / ASF" : "ASF"}</span>`;
  $("result-description").textContent =
    `${config.label} · ${$("product").value}`;
  $("result-source").textContent = {
    idle: "未检索",
    loading: "查询中",
    done: "ASF",
    error: "查询失败",
  }[state.searchPhase];
  $("burst-column").hidden = !burst;
  $("search-notice").textContent = state.warnings.join(" ");
  $("search-notice").hidden = !state.warnings.length;
  const visible = visibleScenes();
  renderFrameNavigation();
  const numberedCell = (value, field) =>
    Number.isFinite(value)
      ? `<button class="cell-link" data-${field}="${value}" title="按此编号筛选">${value}</button>`
      : "—";
  $("result-body").innerHTML = visible
    .map(
      (scene) =>
        `<tr data-scene-row="${escape(scene.id)}" class="${state.selected.has(scene.id) ? "is-selected" : ""}"><td class="checkbox-cell"><input type="checkbox" data-select-scene="${escape(scene.id)}" ${state.selected.has(scene.id) ? "checked" : ""} aria-label="选择 ${escape(scene.platform)} ${escape(scene.date)} ${escape(scene.fullBurstID || scene.time)} ${escape(scene.polarization)}"></td><td><div class="scene-name"><span class="platform-tag">${escape(scene.platform)}</span><span class="scene-text"><button class="scene-main scene-focus" data-focus-scene="${escape(scene.id)}" aria-label="定位影像 ${escape(scene.name)}">${escape(scene.date)} <span class="tiny-note">${escape(scene.time)}</span></button><span class="scene-sub" title="${escape(scene.name)}">${escape(scene.platform)} · ${escape(scene.beamMode || "SAR")} · ${escape(scene.product)}</span></span></div></td><td>${escape(scene.product)}</td><td>${numberedCell(scene.path, "path")}</td><td>${Number.isFinite(frameValue(scene, scheme)) ? `<button class="cell-link" data-focus-scene="${escape(scene.id)}" title="查看此 Path / Frame 的影像">${frameValue(scene, scheme)}</button>` : "—"}</td><td ${burst ? "" : "hidden"}><button class="cell-link" data-focus-scene="${escape(scene.id)}" title="查看相同 Full Burst ID 的影像">${escape(scene.fullBurstID || "—")}</button></td><td>${escape(scene.polarization)}</td><td><span class="direction">${scene.direction === "ASCENDING" ? "↑ 升轨" : scene.direction === "DESCENDING" ? "↓ 降轨" : "—"}</span></td><td class="numeric">${formatSize(scene.size)}</td><td><button class="detail-button" data-detail="${escape(scene.id)}" aria-label="查看 ${escape(scene.id)} 详情">详情 ↗</button></td></tr>`,
    )
    .join("");
  $("result-empty").hidden = state.results.length > 0;
  $("result-empty-title").textContent = {
    idle: "准备检索影像",
    loading: "正在查询 ASF…",
    done: "没有符合条件的影像",
    error: "本次检索未完成",
  }[state.searchPhase];
  $("result-empty-help").textContent = {
    idle: "设置范围、日期和数据集，然后点击检索影像。",
    loading: "正在读取真实目录和影像覆盖范围，请稍候。",
    done: "试着扩大日期范围，或检查 Path / Frame 与研究区。",
    error: "请查看左侧错误提示，调整条件或稍后重试。",
  }[state.searchPhase];
  $("empty-reset").hidden = ["idle", "loading"].includes(state.searchPhase);
  updateSelection();
}

function updateSelection() {
  const scenes = selectedScenes();
  $("selected-count").textContent = scenes.length;
  const knownSize = scenes.reduce((sum, scene) => sum + (scene.size || 0), 0);
  $("selected-size").textContent = scenes.length
    ? knownSize
      ? formatSize(knownSize) +
        (scenes.some((scene) => !Number.isFinite(scene.size)) ? " + 未知" : "")
      : "大小未知"
    : "0 GB";
  $("clear-selection").disabled = !scenes.length;
  $("queue-scenes").disabled = !scenes.length;
  $("copy-links").disabled = !scenes.some((scene) => scene.url);
  $("copy-links").title =
    "复制所选记录的 ASF 官方下载链接；下载需配置 Earthdata 登录。";
  const visible = visibleScenes(),
    selectedInResults = visible.filter((scene) =>
      state.selected.has(scene.id),
    ).length;
  $("select-all").checked =
    visible.length > 0 && selectedInResults === visible.length;
  $("select-all").indeterminate =
    selectedInResults > 0 && selectedInResults < visible.length;
  $("select-all").setAttribute(
    "aria-label",
    state.activeGroup ? "选择当前覆盖组的检索结果" : "选择全部检索结果",
  );
  $("select-all").disabled = !state.results.length;
  document.querySelectorAll("[data-scene-row]").forEach((row) => {
    const active = state.selected.has(row.dataset.sceneRow);
    row.classList.toggle("is-selected", active);
    row.classList.toggle(
      "is-active",
      row.dataset.sceneRow === state.activeScene,
    );
    row.setAttribute(
      "aria-current",
      String(row.dataset.sceneRow === state.activeScene),
    );
    row.querySelector("input").checked = active;
  });
  workbenchMap?.setScenes(state.results, state.selected);
  workbenchMap?.setActiveScenes(
    state.activeGroup ? visible.map((scene) => scene.id) : [],
    state.activeScene,
  );
  schedulePersist();
}

function toggleSelection(id) {
  if (state.selected.has(id)) state.selected.delete(id);
  else state.selected.add(id);
  invalidateDEM();
  updateSelection();
}

function showTab(tab) {
  if (state.tab === "search" && tab !== "search") {
    try {
      applyPendingBounds();
    } catch (error) {
      toast(error.message, true);
      return;
    }
  }
  if (["sentinel", "nisar", "alos"].includes(tab)) {
    switchSearchSensor(tab);
    tab = "search";
  }
  if (tab === "auxiliary") tab = state.auxiliaryTab;
  if (!["search", "orbit", "dem", "downloads"].includes(tab)) return;
  state.tab = tab;
  if (["dem", "orbit"].includes(tab)) state.auxiliaryTab = tab;
  const primary =
    tab === "search"
      ? sensorForDataset(state.dataset)
      : tab === "downloads"
        ? "downloads"
        : "auxiliary";
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === primary;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active);
    button.tabIndex = active ? 0 : -1;
  });
  $("auxiliary-navigation").hidden = primary !== "auxiliary";
  document.querySelectorAll("[data-auxiliary]").forEach((button) => {
    const active = button.dataset.auxiliary === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active);
  });
  document
    .querySelectorAll("[data-sidebar]")
    .forEach(
      (panel) => (panel.hidden = panel.getAttribute("data-sidebar") !== tab),
    );
  for (const name of ["search", "orbit", "dem", "downloads"])
    $(`panel-${name}`).hidden = name !== tab;
  const showMap = ["search", "dem"].includes(tab);
  $("map-stage").hidden = !showMap;
  $("main-stage").classList.toggle("no-map", !showMap);
  mapSplit?.setVisible(showMap);
  workbenchMap?.setTiles(tab === "dem" && state.dem ? state.dem.tiles : []);
  if (tab === "downloads") renderTasks();
  syncDownloadControls();
  renderFrameNavigation();
  setStatus(
    {
      search: state.searchMessage,
      orbit: "辅助数据 · Sentinel-1 精密 / 恢复轨道",
      dem: "辅助数据 · DEM 单独下载",
      downloads: "下载任务 · 整景 / 裁剪 / 辅助数据统一管理",
    }[tab],
  );
  schedulePersist();
}

function resetFilters() {
  state.dataset = sensorForDataset(state.dataset) === "sentinel" ? "SENTINEL-1" : state.dataset;
  setDatasetOptions(state.dataset);
  configureDataset();
  updateBounds(null, null, true);
  $("aoi-instructions").hidden = true;
  document.querySelectorAll("[data-aoi-mode]").forEach((button) => {
    const active = button.dataset.aoiMode === "coordinates";
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active);
  });
  workbenchMap?.cancelDraw();
  clearSearch();
}

function openDialog(id) {
  const dialog = $(id);
  if (!dialog.open) dialog.showModal();
}
function closeDialog(id) {
  $(id).close();
}

function showDetails(id) {
  const scene = state.results.find((item) => item.id === id);
  if (!scene) return;
  state.detailScene = scene;
  const scheme = scene.dataset === "NISAR" ? "nisar" : "asf",
    burst = scene.dataset === "SLC-BURST";
  const detail = (label, value) =>
    `<div><dt>${escape(label)}</dt><dd>${escape(value ?? "—")}</dd></div>`;
  const link = (url, label) =>
    `<a class="text-link" href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(label)} ↗</a>`;
  $("scene-content").innerHTML =
    `<div class="scene-detail-hero"><span class="eyebrow">${escape(DATASETS[scene.dataset].label)} / ASF</span><h3>${escape(scene.name)}</h3></div><dl class="detail-grid">${detail("获取开始 · UTC", scene.startTime)}${detail("获取结束 · UTC", scene.stopTime)}${detail("产品类型", scene.product)}${detail(DATASETS[scene.dataset].columnPath, scene.path)}${detail(`Frame · ${scheme.toUpperCase()}`, frameValue(scene, scheme))}${detail("极化", scene.polarization)}${detail("轨道方向", scene.direction === "ASCENDING" ? "升轨" : scene.direction === "DESCENDING" ? "降轨" : "—")}${detail("主文件大小", formatSize(scene.size))}${burst ? detail("Full Burst ID", scene.fullBurstID) + detail("子条带", scene.subswath) : ""}${detail("文件名", scene.filename)}</dl><button class="button full-width" id="same-path-frame">检索相同 ${burst ? "Burst ID" : "Path / Frame"}</button><div class="asset-links">${scene.url ? link(scene.url, burst ? "打开 Burst TIFF 下载链接" : "打开影像下载链接") : "此记录未提供主文件下载链接"}${(scene.xmlUrls || []).map((url) => link(url, "XML 元数据")).join("")}</div><div class="detail-note">下载需登录 Earthdata。</div><button class="button button-primary full-width" id="detail-queue">加入下载队列</button>`;
  $("same-path-frame").addEventListener("click", () => {
    $("path-filter").value = scene.path ?? "";
    if (burst) $("burst-id").value = scene.fullBurstID || "";
    else {
      $("frame-scheme").value = scheme;
      $("frame-filter").value = frameValue(scene, scheme) ?? "";
    }
    closeDialog("scene-dialog");
    showTab("search");
    search();
  });
  $("detail-queue").textContent =
    state.dataset === "NISAR" && $("nisar-mode").value === "subset"
      ? "设置范围并下载"
      : "加入下载队列";
  $("detail-queue").addEventListener("click", () => {
    closeDialog("scene-dialog");
    prepareSceneTasks([scene]);
  });
  openDialog("scene-dialog");
}

let addingScenes = false;
function prepareSceneTasks(scenes) {
  if (addingScenes || !scenes.length) return;
  if (state.dataset === "NISAR" && $("nisar-mode").value === "subset") {
    try { subsetDialog.open(scenes); }
    catch (error) { toast(error.message, true); }
  } else addSceneTasks(scenes);
}
async function addSceneTasks(scenes, subsetOptions, directory) {
  if (addingScenes || !scenes.length) return;
  addingScenes = true;
  $("queue-scenes").disabled = true;
  try {
    const subset =
      state.dataset === "NISAR" && $("nisar-mode").value === "subset";
    let path, payload;
    if (subset) {
      if (!subsetOptions) throw new Error("请先设置下载范围。");
      if (scenes.some((scene) => scene.product !== "RSLC"))
        throw new Error("按范围裁剪只支持 RSLC，请切换产品后重新检索。");
      path = "/api/nisar/subsets/add";
      payload = { scenes, ...subsetOptions };
    } else if (state.dataset === "NISAR") {
      path = "/api/downloads/scenes";
      payload = { scenes, includeDEM: $("nisar-include-dem").checked };
    } else if (sensorForDataset(state.dataset) === "alos") {
      const health = await api("/api/health");
      if (!health.capabilities?.alos) throw new Error("请重新启动新版 SAR Downloader 后再加入 ALOS 任务。");
      path = "/api/downloads/scene-bundle";
      payload = { scenes, bounds: readBounds(), auxiliary: {
        orbit: false, dem: $("alos-include-dem").checked,
        demOptions: { extent: "scenes", buffer: 5, format: "GTiff", height: "ellipsoid" },
      } };
    } else {
      path = "/api/downloads/scene-bundle";
      payload = {
        scenes,
        includeXML: $("include-burst-xml").checked,
        bounds: readBounds(),
        auxiliary: {
          orbit: $("include-orbit").checked,
          dem: $("include-dem").checked,
          demOptions: {
            extent: $("companion-dem-extent").value,
            buffer: Number($("companion-dem-buffer").value),
            format: $("companion-dem-format").value,
            height: $("companion-dem-height").value,
          },
        },
      };
      if (payload.auxiliary.orbit) {
        const health = await api("/api/health");
        if (!health?.capabilities?.orbitAuto)
          throw new Error(
            "轨道策略已更新，请先关闭旧启动窗口并重新运行“启动SAR Downloader.cmd”，再加入任务。",
          );
      }
    }
    if (state.dataset === "NISAR") {
      const health = await api("/api/health");
      if (
        !health.capabilities?.nisarDEM ||
        !health.capabilities?.simpleClippedName ||
        (subset && (!health.capabilities?.subsetCompanionIDs || !health.capabilities?.rslcDirectorySummary))
      )
        throw new Error(
          "请先关闭旧版启动窗口并运行新版 SAR Downloader，再添加 NISAR 任务。",
        );
    }
    if (subset) {
      const configured = await api("/api/downloads/settings", { ...downloadsUI.getSettings(), output: directory });
      downloadsUI.apply(configured);
      $("output-folder").value = configured.settings.output;
      schedulePersist();
    }
    setStatus(
      subset ? "正在创建 RSLC 裁剪任务…" : "正在核对影像及所选配套数据…",
    );
    const result = await api(path, payload);
    downloadsUI.apply(result);
    const warnings = result.warnings || [];
    $("search-notice").textContent = warnings.join(" ");
    $("search-notice").hidden = !warnings.length;
    if (subset) {
      // Only start this selection, including its companion DEM; leave other queued work alone.
      const ids = result.ids;
      showTab("downloads");
      try {
        if (!Array.isArray(ids) || !ids.length) throw new Error("未返回本次下载任务编号，请在下载任务中手动开始。");
        downloadsUI.apply(await api("/api/downloads/action", { action: "resume", ids }));
        toast(`已提交 ${ids.length} 个下载任务。${warnings.join(" ")}`, Boolean(warnings.length));
      } catch (error) {
        toast(`任务已加入队列，尚未全部启动：${error.message}`, true);
      }
      return true;
    }
    toast(
      `已加入 ${result.added} 个任务，请在下载任务中开始。${warnings.length ? "部分配套数据缺失，详见结果提示。" : ""}`,
      Boolean(warnings.length),
    );
    return true;
  } catch (error) {
    if (subsetOptions) throw error;
    toast(
      error.name === "AbortError"
        ? "配套数据目录查询超时，请重试。"
        : error.message,
      true,
    );
    return false;
  } finally {
    addingScenes = false;
    updateSelection();
  }
}

function renderTasks() {
  downloadsUI?.render();
}

function downloadFile(name, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function exportCSV() {
  if (!state.results.length) {
    toast("当前没有可导出的结果。");
    return;
  }
  const scheme = state.dataset === "NISAR" ? "nisar" : "asf";
  const csvCell = (value) => {
    let text = String(value ?? "");
    if (/^[=+\-@]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  const rows = [
    [
      "scene_id",
      "dataset",
      "start_utc",
      "product",
      "path_track",
      "frame",
      "frame_scheme",
      "burst_id",
      "polarization",
      "direction",
      "size_bytes",
      "download_url",
      "xml_urls",
    ],
    ...state.results.map((scene) => [
      scene.id,
      scene.dataset,
      scene.startTime,
      scene.product,
      scene.path,
      frameValue(scene, scheme),
      scheme,
      scene.fullBurstID,
      scene.polarization,
      scene.direction,
      scene.size,
      scene.url,
      (scene.xmlUrls || []).join(" "),
    ]),
  ];
  downloadFile(
    "sar-search-results.csv",
    "\ufeff" + rows.map((row) => row.map(csvCell).join(",")).join("\r\n"),
    "text/csv;charset=utf-8",
  );
  toast(`已导出 ${state.results.length} 条真实记录和下载链接。`);
}

let importingAOI = false;
async function importAOI() {
  if (importingAOI) return;
  importingAOI = true;
  const buttons = [
    ...document.querySelectorAll('[data-aoi-mode="import"], #map-import'),
  ];
  buttons.forEach((button) => (button.disabled = true));
  try {
    setStatus("请选择 .shp、.kml 或 .kmz 文件…");
    const result = await api("/api/aoi/open", {});
    if (result.cancelled) {
      setStatus("已取消文件选择，保留原研究区。");
      return;
    }
    const geojson = result.geometry;
    updateBounds(geometryBounds(geojson), geojson, true);
    $("aoi-instructions").textContent =
      `${result.filename} · ${result.featureCount} 个面`;
    $("aoi-instructions").hidden = false;
    clearSearch("研究区已导入，请设置日期后检索影像。");
    toast(`研究区已导入：${result.filename}`);
  } catch (error) {
    toast(
      error.message === "Failed to fetch"
        ? "本地服务未连接，请通过“启动界面.cmd”重新打开。"
        : error.message || "范围文件解析失败。",
      true,
    );
  } finally {
    importingAOI = false;
    buttons.forEach((button) => (button.disabled = false));
  }
}

function setAOIMode(mode) {
  document.querySelectorAll("[data-aoi-mode]").forEach((button) => {
    const active = button.dataset.aoiMode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active);
  });
  if (mode === "draw") {
    workbenchMap?.startDraw();
    $("aoi-instructions").hidden = false;
    $("aoi-instructions").textContent =
      "拖动绘制矩形 · Esc 取消";
  } else {
    workbenchMap?.cancelDraw();
    $("aoi-instructions").hidden = mode === "coordinates";
    if (mode === "import") {
      $("aoi-instructions").textContent =
        "支持 SHP、KML、KMZ、GeoJSON";
      importAOI();
    }
  }
}

function initialize(cached, storedBasemap) {
  createUpdatesUI({ openDialog });
  const preferences = createPreferences({
    state,
    initial: storedBasemap,
    getMap: () => workbenchMap,
    toast,
    openDialog,
    closeDialog,
  });
  cached = migrateSharedWorkspace(cached);
  if (cached && DATASETS[cached.dataset]) state.dataset = cached.dataset;
  setDatasetOptions(state.dataset);
  configureDataset();
  for (const [id, key] of [["concurrent", "concurrent"]]) {
    const value = String(saved.settings?.[key] ?? "");
    if ([...$(id).options].some((option) => option.value === value))
      $(id).value = value;
  }
  if (typeof saved.settings?.output === "string")
    $("output-folder").value = saved.settings.output;
  try {
    workbenchMap = createWorkbenchMap({
      onBounds: (bounds) => {
        try {
          updateBounds(bounds, null, false);
          clearSearch("研究区已更新，请检索影像。");
        } catch (error) {
          toast(error.message, true);
        }
      },
      onScene: focusMapScene,
      onDrawEnd: () => {},
      onStatus: setStatus,
      onSatelliteRequest: preferences.requestSatellite,
    });
    updateBounds(state.bounds, null, false);
    workbenchMap.setBasemap(
      state.basemap,
      state.mapKey,
      state.satelliteProvider,
    );
  } catch (error) {
    toast(error.message, true);
  }
  mapSplit = createMapSplit($("main-stage"), $("map-splitter"), {
    onResize: () => workbenchMap?.resize(),
  });
  if (cached) restoreWorkspace(cached);
  downloadsUI = createDownloadsUI({
    toast,
    openAccount: () => openDialog("account-dialog"),
    getPlans: () => state.tasks,
    setPlans: (tasks) => {
      state.tasks = tasks;
      persist();
    },
    initialFilter: state.taskFilter,
    onFilter: (value) => {
      state.taskFilter = value;
      schedulePersist();
    },
  });
  subsetDialog = createSubsetDialog({
    getBounds: readBounds,
    getSettings: () => downloadsUI.getSettings(),
    getBasemap: () => ({ kind: state.basemap, key: state.mapKey, provider: state.satelliteProvider }),
    onConfirm: addSceneTasks,
    toast,
  });
  renderResults();
  renderOrbits();
  renderDEM();
  showTab(state.tab);
  renderTasks();
  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    search();
  });
  $("dataset").addEventListener("change", () => {
    const previous = state.dataset;
    configureDataset(sensorForDataset(previous) === "sentinel" && sensorForDataset($("dataset").value) === "sentinel");
    clearSearch();
    persist();
  });
  $("search-form").addEventListener("change", (event) => {
    if (
      event.target.id !== "dataset" &&
      event.target.id !== "nisar-mode" &&
      !event.target.closest(".download-options")
    )
      clearSearch();
  });
  $("nisar-mode").addEventListener("change", () => {
    const changed =
      $("nisar-mode").value === "subset" && $("product").value !== "RSLC";
    syncDownloadControls();
    if (changed) clearSearch("按范围裁剪使用 RSLC，请重新检索。");
  });
  $("companion-dem-format").addEventListener("change", syncDownloadControls);
  document
    .querySelectorAll("[data-auxiliary]")
    .forEach((button) =>
      button.addEventListener("click", () => showTab(button.dataset.auxiliary)),
    );
  $("cancel-search").addEventListener("click", () =>
    clearSearch("已取消检索，可调整条件后重试。"),
  );
  $("reset-filters").addEventListener("click", resetFilters);
  $("empty-reset").addEventListener("click", resetFilters);
  $("apply-aoi").addEventListener("click", () => {
    try {
      setError();
      applyPendingBounds(true);
    } catch (error) {
      setError(error.message);
    }
  });
  document
    .querySelectorAll("[data-tab]")
    .forEach((button) =>
      button.addEventListener("click", () => showTab(button.dataset.tab)),
    );
  document
    .querySelector('[role="tablist"]')
    .addEventListener("keydown", (event) => {
      const buttons = [...document.querySelectorAll("[data-tab]")],
        index = buttons.indexOf(document.activeElement);
      if (index < 0) return;
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft")
        next = (index + buttons.length - 1) % buttons.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = buttons.length - 1;
      else return;
      event.preventDefault();
      showTab(buttons[next].dataset.tab);
      buttons[next].focus();
    });
  document
    .querySelectorAll("[data-aoi-mode]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        setAOIMode(button.dataset.aoiMode),
      ),
    );
  $("fit-results").addEventListener("click", () => workbenchMap?.fitScenes());
  $("map-frame").addEventListener("change", (event) =>
    focusGroup(event.target.value),
  );
  $("previous-frame").addEventListener("click", () => moveFrame(-1));
  $("next-frame").addEventListener("click", () => moveFrame(1));
  $("show-all-frames").addEventListener("click", () => focusGroup(""));
  $("fit-aoi").addEventListener("click", () => workbenchMap?.fitBounds());
  $("draw-aoi").addEventListener("click", () => setAOIMode("draw"));
  $("map-import").addEventListener("click", () => setAOIMode("import"));
  $("result-body").addEventListener("change", (event) => {
    if (event.target.dataset.selectScene)
      toggleSelection(event.target.dataset.selectScene);
  });
  $("result-body").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.focusScene) focusScene(button.dataset.focusScene);
    if (button.dataset.detail) showDetails(button.dataset.detail);
    if (button.dataset.path) {
      $("path-filter").value = button.dataset.path;
      search();
    }
  });
  $("result-body").addEventListener("pointerover", (event) => {
    const row = event.target.closest("[data-scene-row]");
    if (row) workbenchMap?.highlightScene(row.dataset.sceneRow);
  });
  $("result-body").addEventListener("pointerleave", () =>
    workbenchMap?.clearHighlight(),
  );
  $("select-all").addEventListener("change", (event) => {
    for (const scene of visibleScenes())
      event.target.checked
        ? state.selected.add(scene.id)
        : state.selected.delete(scene.id);
    invalidateDEM();
    updateSelection();
  });
  $("clear-selection").addEventListener("click", () => {
    state.selected.clear();
    invalidateDEM();
    updateSelection();
  });
  $("queue-scenes").addEventListener("click", () =>
    prepareSceneTasks(selectedScenes()),
  );
  $("copy-links").addEventListener("click", async () => {
    const includeXML =
      state.dataset === "SLC-BURST" && $("include-burst-xml").checked;
    const urls = [
      ...new Set(
        selectedScenes().flatMap((scene) =>
          sceneDownloadUrls(scene, includeXML),
        ),
      ),
    ];
    if (!urls.length) return toast("所选记录未提供下载链接。");
    try {
      await navigator.clipboard.writeText(urls.join("\n"));
      toast(
        `已复制 ${urls.length} 个 ASF 官方链接${includeXML ? "（含 XML）" : ""}。`,
      );
    } catch {
      downloadFile("sar-links.txt", urls.join("\r\n"), "text/plain");
      toast("剪贴板不可用，已导出链接文件。");
    }
  });
  $("export-results").addEventListener("click", exportCSV);
  $("help-button").addEventListener("click", () => {
    const frame = $("help-frame");
    if (!frame.hasAttribute("src")) frame.src = frame.dataset.src;
    openDialog("help-dialog");
  });
  $("account-button").addEventListener("click", () =>
    openDialog("account-dialog"),
  );
  document
    .querySelectorAll("[data-close-dialog]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        closeDialog(button.dataset.closeDialog),
      ),
    );
  for (const dialog of document.querySelectorAll("dialog"))
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) {
        const box = dialog.getBoundingClientRect();
        if (
          event.clientX < box.left ||
          event.clientX > box.right ||
          event.clientY < box.top ||
          event.clientY > box.bottom
        )
          if (dialog.dataset.busy !== "true" && (dialog.id !== "subset-dialog" || !subsetDialog.isBusy())) dialog.close();
      }
    });
  $("show-footprints").addEventListener("change", (event) =>
    workbenchMap?.showScenes(event.target.checked),
  );
  $("choose-folder").addEventListener("click", chooseOrbitFolder);
  $("orbit-empty-open").addEventListener("click", chooseOrbitFolder);
  $("queue-orbits").addEventListener("click", matchOrbitFiles);
  $("queue-matched-orbits").addEventListener("click", () =>
    downloadsUI.addAssets(state.orbitAssets || []),
  );
  $("orbit-strategy").addEventListener("change", () => {
    state.orbitAssets = [];
    state.orbits = state.orbits.map((scene) => ({
      ...scene,
      state: "unmatched",
      orbit: null,
    }));
    $("orbit-match-status").textContent = "策略已更新，请重新匹配";
    renderOrbits();
  });
  $("preview-dem").addEventListener("click", previewDEM);
  ["dem-source", "dem-extent", "dem-buffer", "dem-height", "dem-format"].forEach((id) =>
    $(id).addEventListener("change", () => {
      syncDEMControls();
      invalidateDEM();
    }),
  );
  $("queue-dem").addEventListener("click", () => {
    if (!state.dem) return;
    const plan = state.dem;
    if (plan.format === "tiles")
      downloadsUI.addAssets(
        plan.tiles
          .filter((tile) => tile.available)
          .map((tile) => ({
            kind: "dem",
            url: tile.url,
            filename: tile.filename,
            title: `CopDEM · ${tile.id} · EGM2008`,
          })),
      );
    else
      downloadsUI.addDem({
        source: ["COP", "NISAR", "SRTM"].includes(plan.source) ? plan.source : "COP",
        bounds: plan.bounds,
        buffer: plan.buffer,
        height: plan.height,
        format: plan.format,
      });
  });
  $("back-to-search").addEventListener("click", () => showTab("search"));
  $("export-project").addEventListener("click", () => {
    downloadFile(
      "sar-downloader-project.json",
      JSON.stringify(
        {
          version: 1,
          mode: "live-search",
          search: state.lastRequest,
          tasks: [...state.tasks, ...downloadsUI.getTasks()],
          downloadSettings: {
            concurrent: $("concurrent").value,
            output: $("output-folder").value,
          },
        },
        null,
        2,
      ),
      "application/json",
    );
    toast("已导出任务配置，不包含账号或底图 Key。");
  });
  workbenchMap?.map.on("moveend", schedulePersist);
  document.addEventListener("input", (event) => {
    if (FORM_IDS.includes(event.target.id)) schedulePersist();
  });
  document.addEventListener("change", schedulePersist);
  document.addEventListener("click", schedulePersist);
  $("advanced-filters").addEventListener("toggle", schedulePersist);
  document
    .querySelector(".sidebar")
    .addEventListener("scroll", schedulePersist);
  document
    .querySelector(".result-table-wrapper")
    .addEventListener("scroll", schedulePersist);
  window.addEventListener("pagehide", persist);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) persist();
  });
  workspaceReady = true;
  document.querySelector(".workspace").inert = false;
  requestAnimationFrame(() => {
    const target = startupMapTarget(
      cached,
      performance.getEntriesByType("navigation")[0]?.type === "reload",
    );
    if (target?.kind === "view") workbenchMap?.restoreView(target.view);
    else if (target?.kind === "bounds") workbenchMap?.fitExtent(target.bounds);
    if (cached?.scroll) {
      document.querySelector(".sidebar").scrollTop = cached.scroll.sidebar || 0;
      document.querySelector(".result-table-wrapper").scrollTop =
        cached.scroll.results || 0;
    }
    persist();
  });
  api("/api/health")
    .then((result) => {
      if (
        !result.capabilities?.downloads ||
        !result.capabilities?.preflight ||
        !result.capabilities?.auxiliaryFolders
      )
        toast(
          "请关闭旧启动窗口并重新运行“启动SAR Downloader.cmd”，以启用辅助数据子目录与下载前检查。",
          true,
        );
      else if (state.searchPhase === "idle" && state.tab === "search")
        setStatus("ASF 真实检索已就绪 · 设置范围和日期后开始");
    })
    .catch(() => setStatus("本地服务未连接，请通过启动器打开。"));
}
function restoreWorkspace(cached) {
  try {
    if (cached.bounds !== null) validateBounds(cached.bounds);
    if (cached.geometry) geometryBounds(cached.geometry);
    updateBounds(cached.bounds, cached.geometry || null, false);
    const forms = workspaceForms(cached.forms);
    for (const id of FORM_IDS) {
      const value = forms[id],
        element = $(id);
      if (value === undefined || id === "dataset") continue;
      if (element.type === "checkbox") element.checked = Boolean(value);
      else if (
        element.tagName !== "SELECT" ||
        [...element.options].some((option) => option.value === value)
      )
        element.value = value;
    }
    syncDownloadControls();
    state.results = Array.isArray(cached.results)
      ? cached.results
          .filter(
            (scene) =>
              scene &&
              typeof scene.id === "string" &&
              Array.isArray(scene.bounds),
          )
          .slice(0, 1000)
      : [];
    state.selected = new Set(
      (cached.selected || []).filter((id) =>
        state.results.some((scene) => scene.id === id),
      ),
    );
    state.activeGroup = groupScenes(state.results).some(
      (group) => group.key === cached.activeGroup,
    )
      ? cached.activeGroup
      : "";
    state.activeScene = state.results.some(
      (scene) => scene.id === cached.activeScene,
    )
      ? cached.activeScene
      : null;
    state.searchPhase = ["idle", "done", "error"].includes(cached.searchPhase)
      ? cached.searchPhase
      : state.results.length
        ? "done"
        : "idle";
    state.searchMessage =
      cached.searchPhase === "loading"
        ? "上次检索被刷新中断，研究区与条件已保留，请重新检索。"
        : cached.searchMessage || state.searchMessage;
    state.warnings = Array.isArray(cached.warnings) ? cached.warnings : [];
    state.lastRequest = cached.lastRequest;
    state.lastSearchBounds = successfulSearchBounds(cached);
    state.lastSearchAt = Number.isFinite(cached.lastSearchAt)
      ? cached.lastSearchAt
      : 0;
    state.searchSlots =
      cached.searchSlots && typeof cached.searchSlots === "object"
        ? cached.searchSlots
        : {};
    state.auxiliaryTab = cached.auxiliaryTab === "orbit" ? "orbit" : "dem";
    state.orbits = Array.isArray(cached.orbits) ? cached.orbits : [];
    state.orbitAssets = Array.isArray(cached.orbitAssets)
      ? cached.orbitAssets
      : [];
    state.dem = cached.dem?.tiles ? cached.dem : null;
    state.tab = ["search", "orbit", "dem", "downloads"].includes(cached.tab)
      ? cached.tab
      : "search";
    state.taskFilter = [
      "all",
      "queued",
      "active",
      "paused",
      "completed",
      "error",
    ].includes(cached.taskFilter)
      ? cached.taskFilter
      : "all";
    $("orbit-folder-label").textContent = cached.orbitLabel || "未选择文件夹";
    $("orbit-match-status").textContent =
      cached.orbitStatus === "匹配中…"
        ? "请重新匹配"
        : cached.orbitStatus || "尚未匹配";
    $("aoi-instructions").textContent = cached.aoiText || "";
    $("aoi-instructions").hidden = cached.aoiHidden !== false;
    document.querySelectorAll("[data-aoi-mode]").forEach((button) => {
      const active =
        button.dataset.aoiMode === (cached.aoiMode || "coordinates");
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active);
    });
    $("advanced-filters").open = Boolean(cached.advanced);
    $("show-footprints").checked = cached.showFootprints !== false;
    workbenchMap?.showScenes($("show-footprints").checked);
    syncDownloadControls();
    syncDEMControls();
  } catch {
    setStatus("部分本机工作状态无法恢复，请检查研究区与筛选条件。");
  }
}
document.querySelector(".workspace").inert = true;
async function rememberedSearch() {
  try {
    return (await api("/api/last-search", undefined, { timeout: 2500 }))
      .location;
  } catch {
    return null;
  }
}
Promise.all([
  workspace.load(),
  rememberedSearch(),
  api("/api/basemap", undefined, { timeout: 5000 }).catch((error) => ({
    loadError: error.message,
  })),
])
  .then(([cached, location, basemap]) => {
    initialize(
      mergeSearchLocation(cached, location),
      basemap.loadError ? null : basemap,
    );
    if (basemap.loadError || basemap.warning)
      toast(basemap.loadError || basemap.warning, true);
  })
  .catch(() => initialize(null));
