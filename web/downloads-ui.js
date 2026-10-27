import {
  queueRows,
  createTaskListRenderer,
  pollingDelay,
} from "./queue-view.js";
import { escapeHtml as escape } from "./core.js";
import { api } from "./api.js";
import { createPreflightUI } from "./preflight-ui.js";
import { createCleanupUI } from "./cleanup-ui.js";
const $ = (id) => document.getElementById(id);

const ACTIVE = ["downloading", "waiting", "retrying", "processing"];
const labels = {
  queued: "待启动",
  downloading: "下载中",
  waiting: "等待生成",
  retrying: "重试中",
  processing: "处理中",
  paused: "已暂停",
  completed: "已完成",
  error: "失败",
  auth_required: "需要登录",
};
function bytes(value) {
  if (!Number.isFinite(value)) return "大小未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (value >= 1024 && unit < 4) {
    value /= 1024;
    unit++;
  }
  return `${value.toFixed(unit ? 2 : 0)} ${units[unit]}`;
}

export function createDownloadsUI({
  toast,
  openAccount,
  getPlans,
  setPlans,
  initialFilter = "all",
  onFilter = () => {},
}) {
  let tasks = [],
    auth = { loggedIn: false },
    filter = initialFilter,
    kind = $("task-kind").value,
    connected = false,
    authBusy = false,
    refreshing = false,
    cleaning = false;
  let settings = { concurrent: 2, output: "" };
  const renderRows = createTaskListRenderer($("task-list"));
  let revision = 0;
  const tools = createPreflightUI({
    getTasks: () => tasks,
    toast,
  });
  const cleanup = createCleanupUI({ apply, toast, bytes, getTasks: () => tasks,
    onBusy(value) { cleaning = value; render(); },
  });
  function account() {
    $("earthdata-badge").textContent = auth.loggedIn ? "已登录" : "未登录";
    $("earthdata-fields").hidden = auth.loggedIn;
    $("earthdata-login").hidden = auth.loggedIn;
    $("earthdata-login").disabled = authBusy;
    $("earthdata-login").textContent = authBusy ? "验证中…" : "登录";
    $("earthdata-logout").hidden = !auth.loggedIn;
    $("earthdata-logout").disabled = authBusy;
    if (!authBusy) {
      $("earthdata-status").textContent = auth.loggedIn
        ? `已登录：${auth.username}${auth.burstReady ? " · Bursts 已授权" : " · Bursts 授权尚未完成"}`
        : "";
      $("earthdata-status").hidden = !auth.loggedIn;
    }
    $("earthdata-status").classList.remove("is-error");
    $("download-login").hidden = auth.loggedIn;
  }
  function apply(result, fromPoll = false) {
    if (!fromPoll) revision++;
    const changed =
      auth.loggedIn !== result.auth.loggedIn ||
      auth.username !== result.auth.username ||
      auth.burstReady !== result.auth.burstReady;
    tasks = result.tasks;
    auth = result.auth;
    connected = true;
    settings = result.settings;
    if (changed) account();
    render();
  }
  function render() {
    const all = queueRows(tasks, getPlans());
    const hasBusy = tasks.some((task) => task.busy);
    $("queue-count").textContent = all.length;
    $("task-total").textContent = all.length;
    const visible = all
      .filter(
        (task) =>
          kind === "all" ||
          (kind === "dem"
            ? ["dem", "dem-process"].includes(task.kind)
            : task.kind === kind),
      )
      .filter(
        (task) =>
          filter === "all" ||
          (filter === "active"
            ? ACTIVE.includes(task.state)
            : filter === "error"
              ? ["error", "auth_required"].includes(task.state)
              : task.state === filter),
      );
    renderRows(visible, (task) => {
      if (task.legacyPlan)
        return `<article class="task-card"><div class="task-main"><div class="task-title">${escape(task.title)}</div><div class="task-description">旧版${task.kind === "dem" ? " DEM" : "轨道"}规划；请在对应页面重新检查并加入真实任务</div></div><div class="task-right"><span class="badge badge-neutral">旧规划</span><button class="text-button" data-remove-plan="${escape(task.id)}">移除</button></div></article>`;
      const processing = task.kind === "dem-process",
        subset = task.kind === "nisar-subset";
      const percent = processing
        ? Math.min(100, (task.progress || 0) * 100)
        : task.total
          ? Math.min(100, (task.completed / task.total) * 100)
          : null;
      const toggle = ["queued", ...ACTIVE].includes(task.state)
        ? "pause"
        : ["paused", "error", "auth_required"].includes(task.state)
          ? "resume"
          : null;
      return `<article class="task-card" data-download-id="${escape(task.id)}"><div class="task-main"><div class="task-title" title="${escape(task.filename)}">${escape(task.title)}</div><div class="task-description">${processing ? escape(task.phase || "待处理") : `${bytes(task.completed)} / ${bytes(task.total)}`}${percent !== null ? ` · ${percent.toFixed(1)}%` : ""}${task.speed ? ` · ${bytes(task.speed)}/s` : ""}${subset ? ` · ${escape(task.phase)}${task.prepTotal ? ` · ${task.prepCurrent}/${task.prepTotal}` : ""}${task.networkBytes ? ` · 网络 ${bytes(task.networkBytes)}` : ""}` : ""}</div><progress class="download-progress" max="100" ${percent !== null ? `value="${percent}"` : task.state === "completed" ? 'value="100"' : 'value="0"'} aria-label="${escape(task.title)} 任务进度"></progress>${task.error ? `<p class="task-error">${escape(task.error)}</p>` : ""}${task.cleanupWarning ? `<p class="task-cleanup-note">${escape(task.cleanupWarning)}</p>` : ""}${task.target ? `<p class="task-path" title="${escape(task.target)}">${escape(task.target)}</p>` : ""}</div><div class="task-right"><span class="badge ${["error", "auth_required"].includes(task.state) ? "badge-amber" : "badge-neutral"}">${task.busy && task.state === "paused" ? "正在暂停…" : labels[task.state] || task.state}</span>${toggle ? `<button class="text-button" data-task-action="${toggle}" data-id="${escape(task.id)}" ${task.busy && task.state === "paused" ? "disabled" : ""}>${toggle === "pause" ? "暂停" : task.state === "error" ? "重试" : "继续"}</button>` : ""}<button class="text-button" data-task-action="remove" data-id="${escape(task.id)}" ${task.busy ? "disabled" : ""}>移除</button>${task.state !== "completed" ? `<button class="text-button task-clean-action" data-clean-task="${escape(task.id)}" ${hasBusy || cleaning ? "disabled" : ""}>移除并清理未完成数据</button>` : ""}</div></article>`;
    });
    $("queue-empty").hidden = visible.length > 0;
    $("queue-empty").querySelector("h3").textContent = all.length
      ? "此分类下暂无任务"
      : "队列还是空的";
    $("pause-tasks").disabled = !tasks.some((task) =>
      ["queued", ...ACTIVE].includes(task.state),
    );
    $("resume-tasks").disabled = !tasks.some(
      (task) =>
        ["paused", "error", "auth_required"].includes(task.state) && !task.busy,
    );
    $("start-tasks").disabled = !tasks.some((task) => task.state === "queued");
    $("remove-completed-tasks").disabled = !tasks.some(
      (task) => task.state === "completed" && !task.busy,
    );
    $("remove-all-tasks").disabled = !all.length || hasBusy;
    $("remove-all-tasks").title = hasBusy
      ? "请先暂停全部，等待传输停止后再移除"
      : "移除全部任务记录，保留数据文件和断点";
    $("clear-cache").disabled = !connected || cleaning || hasBusy;
    $("clear-cache").textContent = "清理闲置缓存";
    $("clear-unfinished").disabled = !connected || cleaning || hasBusy || !tasks.some((task) => task.state !== "completed");
    $("engine-status").textContent = connected
      ? "下载续传 · DEM 处理"
      : "服务未连接";
    $("engine-badge").textContent = connected ? "已就绪" : "未连接";
    $("download-banner-text").textContent = !connected
      ? "本地下载服务未连接，请重新运行启动器。"
      : !settings.output
        ? "请选择影像目录并保存下载设置。"
        : auth.loggedIn ? `Earthdata · ${auth.username}` : "Earthdata 未登录 · 公共 CopDEM 可直接下载";
  }
  async function action(action, ids) {
    try {
      if (["start", "resume", "retry"].includes(action)) {
        const report = await tools.check(action, ids);
        if (!report.taskCount) return toast("当前范围没有可启动的任务。");
        if (!report.canStart)
          return toast("下载前检查未通过，请处理检查结果后重试。", true);
      }
      apply(await api("/api/downloads/action", { action, ids }));
    } catch (error) {
      if (error.details?.preflight) tools.showReport(error.details.preflight);
      toast(error.message, true);
    }
  }
  async function refresh(initial = false) {
    if (refreshing) return;
    refreshing = true;
    const requestedRevision = revision;
    try {
      const result = await api("/api/downloads");
      if (requestedRevision !== revision) return;
      apply(result, true);
      if (initial) {
        $("concurrent").value = settings.concurrent;
        $("output-folder").value = settings.output;
        if (result.warning) toast(result.warning, true);
        const old = getPlans().filter(
          (task) => task.kind === "sar" && !task.demo && task.metadata?.url,
        );
        if (old.length) {
          apply(
            await api("/api/downloads/add", {
              tasks: old.map((task) => ({
                url: task.metadata.url,
                filename: task.metadata.filename,
                title: task.title,
              })),
            }),
          );
          setPlans(getPlans().filter((task) => !old.includes(task)));
          render();
        }
      }
    } catch (error) {
      connected = false;
      render();
      if (initial) toast(error.message, true);
    } finally {
      refreshing = false;
    }
  }
  $("earthdata-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (authBusy) return;
    authBusy = true;
    account();
    $("earthdata-status").textContent = "正在验证 Earthdata 账号及 ASF 授权…";
    $("earthdata-status").hidden = false;
    const payload = {
      username: $("earthdata-username").value.trim(),
      password: $("earthdata-password").value,
    };
    $("earthdata-password").value = "";
    let failure = "";
    try {
      auth = await api("/api/account/login", payload);
      toast(auth.message, !auth.burstReady);
    } catch (error) {
      failure = error.message;
    } finally {
      payload.password = "";
      authBusy = false;
      account();
      if (failure) {
        $("earthdata-status").textContent = failure;
        $("earthdata-status").hidden = false;
        $("earthdata-status").classList.add("is-error");
      }
      render();
    }
  });
  $("earthdata-logout").addEventListener("click", async () => {
    if (authBusy) return;
    authBusy = true;
    $("earthdata-logout").disabled = true;
    try {
      auth = await api("/api/account/logout", {});
      toast("已退出登录，未完成的下载已暂停。");
    } catch (error) {
      toast(error.message, true);
    } finally {
      authBusy = false;
      account();
      await refresh();
    }
  });
  $("account-dialog").addEventListener("close", () => {
    $("earthdata-password").value = "";
  });
  $("download-login").addEventListener("click", openAccount);
  $("pick-download-folder").addEventListener("click", async () => {
    const button = $("pick-download-folder");
    button.disabled = true;
    try {
      const result = await api("/api/downloads/folder", {});
      if (result.output) $("output-folder").value = result.output;
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("save-download-settings").addEventListener("click", async () => {
    try {
      apply(
        await api("/api/downloads/settings", {
          concurrent: Number($("concurrent").value),
          output: $("output-folder").value.trim(),
        }),
      );
      toast("下载设置已保存；已启动的文件继续使用原目录。");
    } catch (error) {
      toast(error.message, true);
    }
  });
  $("task-list").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.cleanTask) return cleanup.open([button.dataset.cleanTask], true);
    if (button.dataset.removePlan) {
      setPlans(
        getPlans().filter((task) => task.id !== button.dataset.removePlan),
      );
      render();
    }
    if (button.dataset.taskAction)
      action(button.dataset.taskAction, [button.dataset.id]);
  });
  document.querySelectorAll("[data-task-filter]").forEach((button) =>
    button.addEventListener("click", () => {
      filter = button.dataset.taskFilter;
      onFilter(filter);
      document.querySelectorAll("[data-task-filter]").forEach((other) => {
        const active = other === button;
        other.classList.toggle("active", active);
        other.setAttribute("aria-pressed", active);
      });
      render();
    }),
  );
  document.querySelectorAll("[data-task-filter]").forEach((button) => {
    const active = button.dataset.taskFilter === filter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active);
  });
  $("task-kind").addEventListener("change", () => {
    kind = $("task-kind").value;
    render();
  });
  $("pause-tasks").addEventListener("click", () => action("pause"));
  $("resume-tasks").addEventListener("click", () =>
    action(
      "resume",
      tasks
        .filter((t) => ["paused", "error", "auth_required"].includes(t.state))
        .map((t) => t.id),
    ),
  );
  $("start-tasks").addEventListener("click", () => action("start"));
  async function removeTasks(completedOnly) {
    const selected = tasks.filter((task) =>
      completedOnly ? task.state === "completed" && !task.busy : true,
    );
    if (selected.some((task) => task.busy))
      return toast("请先暂停全部，等待传输停止后再移除。", true);
    try {
      if (selected.length)
        apply(
          await api("/api/downloads/action", {
            action: "remove",
            ids: selected.map((task) => task.id),
          }),
        );
      const legacy = completedOnly ? [] : getPlans();
      const legacyCount = legacy.length;
      if (!completedOnly)
        setPlans(getPlans().filter((task) => !legacy.includes(task)));
      render();
      toast(
        `已移除 ${selected.length + legacyCount} 条任务记录，数据文件和断点保留。`,
      );
    } catch (error) {
      toast(error.message, true);
    }
  }
  $("remove-completed-tasks").addEventListener("click", () =>
    removeTasks(true),
  );
  $("remove-all-tasks").addEventListener("click", () => removeTasks(false));
  $("clear-unfinished").addEventListener("click", () => cleanup.open());
  $("clear-cache").addEventListener("click", async () => {
    if (cleaning) return;
    cleaning = true;
    render();
    try {
      const result = await api("/api/cache/clear", {});
      apply(result);
      const info = result.cleanup;
      const message = `已清理 ${info.filesRemoved} 个临时文件，释放 ${bytes(info.bytesFreed)}。${info.demCachePreserved ? "未完成 DEM 任务所需瓦片已保留。" : ""}正式数据、未完成任务断点和高程转换网格保留。${info.skipped ? "部分文件无法清理，请检查文件占用后重试。" : ""}`;
      $("cache-status").textContent = message;
      toast(
        `缓存清理完成，释放 ${bytes(info.bytesFreed)}。${info.skipped ? "部分文件未能清理。" : ""}`,
        Boolean(info.skipped),
      );
    } catch (error) {
      $("cache-status").textContent = error.message;
      toast(error.message, true);
    } finally {
      cleaning = false;
      render();
    }
  });
  let timer,
    stopped = false;
  function schedulePoll() {
    clearTimeout(timer);
    if (!stopped)
      timer = setTimeout(poll, pollingDelay(tasks, document.hidden));
  }
  async function poll() {
    await refresh();
    schedulePoll();
  }
  refresh(true).finally(schedulePoll);
  window.addEventListener("pagehide", () => {
    stopped = true;
    clearTimeout(timer);
  });
  window.addEventListener("pageshow", () => {
    if (stopped) {
      stopped = false;
      poll();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !stopped) {
      clearTimeout(timer);
      poll();
    }
  });
  account();
  render();
  async function enqueue(
    endpoint,
    payload,
    success,
    duplicate = "所选文件已在队列中。",
  ) {
    try {
      const result = await api(endpoint, payload);
      apply(result);
      toast(
        result.added
          ? success ||
              `已加入 ${result.added} 个文件；在下载任务中点击开始下载。`
          : duplicate,
      );
    } catch (error) {
      toast(error.message, true);
    }
  }
  function addAssets(assets) {
    if (!assets.length) return toast("所选记录没有可下载文件。", true);
    return enqueue("/api/downloads/add", { tasks: assets });
  }
  return {
    render,
    apply,
    getTasks: () => tasks,
    getSettings: () => settings,
    addAssets,
    addDem: (plan) =>
      enqueue(
        "/api/dem/add",
        plan,
        "DEM 处理任务已加入；在下载任务中选择目录并开始。",
        "该 DEM 处理任务已在队列中。",
      ),
  };
}
