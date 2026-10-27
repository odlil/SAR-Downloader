import { api } from "./api.js";
import { buildSearchRequest, unionBounds } from "./core.js";

export function createSearchController({
  state,
  getMap,
  readFilters,
  updateBounds,
  setAOIMode,
  clearActiveScene,
  invalidateDEM,
  renderResults,
  persist,
  setError,
  setStatus,
}) {
  const $ = (id) => document.getElementById(id);
  let querySerial = 0,
    searchController;
  function setSearchBusy(busy) {
    $("search-button").disabled = busy;
    $("search-button-label").textContent = busy ? "检索中…" : "检索影像";
    $("cancel-search").hidden = !busy;
    $("panel-search").setAttribute("aria-busy", String(busy));
  }
  function cancelSearch() {
    querySerial++;
    searchController?.abort();
    searchController = null;
    setSearchBusy(false);
  }
  function clearSearch(message = "筛选已更新，请点击检索影像。") {
    cancelSearch();
    clearActiveScene();
    state.results = [];
    state.selected.clear();
    state.warnings = [];
    state.searchPhase = "idle";
    state.searchMessage = message;
    setError();
    invalidateDEM();
    renderResults();
    setStatus(message);
  }
  async function search() {
    cancelSearch();
    const serial = ++querySerial;
    try {
      setError();
      const filters = readFilters();
      const changed = !state.bounds || filters.bounds.some(
        (value, index) => Math.abs(value - state.bounds[index]) > 0.00001,
      );
      if (changed) {
        updateBounds(filters.bounds, null, false);
        setAOIMode("coordinates");
      }
      state.lastRequest = buildSearchRequest(filters, state.geometry);
      clearActiveScene();
      state.results = [];
      state.selected.clear();
      state.warnings = [];
      state.searchPhase = "loading";
      invalidateDEM();
      renderResults();
      setSearchBusy(true);
      setStatus("正在查询 ASF 真实影像目录…");
      const controller = new AbortController();
      searchController = controller;
      if (["ALOS", "ALOS-2"].includes(filters.dataset)) {
        const health = await api("/api/health", undefined, { signal: controller.signal });
        if (serial !== querySerial) return;
        if (!health.capabilities?.alos)
          throw new Error(`当前本地服务${health.version ? ` v${health.version}` : ""}尚未支持本页的 ALOS / ALOS-2 选项。请重新运行新版“启动SAR Downloader.cmd”并刷新页面；如有下载，请先暂停并等待停止后再关闭旧启动窗口。`);
      }
      const result = await api("/api/search", state.lastRequest, {
        signal: controller.signal,
      });
      if (serial !== querySerial) return;
      if (!Array.isArray(result.scenes)) throw new Error("检索返回格式异常。");
      state.results = result.scenes;
      state.warnings = result.warnings || [];
      state.searchPhase = "done";
      state.lastSearchBounds = state.results.length
        ? unionBounds(state.results.map((scene) => scene.bounds))
        : [...filters.bounds];
      state.lastSearchAt = Date.now();
      state.searchMessage = `ASF · 返回 ${state.results.length} 条真实记录${result.truncated ? " · 已达到候选上限" : ""}`;
      renderResults();
      getMap()?.fitScenes();
      persist();
      setStatus(state.searchMessage);
    } catch (error) {
      if (serial !== querySerial) return;
      state.results = [];
      state.selected.clear();
      state.warnings = [];
      state.searchPhase = "error";
      state.searchMessage =
        error.name === "AbortError"
          ? "检索超时，请检查网络或缩小条件后重试。"
          : error.message === "Failed to fetch"
            ? "无法连接本地服务，请重新运行启动器。"
            : error.message;
      setError(state.searchMessage);
      renderResults();
      setStatus(state.searchMessage);
    } finally {
      if (serial === querySerial) {
        searchController = null;
        setSearchBusy(false);
      }
    }
  }

  return { search, cancelSearch, clearSearch };
}
