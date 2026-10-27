import test from "node:test";
import assert from "node:assert/strict";
import { createBasemap } from "../web/basemap.js";

test("switching basemaps preserves the existing map when a satellite key is missing", (t) => {
  const layers = [], removed = [], statuses = [];
  let missing = 0;
  const previous = Object.getOwnPropertyDescriptor(globalThis, "window");
  t.after(() => { if (previous) Object.defineProperty(globalThis, "window", previous); else delete globalThis.window; });
  globalThis.window = { L: { tileLayer(url, options) {
    const layer = { url, options, handlers: {}, on(event, fn) { this.handlers[event] = fn; return this; }, addTo() { return this; } };
    layers.push(layer);
    return layer;
  } } };
  const basemap = createBasemap({ removeLayer: (layer) => removed.push(layer) }, {
    onStatus: (message) => statuses.push(message), onSatelliteRequest: () => missing++,
  });
  basemap.setBasemap("osm");
  assert.match(layers[0].options.attribution, /openstreetmap.org\/copyright/);
  assert.equal(basemap.setBasemap("satellite"), false);
  assert.equal(missing, 1);
  assert.equal(removed.length, 0);
  basemap.setBasemap("satellite", "fixture key", "tianditu");
  assert.deepEqual(removed, [layers[0]]);
  assert.equal(layers.length, 3);
  assert.match(layers[1].url, /tk=fixture%20key/);
  layers[1].handlers.tileload();
  assert.equal(statuses.at(-1), "天地图影像");
  layers[0].handlers.tileerror();
  assert.equal(statuses.at(-1), "天地图影像", "old tile callbacks must not overwrite the new layer status");
  layers[2].handlers.tileerror();
  assert.match(statuses.at(-1), /瓦片不可用/);
  basemap.setBasemap("satellite", "fixture", "esri");
  assert.match(layers.at(-1).url, /ibasemaps-api.arcgis.com/);
  assert.match(layers.at(-1).options.attribution, /Esri/);
});
