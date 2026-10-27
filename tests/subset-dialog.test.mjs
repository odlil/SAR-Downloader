import test from "node:test";
import assert from "node:assert/strict";
import { subsetOptions, subsetBounds } from "../web/subset-dialog.js";

const settings = { frequency: "AB", polarization: "HH,HV", connections: "6", minHeight: "", maxHeight: "", includeDEM: true };
test("subset request uses the confirmed rectangle and includes companion DEM", () => {
  assert.deepEqual(subsetOptions([86, 42.7, 87, 43.3], settings), {
    corners: [[86, 43.3], [87, 43.3], [87, 42.7], [86, 42.7]],
    frequencies: ["A", "B"], polarizations: ["HH", "HV"], connections: 6,
    min_height: "", max_height: "", includeDEM: true,
  });
  assert.equal(subsetOptions([86, 42, 87, 43], { ...settings, includeDEM: false }).includeDEM, false);
});
test("invalid rectangles and reversed heights cannot create subset tasks", () => {
  assert.throws(() => subsetOptions([87, 42, 86, 43], settings), /经纬度/);
  assert.throws(() => subsetOptions([86, NaN, 87, 43], settings), /填写/);
  assert.throws(() => subsetOptions([86, 42, 87, 43], { ...settings, minHeight: "200", maxHeight: "100" }), /高程/);
});

test("two-decimal crop bounds expand outwards and keep small and negative areas", () => {
  assert.deepEqual(subsetBounds([86.795, 43.105, 86.805, 43.115]), [86.79, 43.10, 86.81, 43.12]);
  assert.deepEqual(subsetBounds([-.105, -.015, -.104, -.014]), [-.11, -.02, -.10, -.01]);
  assert.deepEqual(subsetBounds([86.60, 43.10, 86.80, 43.20]), [86.60, 43.10, 86.80, 43.20]);
  assert.deepEqual(subsetOptions([86.795, 43.105, 86.805, 43.115], settings).corners,
    [[86.79, 43.12], [86.81, 43.12], [86.81, 43.10], [86.79, 43.10]]);
});
