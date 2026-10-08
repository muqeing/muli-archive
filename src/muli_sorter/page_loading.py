"""Small first response, independent of the media disk and review snapshot."""

LOADING_PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>素材归档工作台</title>
<style>
body{margin:0;background:#f4f5f7;color:#20252b;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:760px;margin:5vh auto;padding:24px}section{background:white;border:1px solid #dfe3e8;border-radius:12px;padding:24px}
h1{font-size:24px;margin:0 0 16px}p{margin:12px 0}button{font:inherit;padding:10px 16px;border:1px solid #c7ccd3;border-radius:8px;background:#eef1f4;cursor:pointer}
button:disabled{opacity:.5;cursor:wait}.muted{color:#66717d}.error{color:#8d2f23}#load-status{min-height:48px}
.help-link{display:inline-flex;align-items:center;gap:6px;min-height:44px;padding:0 10px;border:1px solid #c7ccd3;border-radius:8px;color:#1c5f4a;text-decoration:none;font-size:14px;margin-bottom:12px}.help-link:focus-visible{outline:3px solid #1c5f4a;outline-offset:3px}.help-header{display:flex;justify-content:space-between;align-items:start;gap:12px;flex-wrap:wrap}
</style></head><body><main><section>
<div class="help-header"><h1>素材归档工作台</h1><a class="help-link" href="/help/" target="_blank" rel="noopener" aria-label="使用帮助（新标签页）">ⓘ 使用帮助</a></div>
<p id="load-status" role="status" aria-live="polite">正在读取待处理素材…</p>
<p class="muted">待处理素材加载完成后即可确认项目；已归档历史可按需展开查看。</p>
<button id="load-retry" type="button" hidden>重新加载</button>
<noscript><p class="error">请允许浏览器运行 JavaScript，再重新打开此页面。</p></noscript>
</section></main><script>
(function () {
  "use strict";
  var busy = false, status = document.getElementById("load-status"), retry = document.getElementById("load-retry");
  async function load() {
    if (busy) return;
    busy = true; retry.hidden = true; status.className = "";
    status.textContent = "正在读取待处理素材…";
    var started = Date.now(), phase = "正在读取待处理素材…";
    var ticker = window.setInterval(function () {
      status.textContent = phase + " 已等待 " + Math.floor((Date.now() - started) / 1000) + " 秒";
    }, 1000);
    try {
      var html = null, interruptions = 0;
      while (Date.now() - started < 180000) {
        var controller = new AbortController();
        var deadline = window.setTimeout(function () { controller.abort(); }, 15000);
        var response;
        try {
          response = await fetch("/api/review-page", {cache: "no-store", credentials: "same-origin", signal: controller.signal});
          if (response.status === 202) {
            var progress = await response.json();
            phase = String(progress.phase || "正在准备待处理页面…");
            interruptions = 0;
          } else if ([408, 429, 502, 503, 504].indexOf(response.status) >= 0) {
            throw new Error("transient");
          } else {
            if (response.status === 401 || response.status === 403) throw new Error("access");
            if (!response.ok || (response.headers.get("Content-Type") || "").indexOf("text/html") !== 0) throw new Error("unavailable");
            html = await response.text();
          }
        } catch (error) {
          // Retry only this read-only page request. The cache still has one
          // builder; no archive, backup, or preflight operation is replayed.
          if (error.name !== "AbortError" && error.name !== "TypeError" && error.message !== "transient") throw error;
          interruptions += 1;
          phase = "读取暂时中断，正在继续连接 NAS…";
        } finally { window.clearTimeout(deadline); }
        if (html !== null) break;
        // Subscribe to the same background build; never starts a second scan.
        await new Promise(function (resolve) { window.setTimeout(resolve, Math.min(10000, 2000 * Math.max(1, interruptions))); });
      }
      if (html === null) throw new Error("waiting");
      if (html.indexOf('id="review-model"') < 0) throw new Error("unavailable");
      window.clearTimeout(deadline); window.clearInterval(ticker);
      status.textContent = "数据已读取，正在显示拍摄段…";
      // Yield a paint before parsing the full, same-origin review document.
      await new Promise(function (resolve) { requestAnimationFrame(function () { requestAnimationFrame(resolve); }); });
      // Keeps the original HTTP URL, origin, and browser draft keys. No iframe.
      document.open(); document.write(html); document.close();
    } catch (error) {
      window.clearTimeout(deadline); window.clearInterval(ticker);
      status.className = "error";
      status.textContent = error.name === "AbortError" ?
        "网络读取超时，请检查连接后重新加载；归档任务状态不会因此改变。" :
        error.message === "access" ? "当前页面访问被拒绝。请从正式归档入口打开；不会自动绕过访问保护。" :
        error.message === "waiting" ? "页面准备或网络恢复时间较长。可稍后重新加载继续查看；后台归档不受影响。" :
        "暂时无法读取页面数据。请确认 NAS 和局域网连接后重新加载；归档任务状态不会因此改变。";
      retry.hidden = false;
    } finally { busy = false; }
  }
  retry.addEventListener("click", load);
  requestAnimationFrame(function () { requestAnimationFrame(load); });
})();
</script></body></html>'''


def loading_page(ingest_url=None):
    if not ingest_url:
        return LOADING_PAGE
    from html import escape
    from .workflow_status import service_origin
    url = escape(service_origin(ingest_url) + '/', quote=True)
    return LOADING_PAGE.replace('<h1>素材归档工作台</h1>',
        '<h1>素材归档工作台</h1><p><a id="workflow-back-loading" href="' + url + '">返回拷贝备份</a></p>') + r'''<script>(function(){var a=document.getElementById('workflow-back-loading'), b=new URL(location.href).searchParams.get('batch_id');if(b&&/^BATCH_\d{8}_\d{6,}$/.test(b)){var u=new URL(a.href);u.searchParams.set('batch_id',b);a.href=u.href;}})();</script>'''
