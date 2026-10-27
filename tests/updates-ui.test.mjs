import test from "node:test";
import assert from "node:assert/strict";
import { createUpdatesUI, updateSummary } from "../web/updates-ui.js";

const settings = { repository: "publisher/SAR-Downloader", currentVersion: "1.0" };
const newer = { ...settings, latestVersion: "1.1", updateAvailable: true, packageLabel: "Windows x64 免安装包",
  releaseUrl: "https://github.com/publisher/SAR-Downloader/releases/tag/v1.1",
  releaseNotes: "<script>alert('plain text only')</script>",
  package: { name: "SAR-v1.1-windows-x64.zip", kind: "windows-x64.zip", size: 1024,
    url: "https://github.com/publisher/SAR-Downloader/releases/download/v1.1/SAR-v1.1-windows-x64.zip" } };

function dom() {
  const elements = new Map();
  return { getElementById(id) {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", hidden: false, disabled: false, events: {},
      addEventListener(name, fn) { this.events[name] = fn; },
      removeAttribute(name) { delete this[name]; },
    });
    return elements.get(id);
  }};
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("Update summaries distinguish new, equal, development and unavailable packages", () => {
  assert.match(updateSummary(newer), /发现新版本/);
  assert.match(updateSummary({ ...newer, package: null }), /尚无适合/);
  assert.match(updateSummary({ ...newer, updateAvailable: false }), /已是最新/);
  assert.match(updateSummary({ ...newer, updateAvailable: false, localNewer: true }), /高于最新/);
});

test("Startup reads localhost settings only; checks render notes as plain text and select the download", async () => {
  const doc = dom(), calls = [];
  createUpdatesUI({ doc, openDialog() {}, request: async (path, body) => {
    calls.push([path, body]); return path.endsWith("/check") ? newer : settings;
  }});
  await tick();
  assert.deepEqual(calls.map(([path]) => path), ["/api/updates"]);
  assert.equal(doc.getElementById("app-version").textContent, "SAR Downloader v1.0");
  await doc.getElementById("update-form").events.submit({ preventDefault() {} });
  assert.deepEqual(calls[1], ["/api/updates/check", { repository: settings.repository }]);
  assert.equal(doc.getElementById("update-notes").textContent, newer.releaseNotes);
  assert.equal(doc.getElementById("update-download").href, newer.package.url);
  assert.equal(doc.getElementById("update-download").hidden, false);
  doc.getElementById("update-repository").events.input();
  assert.equal(doc.getElementById("update-download").hidden, true);
  assert.equal(doc.getElementById("update-download").href, undefined);
});

test("A failed recheck clears earlier download links and allows retry", async () => {
  const doc = dom(); let fail = false;
  createUpdatesUI({ doc, openDialog() {}, request: async (path) => {
    if (!path.endsWith("/check")) return settings;
    if (fail) throw new Error("GitHub unavailable");
    return newer;
  }});
  await tick();
  await doc.getElementById("update-form").events.submit({ preventDefault() {} });
  fail = true;
  await doc.getElementById("update-form").events.submit({ preventDefault() {} });
  assert.equal(doc.getElementById("update-download").hidden, true);
  assert.equal(doc.getElementById("update-status").textContent, "GitHub unavailable");
  assert.equal(doc.getElementById("update-check").disabled, false);
});

test("Closing the dialog prevents an old network response from reviving stale links", async () => {
  const doc = dom(); let finish;
  createUpdatesUI({ doc, openDialog() {}, request: async (path) =>
    path.endsWith("/check") ? new Promise((resolve) => { finish = resolve; }) : settings });
  await tick();
  const checking = doc.getElementById("update-form").events.submit({ preventDefault() {} });
  doc.getElementById("updates-dialog").events.close();
  finish(newer); await checking;
  assert.equal(doc.getElementById("update-download").hidden, true);
  assert.equal(doc.getElementById("update-repository").disabled, false);
});
