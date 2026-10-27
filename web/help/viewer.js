import { getDocument, GlobalWorkerOptions } from "../vendor/pdfjs/pdf.mjs";

GlobalWorkerOptions.workerSrc = new URL("../vendor/pdfjs/pdf.worker.mjs", import.meta.url).href;
const $ = id => document.getElementById(id);
let pdf, current = 1, generation = 0, activeRender;
const status = $("status"), canvas = $("page-canvas"), pageInput = $("page-number");

async function destinationPage(destination) {
  const dest = typeof destination === "string" ? await pdf.getDestination(destination) : destination;
  if (!Array.isArray(dest) || dest.length === 0) return null;
  return typeof dest[0] === "number" ? dest[0] + 1 : (await pdf.getPageIndex(dest[0])) + 1;
}

async function renderPage(number, scroll = true) {
  const token = ++generation;
  if (activeRender) { activeRender.cancel(); try { await activeRender.promise; } catch {} }
  if (token !== generation) return;
  current = Math.max(1, Math.min(pdf.numPages, Number(number) || 1));
  pageInput.value = String(current);
  $("previous").disabled = current === 1;
  $("next").disabled = current === pdf.numPages;
  status.hidden = false;
  status.textContent = "正在显示第 " + current + " 页…";
  try {
    const page = await pdf.getPage(current);
    if (token !== generation) return;
    const base = page.getViewport({ scale: 1 });
    const padding = innerWidth <= 480 ? 16 : 32;
    const scale = $("zoom").value === "fit" ? Math.max(.2, (document.documentElement.clientWidth - padding) / base.width) : Number($("zoom").value);
    const viewport = page.getViewport({ scale });
    const ratio = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.ceil(viewport.width * ratio);
    canvas.height = Math.ceil(viewport.height * ratio);
    canvas.style.width = viewport.width + "px";
    canvas.style.height = viewport.height + "px";
    canvas.setAttribute("aria-label", "说明书第 " + current + " 页，共 " + pdf.numPages + " 页");
    $("links").replaceChildren();
    activeRender = page.render({ canvasContext: canvas.getContext("2d"), viewport, transform: [ratio, 0, 0, ratio, 0, 0] });
    await activeRender.promise;
    if (token !== generation) return;
    const annotations = await page.getAnnotations({ intent: "display" });
    if (token !== generation) return;
    for (const annotation of annotations) {
      if (annotation.subtype !== "Link" || !annotation.dest) continue;
      const rect = viewport.convertToViewportRectangle(annotation.rect);
      const link = document.createElement("button");
      link.type = "button";
      link.className = "page-link";
      link.setAttribute("aria-label", "跳转到对应章节");
      Object.assign(link.style, { left: Math.min(rect[0],rect[2])+"px", top: Math.min(rect[1],rect[3])+"px", width: Math.abs(rect[2]-rect[0])+"px", height: Math.abs(rect[3]-rect[1])+"px" });
      link.addEventListener("click", async () => {
        try { const target = await destinationPage(annotation.dest); if (target) await renderPage(target); }
        catch { status.hidden = false; status.textContent = "章节跳转失败，请通过上方页码跳转。"; }
      });
      $("links").append(link);
    }
    status.hidden = true;
    document.body.dataset.renderedPage = String(current);
    if (scroll) window.scrollTo(0, 0);
  } catch (error) {
    if (token !== generation || error.name === "RenderingCancelledException") return;
    console.error("Unable to render manual page", error.message);
    status.hidden = false;
    status.textContent = "此页暂时无法显示，请重试或通过上方入口打开 PDF。";
  }
}

$("previous").addEventListener("click", () => renderPage(current - 1));
$("next").addEventListener("click", () => renderPage(current + 1));
pageInput.addEventListener("change", () => renderPage(pageInput.value));
$("zoom").addEventListener("change", () => renderPage(current, false));
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); if (pdf && $("zoom").value === "fit") resizeTimer = setTimeout(() => renderPage(current, false), 120); });
document.addEventListener("keydown", event => {
  if (!pdf || /INPUT|SELECT/.test(event.target.tagName)) return;
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") { event.preventDefault(); renderPage(current + (event.key === "ArrowRight" ? 1 : -1)); }
});

try {
  pdf = await getDocument({ url: new URL("./manual-data", import.meta.url).href, isEvalSupported: false, disableRange: true, disableStream: true }).promise;
  pageInput.max = String(pdf.numPages);
  pageInput.disabled = false;
  $("zoom").disabled = false;
  $("page-count").textContent = "/ " + pdf.numPages;
  await renderPage(1);
} catch (error) {
  console.error("Unable to open bundled manual", error.message);
  status.textContent = "说明书暂时无法显示，请选择弹窗上方的“新窗口打开”或“下载 PDF”。";
}
