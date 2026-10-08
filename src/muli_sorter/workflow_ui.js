(function () {
  'use strict';
  var settings = JSON.parse(document.getElementById('review-settings').textContent);
  var workflow = settings.workflow_state;
  if (!workflow || !workflow.ingest_url || !window.muliReview) return;
  var panel = document.getElementById('workflow-panel'), select = document.getElementById('workflow-batch');
  var back = document.getElementById('workflow-back'), progress = document.getElementById('workflow-progress');
  var phases = {copying:'正在拷贝，完成后交接', verifying:'正在核对内容，校验通过后交接', organizing:'正在整理，完成后进入待归档', awaiting_confirmation:'待确认项目，可进入归档', archived:'当前清单已归档', copied_unverified:'已复制，来源保留（未完整校验）', manual_archived:'已核验手工归档', needs_attention:'需要处理', no_pending_shoot:'没有待归档的拍摄素材', unknown:'交接状态暂不可确认'};
  var rows = Object.create(null);
  (workflow.batches || []).forEach(function (row) { rows[row.batch_id] = row; var option = document.createElement('option'); option.value = row.batch_id; option.textContent = row.batch_id; select.appendChild(option); });
  var requested = new URL(window.location.href).searchParams.get('batch_id') || '';
  if (requested && /^BATCH_\d{8}_\d{6,}$/.test(requested) && !rows[requested]) {
    var option = document.createElement('option'); option.value = requested; option.textContent = requested; select.appendChild(option);
  } else if (requested && !/^BATCH_\d{8}_\d{6,}$/.test(requested)) requested = '';
  select.value = requested;
  function render() {
    var bid = select.value, row = rows[bid], url = new URL(workflow.ingest_url + '/');
    if (bid) url.searchParams.set('batch_id', bid);
    back.href = url.href; back.textContent = bid ? '返回这个批次的拷贝记录' : '打开拷贝备份';
    progress.textContent = !bid ? '两项服务独立运行。选择批次查看交接状态，或浏览下方全部待处理素材。' : !row ? '当前清单尚未包含这个批次。请查看“新拷贝素材”进度，准备好后保存草稿并刷新。' :
      (phases[row.phase] || phases.unknown) + ' · ' + row.units + ' 单元 / ' + row.files + ' 文件；已归档 ' + row.archived + (row.retained ? '，已复制但未完整校验 ' + row.retained : '') + '，待确认 ' + row.pending + '，异常 ' + row.exceptions + '，辅助 ' + row.support + '，弃用 ' + row.discarded + (row.reason ? '。' + row.reason : '') + (row.verified_files != null ? '；手工归档已核验 ' + row.verified_files + ' 文件' : '') + '。素材数量取自本页快照。';
    window.muliReview.setBatchFilter(bid);
  }
  select.addEventListener('change', function () {
    var url = new URL(window.location.href);
    if (select.value) url.searchParams.set('batch_id', select.value); else url.searchParams.delete('batch_id');
    window.history.replaceState(null, '', url.href); render();
  });
  window.addEventListener('muli-discovery-status', function (event) {
    var data = event.detail, bid = select.value;
    if (!data || !bid) return;
    var incoming = data.batches.find(function (r) { return r.batch_id === bid; });
    if (data.source_ok !== true) { progress.textContent = '暂时无法读取备份交接状态，当前选择保留。'; return; }
    if (incoming) {
      var verification = incoming.verification || {}, label;
      if (verification.state === 'manual_archive_verified') label = '已核验手工归档，无需重复归档';
      else if (['failed','blocked','interrupted','unavailable'].indexOf(verification.state) >= 0 || /失败/.test(incoming.reason || '')) label = '交接需要处理';
      else label = ({waiting_completion:'正在拷贝，完成后交接', verification_required:'正在核对内容，校验通过后交接', queued:'正在排队整理', processing:'正在整理拍摄段', retry_wait:'交接需要处理'})[incoming.state] || '状态待核对';
      progress.textContent = label + (verification.total_files ? ' · 已核验 ' + (verification.verified_files || 0) + '/' + verification.total_files + ' 文件' : '') + '。准备好后保存草稿并查看新素材。';
    } else if (data.base_report_id && data.base_report_id !== settings.discovery_state?.base_report_id) {
      progress.textContent = '素材清单已更新，请保存草稿并查看新素材。';
    }
  });
  panel.hidden = false; render();
})();
