(function () {
  'use strict';
  if (!/^https?:$/.test(location.protocol)) return;
  var panel = document.getElementById('discovery-panel'), list = document.getElementById('discovery-batches');
  var message = document.getElementById('discovery-message'), reload = document.getElementById('discovery-reload');
  if (!panel || !list || !message || !reload) return;
  var settings = JSON.parse(document.getElementById('review-settings').textContent), initial = settings.discovery_state || {};
  var baseline = initial.base_report_id || '', changed = false, stopped = false;
  function show(data) {
    if (!data || data.schema !== 'discovery-status/1' || !Array.isArray(data.batches)) return;
    if (!baseline) baseline = data.base_report_id || '';
    changed = changed || !!(baseline && data.base_report_id && baseline !== data.base_report_id);
    window.dispatchEvent(new CustomEvent("muli-discovery-status", {detail: data}));
    list.replaceChildren();
    data.batches.forEach(function (batch) {
      var p = document.createElement('p'), v = batch.verification || {}, label;
      if (v.state === 'verifying') label = '正在内容校验 · ' + (v.verified_files || 0) + '/' + (v.total_files || 0) + ' 个文件 · 已读取 ' + ((v.read_bytes || 0)/1e9).toFixed(2) + ' GB';
      else if (v.state === 'manual_archive_verified') label = '已核验手工归档 · ' + (v.verified_files || 0) + ' 个原片 · ' + String(v.project || '已有订单') + '；无需重复归档';
      else if (v.state === 'completed') label = '内容校验通过，正在整理拍摄段';
      else if (v.state === 'waiting_lock') label = '正在等待移动清理结束，再开始内容校验';
      else if (['failed', 'blocked', 'interrupted', 'unavailable'].indexOf(v.state) >= 0) label = '需要核对：' + (v.error || '独立校验未完成；原文件保留');
      else label = ({waiting_completion:'正在拷贝或等待备份完成', verification_required:'已拷贝，等待完整内容校验', queued:'正在排队整理拍摄段', processing:'正在整理拍摄段', retry_wait:'整理暂未完成，需要查看提示'})[batch.state] || '等待检查';
      p.textContent = String(batch.batch_id || '') + ' · ' + label;
      p.style.overflowWrap = 'anywhere'; list.appendChild(p);
    });
    message.textContent = data.source_ok === false ? '暂时无法读取备份状态；当前选择保持。' : changed ? '发现新的待归档素材。点击下方按钮保存草稿并查看。' : '';
    reload.hidden = !changed; panel.hidden = !data.batches.length && !changed && data.source_ok !== false;
  }
  reload.addEventListener('click', function () {
    if (!window.muliReview || window.muliReview.saveDraft() !== true) { message.textContent = '草稿保存未通过，已保留当前页面。'; return; }
    location.reload();
  });
  async function poll() {
    if (stopped) return;
    if (!document.hidden) {
      var controller = new AbortController(), timeout = setTimeout(function () { controller.abort(); }, 5000);
      try {
        var response = await fetch('/api/discovery-status', {cache:'no-store', credentials:'same-origin', signal:controller.signal});
        if (!response.ok) throw new Error('unavailable');
        show(await response.json());
      } catch (error) { panel.hidden = false; message.textContent = '新批次进度暂时不可用；当前选择和归档任务保持。'; }
      finally { clearTimeout(timeout); }
    }
    if (!stopped) setTimeout(poll, 5000);
  }
  window.addEventListener('pagehide', function () { stopped = true; });
  show(initial); setTimeout(poll, 5000);
})();
