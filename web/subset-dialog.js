import { validateBounds, sceneGeometry } from "./core.js";
import { api } from "./api.js";
import { createBasemap } from "./basemap.js";

const $ = (id) => document.getElementById(id);
const EDGES = ["west", "south", "east", "north"];

export function subsetBounds(bounds) {
  return validateBounds(validateBounds(bounds).map((value, index) => {
    const scaled = Math.round(value * 1e8) / 1e6;
    return (index < 2 ? Math.floor(scaled) : Math.ceil(scaled)) / 100;
  }));
}

export function subsetOptions(bounds, values) {
  const [w, s, e, n] = subsetBounds(bounds);
  const low = values.minHeight === "" ? null : Number(values.minHeight);
  const high = values.maxHeight === "" ? null : Number(values.maxHeight);
  if ([low, high].some((v) => v !== null && (!Number.isFinite(v) || v < -1000 || v > 10000)) ||
      (low !== null && high !== null && low > high))
    throw new Error("请检查高程范围，最低值不能大于最高值。留空时使用全部高度层。");
  return {
    corners: [[w, n], [e, n], [e, s], [w, s]],
    frequencies: values.frequency === "AB" ? ["A", "B"] : [values.frequency],
    polarizations: values.polarization.split(","),
    connections: Number(values.connections),
    min_height: values.minHeight,
    max_height: values.maxHeight,
    includeDEM: values.includeDEM,
  };
}

export function createSubsetDialog({ getBounds, getSettings, getBasemap, onConfirm, toast }) {
  const dialog = $("subset-dialog"), form = $("subset-form");
  let map, basemap, outlines, polygon, markers = [], scenes = [], busy = false, mapKind = "osm";
  function setBasemap(kind) {
    const { key, provider } = getBasemap();
    if (basemap.setBasemap(kind, key, provider)) mapKind = kind;
    $("subset-basemap").value = mapKind;
  }
  const readBounds = () => EDGES.map((edge) => {
    const value = $("crop-" + edge).value.trim();
    return value === "" ? NaN : Number(value);
  });
  function writeBounds(bounds) {
    subsetBounds(bounds).forEach((value, i) => { $("crop-" + EDGES[i]).value = value.toFixed(2); });
    updateMap();
  }
  function updateMap() {
    try {
      const [w, s, e, n] = validateBounds(readBounds());
      markers.forEach((marker) => marker.dragging.enable());
      $("subset-error").hidden = true;
      if (!map) return;
      const points = [[n, w], [n, e], [s, e], [s, w]];
      if (polygon) polygon.setLatLngs(points);
      else polygon = window.L.polygon(points, { color: "#c46b2a", weight: 2, fillOpacity: .13 }).addTo(map);
      points.forEach((point, i) => {
        if (markers[i]) markers[i].setLatLng(point);
        else {
          const marker = window.L.marker(point, {
            draggable: true,
            icon: window.L.divIcon({ className: "subset-corner", html: ["NW", "NE", "SE", "SW"][i], iconSize: [28, 28], iconAnchor: [14, 14] }),
          }).addTo(map);
          marker.on("drag", () => {
            const bounds = validateBounds(readBounds()), { lat, lng } = marker.getLatLng();
            const x = Math.max(-180, Math.min(180, lng)), y = Math.max(-85, Math.min(85, lat));
            if (i === 0 || i === 3) bounds[0] = Math.min(x, bounds[2] - .01);
            else bounds[2] = Math.max(x, bounds[0] + .01);
            if (i < 2) bounds[3] = Math.max(y, bounds[1] + .01);
            else bounds[1] = Math.min(y, bounds[3] - .01);
            writeBounds(bounds);
          });
          markers[i] = marker;
        }
      });
    } catch (error) {
      markers.forEach((marker) => marker.dragging.disable());
      $("subset-error").textContent = error.message;
      $("subset-error").hidden = false;
    }
  }
  function fit() {
    if (!polygon) return;
    map.invalidateSize({ animate: false });
    map.fitBounds(polygon.getBounds().pad(.3), { maxZoom: 17, animate: false });
  }
  EDGES.forEach((edge) => {
    $("crop-" + edge).addEventListener("input", updateMap);
    $("crop-" + edge).addEventListener("change", () => {
      try { writeBounds(readBounds()); } catch { updateMap(); }
    });
  });
  $("subset-fit").addEventListener("click", fit);
  $("subset-basemap").addEventListener("change", () => setBasemap($("subset-basemap").value));
  $("subset-reset-area").addEventListener("click", () => { writeBounds(getBounds()); fit(); });
  $("subset-pick-directory").addEventListener("click", async () => {
    const button = $("subset-pick-directory");
    button.disabled = true;
    try {
      const result = await api("/api/downloads/folder", {});
      if (result.output && dialog.open) $("subset-directory").value = result.output;
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });
  // Keep the two visible DEM switches in step; the submitted value is snapshotted below.
  $("subset-include-dem").addEventListener("change", () => { $("nisar-include-dem").checked = $("subset-include-dem").checked; });
  dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    try {
      const options = subsetOptions(readBounds(), {
        frequency: $("subset-frequency").value,
        polarization: $("subset-polarization").value,
        connections: $("subset-connections").value,
        minHeight: $("subset-min-height").value,
        maxHeight: $("subset-max-height").value,
        includeDEM: $("subset-include-dem").checked,
      });
      writeBounds(readBounds());
      busy = true;
      $("subset-fields").disabled = true;
      $("subset-start").disabled = true;
      $("subset-close").disabled = true;
      $("subset-start").textContent = "正在创建任务…";
      if (await onConfirm(scenes, options, $("subset-directory").value.trim())) dialog.close();
    } catch (error) {
      $("subset-error").textContent = error.message;
      $("subset-error").hidden = false;
    } finally {
      busy = false;
      $("subset-fields").disabled = false;
      $("subset-start").disabled = false;
      $("subset-close").disabled = false;
      $("subset-start").textContent = "开始下载";
    }
  });
  return {
    open(selected) {
      const L = window.L;
      if (busy || !selected.length) return;
      if (selected.some((scene) => scene.dataset !== "NISAR" || scene.product !== "RSLC"))
        return toast("按范围裁剪只支持 NISAR RSLC，请重新检索。", true);
      scenes = [...selected];
      $("subset-summary").textContent = `已选 ${scenes.length} 景 · L-band RSLC`;
      $("subset-directory").value = getSettings().output || "";
      $("subset-include-dem").checked = $("nisar-include-dem").checked;
      writeBounds(getBounds());
      dialog.showModal();
      if (!map) {
        map = L.map("subset-map", { zoomControl: false, minZoom: 2, maxZoom: 19 }).setView([30, 104], 6);
        L.control.zoom({ position: "topright" }).addTo(map);
        L.control.scale({ imperial: false }).addTo(map);
        map.createPane("subset-reference").style.zIndex = 190;
        map.getPane("subset-reference").style.pointerEvents = "none";
        basemap = createBasemap(map, {
          onStatus: (message) => { $("subset-map-status").textContent = message; },
          onSatelliteRequest: () => { $("subset-map-status").textContent = "请先在主界面“个人中心”配置卫星底图 Key。"; },
        });
        outlines = L.layerGroup().addTo(map);
        fetch("./assets/world.geojson").then((r) => r.json()).then((data) => {
          L.geoJSON(data, { pane: "subset-reference", interactive: false, style: { color: "#b9c8d8", weight: 1, fillColor: "#e6edf5", fillOpacity: 1 } }).addTo(map);
          map.attributionControl.addAttribution("轮廓 © Natural Earth");
        }).catch(() => {});
      }
      setBasemap(getBasemap().kind);
      outlines.clearLayers();
      for (const scene of scenes) L.geoJSON(sceneGeometry(scene), { interactive: false, style: { color: "#2563a6", weight: 1.3, fillOpacity: .03 } }).addTo(outlines);
      updateMap();
      requestAnimationFrame(fit);
    },
    isBusy: () => busy,
  };
}
