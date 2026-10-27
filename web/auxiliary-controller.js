import { api } from "./api.js";
import { unionBounds } from "./core.js";
import { demSettings } from "./dem-settings.js";

export function createAuxiliaryController({
  state,
  getMap,
  escape,
  icon,
  toast,
  schedulePersist,
  persist,
  selectedScenes,
}) {
  const $ = (id) => document.getElementById(id);
  function syncDEMControls() {
    const settings = demSettings($("dem-source").value);
    $("dem-source").value = settings.source;
    const previousHeight = $("dem-height").value;
    $("dem-height").replaceChildren(...settings.heights.map(([value, label]) => new Option(label, value)));
    $("dem-height").value = settings.heights.some(([value]) => value === previousHeight) ? previousHeight : "ellipsoid";
    const tileOption = [...$("dem-format").options].find((option) => option.value === "tiles");
    tileOption.disabled = !settings.tiles;
    if (!settings.tiles && $("dem-format").value === "tiles") $("dem-format").value = "GTiff";
    $("dem-height").disabled = settings.source === "NISAR" || $("dem-format").value === "tiles";
    if ($("dem-format").value === "tiles") $("dem-height").value = "egm2008";
    $("dem-source-note").textContent = settings.note;
  }
  function renderOrbits() {
    const stamp = (value) =>
      value
        ? `${value.slice(0, 4)}-${value.slice(4, 6)}-${value.slice(6, 8)} ${value.slice(9, 11)}:${value.slice(11, 13)}:${value.slice(13, 15)}`
        : "—";
    $("orbit-body").innerHTML = state.orbits
      .map(
        (scene) =>
          `<tr><td>${escape(scene.platform)}</td><td title="${escape(scene.id)}">${escape(scene.id)}</td><td>${escape(stamp(scene.start))}<br><span class="tiny-note">至 ${escape(stamp(scene.end))}</span></td><td>${scene.orbit ? `<span class="badge badge-neutral">${escape(scene.orbit.type)}</span><a class="orbit-file" href="${escape(scene.orbit.url)}" target="_blank" rel="noopener noreferrer" title="${escape(scene.orbit.filename)}">${escape(scene.orbit.filename)}</a>` : "—"}</td><td><span class="badge ${scene.state === "missing" ? "badge-amber" : "badge-neutral"}" title="${escape(scene.message || "")}">${scene.state === "matched" ? "已匹配" : scene.state === "missing" ? "未找到" : "待匹配"}</span></td></tr>`,
      )
      .join("");
    $("orbit-count").innerHTML = `${state.orbits.length} <small>景</small>`;
    $("orbit-empty").hidden = state.orbits.length > 0;
    $("queue-matched-orbits").disabled = !state.orbitAssets?.length;
    schedulePersist();
  }

  async function chooseOrbitFolder() {
    const buttons = [$("choose-folder"), $("orbit-empty-open")];
    buttons.forEach((button) => (button.disabled = true));
    try {
      const result = await api("/api/orbits/folder", {});
      if (result.cancelled) return;
      state.orbits = result.scenes;
      state.orbitAssets = [];
      $("orbit-folder-label").textContent = result.label;
      $("orbit-match-status").textContent = "尚未匹配";
      renderOrbits();
      toast(
        `已识别 ${state.orbits.length} 景 Sentinel-1。${(result.warnings || []).join(" ")}`,
      );
    } catch (error) {
      toast(
        error.name === "AbortError"
          ? "目录选择或扫描超时，请重试。"
          : error.message,
        true,
      );
    } finally {
      buttons.forEach((button) => (button.disabled = false));
    }
  }
  async function matchOrbitFiles() {
    if (!state.orbits.length)
      return toast("请先选择 Sentinel-1 影像文件夹。", true);
    const controls = [
      "queue-orbits",
      "choose-folder",
      "orbit-strategy",
      "queue-matched-orbits",
    ].map($);
    controls.forEach((control) => (control.disabled = true));
    $("orbit-match-status").textContent = "匹配中…";
    try {
      const result = await api("/api/orbits/match", {
        scenes: state.orbits.map((scene) => scene.id),
        strategy: $("orbit-strategy").value,
      });
      state.orbits = result.scenes;
      state.orbitAssets = result.assets;
      renderOrbits();
      $("orbit-match-status").textContent =
        `${result.matched}/${result.scenes.length} 景 · ${result.files} 个 EOF`;
      toast(
        `已匹配 ${result.matched} 景，去重后 ${result.files} 个轨道文件${result.missing ? `；${result.missing} 景暂未找到` : ""}。`,
      );
    } catch (error) {
      state.orbitAssets = [];
      $("orbit-match-status").textContent = "匹配失败";
      toast(
        error.name === "AbortError"
          ? "轨道目录读取超时，请稍后重试。"
          : error.message,
        true,
      );
    } finally {
      controls.forEach((control) => (control.disabled = false));
      $("queue-matched-orbits").disabled = !state.orbitAssets?.length;
    }
  }

  let demSerial = 0;
  function invalidateDEM() {
    demSerial++;
    state.dem = null;
    $("dem-tiles").replaceChildren();
    $("dem-count").textContent = "0";
    $("dem-empty").hidden = false;
    $("dem-note-panel").hidden = true;
    $("queue-dem").disabled = true;
    getMap()?.setTiles([]);
    schedulePersist();
  }
  function renderDEM() {
    if (!state.dem) return;
    const plan = state.dem;
    $("dem-count").textContent = plan.available;
    $("dem-description").textContent =
      `${plan.sourceLabel || "Copernicus GLO-30"} · ${plan.available}/${plan.tiles.length} 个${plan.coverageVerified === false ? "规划" : "公开"}瓦片 · ${plan.buffer} km 缓冲 · ${plan.height === "ellipsoid" ? "WGS84 椭球高" : plan.height.toUpperCase()}`;
    $("dem-empty").hidden = true;
    $("queue-dem").disabled = !plan.available;
    $("dem-tiles").innerHTML = plan.tiles
      .map(
        (tile) =>
          `<div class="dem-tile ${tile.available ? "" : "is-missing"}">${icon("mountain")}<span>${escape(tile.id)}</span><span class="tiny-note">${tile.available ? (plan.coverageVerified === false ? "待源站核验" : "可下载") : "目录缺失"}</span></div>`,
      )
      .join("");
    $("dem-note").textContent = plan.warnings.join(" ");
    $("dem-note-panel").hidden = !plan.warnings.length;
    getMap()?.setTiles(state.tab === "dem" ? plan.tiles : []);
    schedulePersist();
  }
  async function previewDEM() {
    invalidateDEM();
    const serial = demSerial;
    const button = $("preview-dem");
    button.disabled = true;
    button.textContent = "检查中…";
    try {
      const buffer = Number($("dem-buffer").value);
      if (!Number.isFinite(buffer) || buffer < 0 || buffer > 100)
        throw new Error("缓冲距离应介于 0–100 km。");
      let bounds;
      if ($("dem-extent").value === "selection") {
        const selected = selectedScenes();
        if (!selected.length)
          throw new Error("请先选择影像，或将覆盖范围切换为“当前研究区”。");
        bounds = unionBounds(selected.map((scene) => scene.bounds));
      } else {
        if (!state.bounds) throw new Error("请先在地图中绘制研究区或导入范围。");
        bounds = state.bounds;
      }
      const format = $("dem-format").value,
        height = format === "tiles" ? "egm2008" : $("dem-height").value;
      const result = await api("/api/dem/preview", {
        source: $("dem-source").value,
        bounds,
        buffer,
        height,
        format,
      });
      if (serial !== demSerial) return;
      state.dem = result;
      renderDEM();
      persist();
      toast(
        state.dem.coverageVerified === false ? `已规划 ${state.dem.available} 个瓦片，下载时核验源站；请先登录 Earthdata。` : `源站可下载 ${state.dem.available} 个瓦片${state.dem.missing.length ? `，${state.dem.missing.length} 个目录缺失` : ""}。`,
      );
    } catch (error) {
      if (serial !== demSerial) return;
      invalidateDEM();
      toast(
        error.name === "AbortError"
          ? "DEM 目录读取超时，请重试。"
          : error.message,
        true,
      );
    } finally {
      button.disabled = false;
      button.textContent = "检查覆盖瓦片";
    }
  }

  return {
    renderOrbits,
    chooseOrbitFolder,
    matchOrbitFiles,
    invalidateDEM,
    renderDEM,
    previewDEM,
    syncDEMControls,
  };
}
