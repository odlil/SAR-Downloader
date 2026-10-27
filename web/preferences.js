import { api } from "./api.js";

export function normalizeBasemap(saved) {
  const provider = ["tianditu", "esri"].includes(saved?.provider)
    ? saved.provider
    : "tianditu";
  const keys = Object.fromEntries(
    Object.entries(saved?.keys || {}).filter(
      ([name, value]) =>
        ["tianditu", "esri"].includes(name) &&
        typeof value === "string" &&
        value,
    ),
  );
  return {
    provider,
    keys,
    kind: saved?.kind === "satellite" && keys[provider] ? "satellite" : "osm",
  };
}

export function createPreferences({
  state,
  initial,
  getMap,
  toast,
  openDialog,
  closeDialog,
}) {
  const $ = (id) => document.getElementById(id);
  let saved = normalizeBasemap(initial),
    writes = Promise.resolve();

  function fields() {
    const provider = $("satellite-provider").value;
    $("basemap-key").value = saved.keys[provider] || "";
    $("provider-docs").href =
      provider === "tianditu"
        ? "https://lbs.tianditu.gov.cn/"
        : "https://developers.arcgis.com/documentation/";
  }
  function restore() {
    state.satelliteProvider = saved.provider;
    state.mapKey = saved.keys[saved.provider] || "";
    state.basemap = saved.kind;
    $("satellite-provider").value = saved.provider;
    $("basemap").value = saved.kind;
    fields();
  }
  // A slow earlier save must not overtake the latest selection in this page.
  function save(payload) {
    const operation = writes.then(() => api("/api/basemap", payload));
    writes = operation.then(
      (result) => {
        saved = normalizeBasemap(result);
      },
      () => {},
    );
    return operation;
  }

  restore();
  $("satellite-provider").addEventListener("change", fields);
  $("basemap").addEventListener("change", async () => {
    const kind = $("basemap").value;
    if (!getMap()?.setBasemap(kind, state.mapKey, state.satelliteProvider))
      return;
    state.basemap = kind;
    try {
      await save({ provider: state.satelliteProvider, kind });
    } catch (error) {
      toast(error.message, true);
    }
  });
  $("apply-basemap").addEventListener("click", async () => {
    const key = $("basemap-key").value.trim(),
      provider = $("satellite-provider").value;
    if (!key) return toast("请先填写所选服务的 API Key / Token。", true);
    const controls = ["apply-basemap", "satellite-provider", "basemap"].map($);
    controls.forEach((control) => (control.disabled = true));
    try {
      await save({ provider, kind: "satellite", key });
      restore();
      getMap()?.setBasemap(
        state.basemap,
        state.mapKey,
        state.satelliteProvider,
      );
      closeDialog("account-dialog");
      toast("卫星底图已应用并加密保存，下次打开自动恢复。");
    } catch (error) {
      toast(error.message, true);
    } finally {
      controls.forEach((control) => (control.disabled = false));
    }
  });
  return {
    requestSatellite() {
      openDialog("account-dialog");
      toast("请先配置卫星底图的 API Key。");
      $("basemap").value = state.basemap;
    },
  };
}
