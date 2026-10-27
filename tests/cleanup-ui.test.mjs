import test from "node:test";
import assert from "node:assert/strict";
import { cleanupSelection } from "../web/cleanup-ui.js";

const preview = {
  tasks: [ { id: "a", bytes: 20, error: "" }, { id: "b", bytes: 30, error: "" },
    { id: "blocked", bytes: 90, error: "unsafe path" } ],
  demIds: ["a", "b"], shared: { bytes: 100, error: "" },
};
test("cleanup estimates include shared DEM cache only when all owners are selected", () => {
  assert.deepEqual(cleanupSelection(preview, []), { ids: [], bytes: 0, shared: false });
  assert.deepEqual(cleanupSelection(preview, ["a", "a", "unknown", "blocked"]), { ids: ["a"], bytes: 20, shared: false });
  assert.deepEqual(cleanupSelection(preview, ["a", "b"]), { ids: ["a", "b"], bytes: 150, shared: true });
});
test("unsafe shared DEM cache is excluded from the estimate", () => {
  const unsafe = { ...preview, shared: { bytes: 100, error: "unsafe cache" } };
  assert.equal(cleanupSelection(unsafe, ["a", "b"]).bytes, 50);
});
