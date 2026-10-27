export class ApiError extends Error {
  constructor(message, status = 0, details = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

function requestTimeout(path) {
  if (path === "/api/updates/check") return 25000;
  if (path === "/api/downloads/preflight" || path === "/api/downloads/action")
    return 45000;
  if (path.endsWith("/open") || path.endsWith("/folder")) return 330000;
  if (path === "/api/search") return 60000;
  if (
    path === "/api/account/login" ||
    path.startsWith("/api/cache/unfinished/") ||
    path.includes("/orbits/") ||
    path.includes("/dem/") ||
    path === "/api/downloads/scenes" ||
    path === "/api/nisar/subsets/add" ||
    path.endsWith("/scene-bundle")
  )
    return 120000;
  return 15000;
}

// All localhost requests share status handling, cancellation and bounded waits.
export async function api(
  path,
  payload,
  { signal, timeout = requestTimeout(path) } = {},
) {
  const controller = new AbortController();
  let timedOut = false;
  const abort = () => controller.abort();
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeout);
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, { once: true });
  try {
    const response = await fetch(path, {
      signal: controller.signal,
      ...(payload === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          }),
    });
    if ([404, 501].includes(response.status))
      throw new ApiError(
        "请关闭旧启动窗口并重新运行“启动SAR Downloader.cmd”，以启用新版接口。",
        response.status,
      );
    let result;
    try {
      result = await response.json();
    } catch (error) {
      if (controller.signal.aborted) throw error;
      throw new ApiError(
        "本地服务返回格式异常，请重新启动工具。",
        response.status,
      );
    }
    if (!response.ok || result?.error)
      throw new ApiError(
        typeof result?.error === "string" ? result.error : "本地操作未完成。",
        response.status,
        result,
      );
    return result;
  } catch (error) {
    if (timedOut) throw new ApiError("操作超时，请检查网络或稍后重试。");
    if (signal?.aborted || error instanceof ApiError) throw error;
    throw new ApiError("无法连接本地服务，请重新运行启动器。");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}
