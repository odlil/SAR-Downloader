import { api } from "./api.js";
import { escapeHtml as escape } from "./core.js";

const $ = (id) => document.getElementById(id);

export function cleanupSelection(preview, ids) {
  const selected = new Set(ids);
  const rows = preview.tasks.filter((task) => selected.has(task.id) && !task.error);
  const shared = preview.demIds.length > 0 &&
    preview.demIds.every((id) => rows.some((task) => task.id === id)) && !preview.shared.error;
  return { ids: rows.map((task) => task.id), shared,
    bytes: rows.reduce((sum, task) => sum + task.bytes, 0) + (shared ? preview.shared.bytes : 0) };
}

export function createCleanupUI({ apply, toast, bytes, getTasks, onBusy }) {
  const dialog = $("cleanup-dialog"), form = $("cleanup-form");
  let preview, busy = false, requestedIds, remove = false;
  function setBusy(value) {
    busy = value;
    dialog.dataset.busy = String(value);
    $("cleanup-fields").disabled = value;
    $("cleanup-close").disabled = value;
    $("cleanup-cancel").disabled = value;
    $("cleanup-refresh").disabled = value;
    onBusy(value);
    update();
  }
  function selection() {
    return cleanupSelection(preview, Array.from(
      form.querySelectorAll("input[data-clean-id]:checked"), (input) => input.dataset.cleanId));
  }
  function update() {
    if (!preview) return;
    const selected = selection();
    $("cleanup-summary").textContent = `已选 ${selected.ids.length} 个任务 · 预计释放 ${bytes(selected.bytes)}${selected.shared ? "（含共享 DEM 缓存）" : ""}`;
    $("cleanup-shared").textContent = preview.shared.error || (selected.shared
      ? "已选中全部未完成 DEM 任务，共享瓦片及未完成网格下载一并清理；完整高程转换网格保留。"
      : "共享 DEM 缓存仅在全部未完成 DEM 任务都选中时清理。完整高程转换网格始终保留。");
    $("cleanup-submit").disabled = busy || !selected.ids.length || !$("cleanup-confirm").checked;
    $("cleanup-submit").textContent = busy ? "正在清理…" : $("cleanup-remove").checked ? "清理并移除记录" : "清理所选任务";
  }
  async function load() {
    if (busy) return;
    if (getTasks().some((task) => task.busy))
      return toast("请先暂停全部，等待下载和处理完全停止后再清理。", true);
    setBusy(true);
    try {
      preview = await api("/api/cache/unfinished/preview", requestedIds ? { ids: requestedIds } : {});
      $("cleanup-list").innerHTML = preview.tasks.map((task) => `
        <article class="cleanup-task">
          <label><input type="checkbox" data-clean-id="${escape(task.id)}" ${task.error ? "disabled" : requestedIds ? "checked" : ""} />
            <span><strong>${escape(task.title)}</strong><small>${bytes(task.bytes)} · ${task.files} 个临时文件</small></span></label>
          ${task.error ? `<p class="task-error">${escape(task.error)}</p>` : `<details><summary>查看临时文件位置</summary><ul>${task.paths.map((path) => `<li>${escape(path)}</li>`).join("") || "<li>任务尚未生成临时文件。</li>"}</ul></details>`}
        </article>`).join("") || '<p class="field-help">没有未完成的任务。</p>';
      $("cleanup-confirm").checked = false;
      $("cleanup-remove").checked = remove;
      $("cleanup-error").hidden = true;
      if (!dialog.open) dialog.showModal();
    } catch (error) {
      if (dialog.open) {
        $("cleanup-error").textContent = error.message;
        $("cleanup-error").hidden = false;
      } else toast(error.message, true);
    } finally { setBusy(false); }
  }
  form.addEventListener("change", update);
  $("cleanup-refresh").addEventListener("click", load);
  dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !preview || !$("cleanup-confirm").checked) return;
    const selected = selection();
    if (!selected.ids.length) return;
    setBusy(true);
    try {
      const result = await api("/api/cache/unfinished/clear", {
        token: preview.token, ids: selected.ids, removeTasks: $("cleanup-remove").checked,
      });
      apply(result);
      const info = result.cleanup;
      const failure = info.failedIds.length ? "部分任务未能完全清理，记录已保留。"
        : info.skipped ? "部分共享缓存未能清理，可稍后重试清理闲置缓存。" : "";
      const message = `已清理 ${info.filesRemoved} 个临时文件，释放 ${bytes(info.bytesFreed)}。${info.removedCount ? `已移除 ${info.removedCount} 条任务记录。` : "任务记录保留，继续时重新下载。"}${failure}`;
      $("cache-status").textContent = message;
      dialog.close();
      toast(message, Boolean(info.skipped));
    } catch (error) {
      // Tokens are single-use. Require a fresh preview after any failed attempt.
      $("cleanup-confirm").checked = false;
      $("cleanup-error").textContent = `${error.message} 请刷新预览后重试。`;
      $("cleanup-error").hidden = false;
      preview = undefined;
      $("cleanup-submit").disabled = true;
      $("cleanup-submit").textContent = "请刷新预览";
    } finally { setBusy(false); }
  });
  return {
    open(ids, removeTasks = false) {
      if (busy) return;
      requestedIds = ids;
      remove = removeTasks;
      return load();
    },
  };
}
