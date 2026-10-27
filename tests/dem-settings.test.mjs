import test from "node:test";
import assert from "node:assert/strict";
import { demSettings } from "../web/dem-settings.js";

test("DEM choices preserve each source's actual vertical datum", () => {
  assert.deepEqual(demSettings("NISAR").heights.map(([v]) => v), ["ellipsoid"]);
  assert.deepEqual(demSettings("SRTM").heights.map(([v]) => v), ["ellipsoid", "egm96"]);
  assert.deepEqual(demSettings("COP").heights.map(([v]) => v), ["ellipsoid", "egm2008"]);
  assert.equal(demSettings("NISAR").tiles, false);
  assert.equal(demSettings("SRTM").tiles, false);
  assert.equal(demSettings("COP").tiles, true);
  assert.equal(demSettings("Copernicus GLO-30 Public").source, "COP");
});
