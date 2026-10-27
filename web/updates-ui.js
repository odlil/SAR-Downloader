import { api } from "./api.js";

export function updateSummary(result) {
  if (result.updateAvailable) return result.package
    ? `发现新版本 v${result.latestVersion}，可下载${result.packageLabel}。`
    : `发现新版本 v${result.latestVersion}，此发行版尚无适合当前平台的下载包。`;
  return result.localNewer
    ? `当前版本 v${result.currentVersion} 高于最新正式版 v${result.latestVersion}。`
    : `当前已是最新正式版 v${result.currentVersion}。`;
}

export function createUpdatesUI({ openDialog, request = api, doc = document }) {
  const $ = (id) => doc.getElementById(id);
  let serial = 0, loading = null;
  function resetResult() {
    for (const id of ["update-download", "update-source-download", "update-release-link"]) {
      $(id).hidden = true;
      $(id).removeAttribute("href");
    }
    $("update-notes").textContent = "";
    $("update-details").hidden = true;
    $("update-package").textContent = "";
  }
  function link(id, asset) {
    if (!asset) return;
    $(id).href = asset.url;
    $(id).hidden = false;
  }
  async function load({ opening = false } = {}) {
    const token = ++serial;
    loading?.abort();
    loading = new AbortController();
    if (opening) {
      resetResult();
      $("update-status").textContent = "正在读取更新设置…";
      $("update-check").disabled = true;
    }
    try {
      const result = await request("/api/updates", undefined, { signal: loading.signal });
      if (token !== serial) return;
      $("app-version").textContent = `SAR Downloader v${result.currentVersion}`;
      $("update-current").textContent = `当前版本 v${result.currentVersion}`;
      $("update-repository").value = result.repository;
      $("update-source-settings").open = !result.repository;
      if (opening) $("update-status").textContent = result.repository
        ? "点击“检查更新”获取最新正式发行版。"
        : "尚未配置发布仓库。创建 GitHub 仓库后，填写仓库地址再检查。";
    } catch (error) {
      if (token === serial && opening) $("update-status").textContent = error.message;
    } finally {
      if (token === serial) $("update-check").disabled = false;
    }
  }
  async function check(event) {
    event.preventDefault();
    const token = ++serial;
    loading?.abort();
    loading = new AbortController();
    resetResult();
    $("update-check").disabled = true;
    $("update-repository").disabled = true;
    $("update-status").textContent = "正在检查 GitHub 正式发行版…";
    try {
      const result = await request("/api/updates/check", {
        repository: $("update-repository").value,
      }, { signal: loading.signal });
      if (token !== serial) return;
      $("update-repository").value = result.repository;
      $("update-source-settings").open = false;
      $("update-status").textContent = updateSummary(result);
      $("update-release-link").href = result.releaseUrl;
      $("update-release-link").hidden = false;
      $("update-notes").textContent = result.releaseNotes || "此版本未填写更新说明。";
      $("update-details").hidden = false;
      if (result.updateAvailable) {
        link("update-download", result.package);
        if (result.package) {
          $("update-download").textContent = result.package.kind === "source.zip" ? "下载源码包" : "下载新版";
          $("update-package").textContent = result.package.name + (result.package.size
            ? ` · ${(result.package.size / 1024 / 1024).toFixed(1)} MB` : "");
        }
        if (result.sourcePackage && result.package?.kind !== "source.zip") link("update-source-download", result.sourcePackage);
      }
    } catch (error) {
      if (token === serial) $("update-status").textContent = error.message;
    } finally {
      if (token === serial) {
        $("update-check").disabled = false;
        $("update-repository").disabled = false;
      }
    }
  }
  $("open-updates").addEventListener("click", () => {
    openDialog("updates-dialog");
    load({ opening: true });
  });
  $("update-form").addEventListener("submit", check);
  $("update-repository").addEventListener("input", () => {
    resetResult();
    $("update-status").textContent = "更新来源已修改，请重新检查。";
  });
  $("updates-dialog").addEventListener("close", () => {
    serial++;
    loading?.abort();
    $("update-check").disabled = false;
    $("update-repository").disabled = false;
  });
  // This reads localhost only; GitHub is contacted exclusively on an explicit check.
  load();
}
