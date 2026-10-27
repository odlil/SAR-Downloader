import { api } from "./api.js";
import { formatSize, escapeHtml as escape } from "./core.js";

export function createPreflightUI({ getTasks, toast }) {
  const $ = (id) => document.getElementById(id);
  function showReport(report) {
    $("preflight-report").hidden = false;
    $("preflight-summary").textContent = report.taskCount
      ? `${new Date().toLocaleTimeString()} · 检查 ${report.taskCount} 个任务，可运行 ${report.readyCount} 个；已知剩余下载量 ${formatSize(report.knownBytes)}${report.unknownCount ? `，另有 ${report.unknownCount} 个任务体积未知` : ""}。`
      : "当前范围没有待检查的任务。";
    const registry = new Map(getTasks().map((task) => [task.id, task.title]));
    const grouped = new Map();
    for (const item of report.checks) {
      const key = item.level + item.message;
      const existing = grouped.get(key);
      if (existing) existing.taskIds.push(...item.taskIds);
      else grouped.set(key, { ...item, taskIds: [...item.taskIds] });
    }
    $("preflight-checks").innerHTML =
      [...grouped.values()]
        .map((item) => {
          const names = [...new Set(item.taskIds)].map(
            (id) => registry.get(id) || id,
          );
          return `<li class="check-${escape(item.level)}"><strong>${item.level === "error" ? "需处理" : "提示"}</strong> ${escape(item.message)}${names.length ? `<small>${escape(names.slice(0, 3).join("；"))}${names.length > 3 ? ` 等 ${names.length} 个任务` : ""}</small>` : ""}</li>`;
        })
        .join("") ||
      '<li class="check-ok">本机检查通过；源站权限和资源可用性将在传输时确认。</li>';
    $("preflight-disks").textContent = report.destinations
      .map(
        (item) =>
          `${item.path} · 可用 ${formatSize(item.freeBytes)} · 已知剩余 ${formatSize(item.knownBytes)}`,
      )
      .join("\n");
  }
  async function check(action = "resume", identifiers) {
    const report = await api("/api/downloads/preflight", {
      action,
      ids: identifiers,
    });
    showReport(report);
    return report;
  }
  $("preflight-button").addEventListener("click", async () => {
    const button = $("preflight-button");
    button.disabled = true;
    try {
      await check();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  return { check, showReport };
}
