// The main map and subset preview use the same providers, attribution and key handling.
export function createBasemap(map, { onStatus = () => {}, onSatelliteRequest = () => {} } = {}) {
  const L = window.L;
  let layers = [], generation = 0;
  function setBasemap(kind, key = "", provider = "tianditu") {
    if (kind === "satellite" && !key) {
      onSatelliteRequest();
      return false;
    }
    const current = ++generation;
    layers.forEach((layer) => map.removeLayer(layer));
    let label;
    if (kind === "osm") {
      label = "OpenStreetMap";
      layers = [L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
        maxZoom: 19, referrerPolicy: "strict-origin-when-cross-origin",
      })];
    } else if (provider === "tianditu") {
      label = "天地图影像";
      const url = (layer) => `https://t{s}.tianditu.gov.cn/${layer}_w/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=${layer}&STYLE=default&TILEMATRIXSET=w&FORMAT=tiles&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&tk=${encodeURIComponent(key)}`;
      layers = [
        L.tileLayer(url("img"), { subdomains: "01234567", maxZoom: 18, attribution: '© <a href="https://www.tianditu.gov.cn/" target="_blank" rel="noopener">天地图</a>' }),
        L.tileLayer(url("cia"), { subdomains: "01234567", maxZoom: 18 }),
      ];
    } else {
      label = "Esri World Imagery";
      layers = [L.tileLayer(`https://ibasemaps-api.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}?token=${encodeURIComponent(key)}`, {
        maxZoom: 19, attribution: "Powered by Esri · Sources: Esri, Vantor, Earthstar Geographics, GIS User Community",
      })];
    }
    let failed = false;
    onStatus("在线底图连接中…");
    layers.forEach((layer) => {
      layer.on("tileload", () => { if (generation === current && !failed) onStatus(label); });
      layer.on("tileerror", () => {
        if (generation !== current) return;
        failed = true;
        onStatus("部分底图瓦片不可用 · 请检查网络或稍后重试");
      });
      layer.addTo(map);
    });
    return true;
  }
  return { setBasemap };
}
