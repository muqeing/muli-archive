/* Same-origin submission controls. This script is embedded only when enabled. */
(function () {
  "use strict";

  var settingsNode = document.getElementById("review-settings");
  var settings = {};
  try { settings = JSON.parse(settingsNode && settingsNode.textContent || "{}"); } catch (error) { settings = {}; }
  if (!settings || settings.submission_enabled !== true) return;

  var review = window.muliReview;
  var model = {};
  try { model = JSON.parse((document.getElementById("review-model") || {}).textContent || "{}"); } catch (error) { model = {}; }
  try { window.muliExpandReviewPresentation(model); } catch (error) { return; }
  var units = Array.isArray(model.units) ? model.units : [];
  var unitById = Object.create(null);
  units.forEach(function (unit) { unitById[String(unit && unit.unit_id || "")] = unit; });
  var pageIsHttp = window.location && (window.location.protocol === "http:" || window.location.protocol === "https:");
  var recoveryStorageKey = "muli.archive-submit.recovery/v1:" + String(model.report_id || (window.location && window.location.pathname) || "unknown");
  var submissionRecoveryStorageKey = "muli.archive-submit.pending-submission/v1";
  var initialRecovery = readRecoveryState();
  var initialSubmissionRecovery = readGlobalSubmissionState();

  function validClientId(value) {
    return typeof value === "string" && /^[a-f0-9]{32}$/i.test(value);
  }

  function randomHex(length) {
    var output = "";
    var cryptoObject = window.crypto;
    if (cryptoObject && typeof cryptoObject.getRandomValues === "function") {
      var bytes = new Uint8Array(Math.ceil(length / 2));
      cryptoObject.getRandomValues(bytes);
      Array.prototype.forEach.call(bytes, function (value) { output += value.toString(16).padStart(2, "0"); });
    } else {
      while (output.length < length) output += Math.floor(Math.random() * 0x100000000).toString(16);
    }
    return output.slice(0, length);
  }
  var clientId = initialRecovery && validClientId(initialRecovery.client_id) ? initialRecovery.client_id.toLowerCase() : randomHex(32);
  var revision = initialRecovery && Number.isInteger(initialRecovery.revision) && initialRecovery.revision >= 1 ? initialRecovery.revision : 0;
  var preflightTimer = null;
  var preflightRequest = 0;
  var preflightPollTimer = null;
  var preflightPollActive = false;
  var preflightPollToken = 0;
  var preflightCheckId = null;
  var preflightCheckRevision = 0;
  var preflightStatus = null;
  var preflightPollFailed = false;
  var preflightNeedsRestart = false;
  var preflightPlan = null;
  var planFingerprint = null;
  var ready = null;
  var frozenPlan = null;
  var submitBusy = false;
  var submissionDone = false;
  var uncertainSubmit = false;
  var submissionRecoveryId = null;
  var submissionRecoveryStatus = null;
  var submissionRecoveryPlanFingerprint = null;
  var submissionPollTimer = null;
  var submissionPollActive = false;
  var submissionPollToken = 0;
  var preflightRecoveryPending = false;
  var confirmationRecoveryPending = false;
  var confirmationRecoveryTicketId = null;
  var confirmationRecoveryRetryable = false;
  var confirmationRecoveryManualRequired = false;
  var jobs = [];
  var polling = Object.create(null);
  var retrying = Object.create(null);
  var archiveOptions = { mode: "copy", existing: "skip_identical" };
  var moveEnabled = settings.move_enabled === true;
  var archiveStateBlocked = false;
  var refreshFeedbackTimer = null;
  var refreshRequest = 0;
  var locatedJobId = null;

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function byId(id) { return document.getElementById(id); }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function finiteNumber(value) { var n = Number(value); return Number.isFinite(n) && n >= 0 ? n : 0; }
  function humanBytes(value) {
    var amount = finiteNumber(value), labels = ["B", "KB", "MB", "GB", "TB"], index = 0;
    while (amount >= 1024 && index < labels.length - 1) { amount /= 1024; index += 1; }
    return (index ? amount.toFixed(1) : Math.round(amount)) + " " + labels[index];
  }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function canonicalPlan(value) {
    if (Array.isArray(value)) return value.map(canonicalPlan);
    if (!value || typeof value !== "object") return value;
    var result = {};
    Object.keys(value).sort().forEach(function (key) { result[key] = canonicalPlan(value[key]); });
    return result;
  }
  function planFingerprintFor(plan) {
    var comparable = clone(plan || {});
    if (comparable && typeof comparable === "object") delete comparable.created_at;
    return JSON.stringify(canonicalPlan(comparable));
  }
  function readRecoveryState() {
    try {
      var raw = window.sessionStorage && window.sessionStorage.getItem(recoveryStorageKey);
      if (!raw) return null;
      var value = JSON.parse(raw);
      if (!value || value.schema !== 1 || value.report_id !== String(model.report_id || "")) return null;
      if (!validClientId(value.client_id) || !Number.isInteger(value.revision) || value.revision < 1 || typeof value.plan_fingerprint !== "string") return null;
      return value;
    } catch (error) { return null; }
  }
  function readGlobalSubmissionState() {
    try {
      var raw = window.sessionStorage && window.sessionStorage.getItem(submissionRecoveryStorageKey);
      if (!raw) return null;
      var value = JSON.parse(raw);
      if (!value || value.schema !== 1 || typeof value.submission_id !== "string" || !value.submission_id ||
          ["checking", "unknown"].indexOf(value.status) < 0 || typeof value.plan_fingerprint !== "string") return null;
      return value;
    } catch (error) { return null; }
  }
  function recoverySubmissionSnapshot() {
    if (!submissionRecoveryId || !submissionRecoveryStatus) return null;
    return { submission_id: submissionRecoveryId, status: submissionRecoveryStatus, plan_fingerprint: submissionRecoveryPlanFingerprint || planFingerprint,
      report_id: String(model.report_id || "") };
  }
  function writeRecoveryState() {
    var value = {
      schema: 1,
      report_id: String(model.report_id || ""),
      client_id: clientId,
      revision: revision,
      plan_fingerprint: planFingerprint,
      check_id: preflightCheckId,
      check_revision: preflightCheckRevision,
      preflight_status: preflightStatus,
      frozen_plan: frozenPlan ? clone(frozenPlan) : (preflightPlan ? clone(preflightPlan) : null),
      ready: ready && validExpiry(ready.expires_at) ? clone(ready) : null,
      confirmation_ticket_id: confirmationRecoveryTicketId,
      confirmation_manual_required: confirmationRecoveryManualRequired,
      submission: recoverySubmissionSnapshot(),
      saved_at: Date.now()
    };
    if (!value.plan_fingerprint && !value.check_id && !value.submission) {
      clearRecoveryState();
      try { if (window.sessionStorage) window.sessionStorage.removeItem(submissionRecoveryStorageKey); } catch (error) {}
      return;
    }
    try { if (window.sessionStorage) window.sessionStorage.setItem(recoveryStorageKey, JSON.stringify(value)); } catch (error) {}
    try {
      if (!window.sessionStorage) return;
      if (value.submission) window.sessionStorage.setItem(submissionRecoveryStorageKey, JSON.stringify({ schema: 1, submission_id: value.submission.submission_id,
        status: value.submission.status, plan_fingerprint: value.submission.plan_fingerprint, report_id: value.submission.report_id }));
      else window.sessionStorage.removeItem(submissionRecoveryStorageKey);
    } catch (error) {}
  }
  function clearRecoveryState() {
    try { if (window.sessionStorage) window.sessionStorage.removeItem(recoveryStorageKey); } catch (error) {}
  }
  function clearPreflightRecoveryState() {
    preflightRecoveryPending = false;
    confirmationRecoveryPending = false;
    confirmationRecoveryTicketId = null;
    confirmationRecoveryRetryable = false;
    confirmationRecoveryManualRequired = false;
    preflightCheckId = null;
    preflightCheckRevision = 0;
    preflightStatus = null;
    preflightPlan = null;
    ready = null;
    frozenPlan = null;
    writeRecoveryState();
  }
  function statusText(status, job) {
    if (status === "copied_unverified") return "已复制，来源保留（未完整校验）";
    if (status === "completed") return "已完成（已核验" + (archiveModeLabel(job) === "移动归档" ? "移动" : "拷贝") + "）";
    return ({ queued: "排队中", running: "归档中", partial: "部分完成", failed: "失败" })[status] || "状态待确认";
  }
  function archiveModeLabel(source) {
    var options = source && source.archive_options ? source.archive_options : source;
    return options && options.mode === "move" ? "移动归档" : "复制归档";
  }
  function existingBehaviorLabel(source) {
    var options = source && source.archive_options ? source.archive_options : source;
    return options && options.existing === "skip_identical" ? "相同跳过，冲突自动编号" : "遇到现有文件即停止";
  }
  function countValue(value) {
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) return value;
    if (Array.isArray(value)) return value.length;
    return 0;
  }
  function verificationText(verification) {
    if (!verification || typeof verification !== "object" || Array.isArray(verification)) return "";
    var known = ["checked_files", "total_files", "checked_bytes", "total_bytes", "reused_files", "hashed_files"];
    if (!known.some(function (key) { return Object.prototype.hasOwnProperty.call(verification, key); })) return "";
    return "核对 " + countValue(verification.checked_files) + " / " + countValue(verification.total_files) +
      " 个文件 · 核对量 " + humanBytes(verification.checked_bytes) + " / " + humanBytes(verification.total_bytes) +
      " · 复用校验数 " + countValue(verification.reused_files) + " · 本次读取数 " + countValue(verification.hashed_files);
  }
  function submissionPlan(plan) {
    var result = clone(plan || {});
    result.archive_options = { mode: archiveOptions.mode, existing: archiveOptions.existing };
    return result;
  }
  function archivedUnitSet() {
    var result = Object.create(null);
    if (review && typeof review.getArchivedUnitIds === "function") {
      review.getArchivedUnitIds().forEach(function (id) { result[String(id)] = true; });
    } else {
      var initial = settings.archive_state && Array.isArray(settings.archive_state.archived_units) ? settings.archive_state.archived_units : [];
      initial.forEach(function (entry) { if (entry && entry.in_current_model === true) result[String(entry.unit_id)] = true; });
    }
    return result;
  }
  function summaryOutcomeText(summary, source, planned) {
    summary = summary || {};
    var parts = [archiveModeLabel(source) + " · " + existingBehaviorLabel(source)];
    if (!planned && source && source.execution_strategy === "same_volume_rename/v1") parts.push("新文件同卷直接移动；相同文件单独跳过");
    if (!planned && source && source.execution_strategy === "copy_then_cleanup" && source.archive_options && source.archive_options.mode === "move") parts.push("旧任务执行方式：先复制并核验，再清理来源");
    var verb = planned ? "将" : "已";
    var copied = countValue(summary.copy_files);
    var direct = countValue(summary.direct_move_files !== undefined ? summary.direct_move_files : summary.direct_moved_files);
    var skipped = countValue(summary.skipped_files);
    var cleaned = countValue(summary.cleanup_files !== undefined ? summary.cleanup_files : summary.removed_sources);
    var renamed = countValue(summary.renamed_files);
    if (direct) parts.push(verb + "同卷直接移动 " + direct + " 个文件");
    if (direct && planned) parts.push("核对整批来源后逐个移入项目；中断后按记录继续");
    if (copied) parts.push(verb + "复制 " + copied + " 个文件");
    if (skipped) parts.push(verb + "跳过相同文件 " + skipped + " 个");
    if (planned && skipped && source && source.archive_options && source.archive_options.mode === "move") parts.push("全部目标核对后清理重复来源，不复制新副本");
    if (renamed) parts.push(verb + "为冲突文件编号 " + renamed + " 个");
    if (cleaned) parts.push(verb + "清理源文件 " + cleaned + " 个");
    return parts.join(" · ");
  }
  function basename(value) {
    var parts = String(value || "").split(/[\\/]/);
    return parts[parts.length - 1];
  }
  function renderFileActions(actions) {
    var node = byId("archive-submit-file-actions");
    if (!node) return;
    clear(node);
    var renamed = (Array.isArray(actions) ? actions : []).filter(function (action) {
      if (!action || typeof action !== "object") return false;
      if (action.renamed === true || action.action === "renamed") return true;
      return basename(action.source_name) && basename(action.target_path) && basename(action.source_name) !== basename(action.target_path);
    });
    if (!renamed.length) { node.hidden = true; return; }
    node.hidden = false;
    var details = make("details");
    details.appendChild(make("summary", "", "查看编号后的文件名"));
    renamed.slice(0, 50).forEach(function (action) {
      var source = String(action.source_name || "未知源文件");
      var target = String(action.target_path || "未知目标文件");
      details.appendChild(make("div", "archive-submit-file-action", source + " → " + target));
    });
    if (renamed.length > 50) details.appendChild(make("p", "muted", "其余 " + (renamed.length - 50) + " 个编号文件未展开。"));
    node.appendChild(details);
  }
  function formatWhen(value) {
    var date = null;
    if (typeof value === "number" && Number.isFinite(value)) date = new Date(value * 1000);
    else if (typeof value === "string" && value) date = new Date(value);
    if (!date || !Number.isFinite(date.getTime())) return String(value || "");
    return date.toLocaleString("zh-CN", { hour12: false });
  }
  function feedbackCount(value) {
    var number = Number(value);
    return Number.isInteger(number) && number >= 0 ? String(number) : "—";
  }
  function archiveHandoffView(value, jobStatus) {
    var labels = { disabled: "批次反馈未启用", pending: "批次反馈待同步", synced: "批次反馈已同步", needs_review: "批次反馈需要核对", not_included: "历史任务未自动回填" };
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      if (jobStatus === "queued" || jobStatus === "running") {
        return { key: "pending", label: "归档完成后同步批次反馈", note: "当前归档尚未完成，暂不生成批次反馈。", batches: [] };
      }
      if (jobStatus === "failed" || jobStatus === "partial") {
        return { key: "unsynced", label: "归档未完成，未生成批次反馈", note: "当前任务未完成，不能据此确认已转存。", batches: [] };
      }
      return { key: "unsynced", label: "批次反馈未同步", note: "旧服务未返回批次反馈，不能据此判断已反馈。", batches: [] };
    }
    var key = Object.prototype.hasOwnProperty.call(labels, value.status) ? value.status : "needs_review";
    var notes = {
      disabled: "当前归档服务未启用批次反馈。",
      pending: "后台尚未完成批次反馈同步。",
      synced: "已反馈到拷贝系统，各批次进度如下。",
      needs_review: "反馈状态需要核对，不能推断批次已完成转存。",
      not_included: "该任务早于批次反馈启用时间，未自动回填；不能据此判断已转存。"
    };
    var batches = Array.isArray(value.batches) ? value.batches.filter(function (batch) { return batch && typeof batch === "object" && !Array.isArray(batch); }) : [];
    var revoked = value.revoked_batches && typeof value.revoked_batches === "object" && !Array.isArray(value.revoked_batches) ? value.revoked_batches : {};
    return { key: key, label: labels[key], note: notes[key], checkedAt: value.checked_at, batches: batches, revoked: revoked };
  }
  function ingestBatchUrl(batchId) {
    var workflow = settings.workflow_state;
    var base = workflow && workflow.ingest_url;
    if (!base || batchId == null || String(batchId) === "") return null;
    try {
      var url = new URL(String(base), window.location.href);
      if (["http:", "https:"].indexOf(url.protocol) < 0 || url.username || url.password) return null;
      url.search = "";
      url.hash = "";
      url.searchParams.set("batch_id", String(batchId));
      return url.href;
    } catch (error) { return null; }
  }
  function validJobId(value) { return typeof value === "string" && /^[0-9a-f]{64}$/i.test(value); }
  function requestedJobId() {
    try {
      var value = new URL(window.location.href).searchParams.get("job_id");
      return validJobId(value) ? value.toLowerCase() : null;
    } catch (error) { return null; }
  }
  function archiveBatchFeedback(batch, revokedBatches) {
    var id = String(batch && (batch.batch_id || batch.batch_uid) || "未命名批次");
    var status = String(batch && batch.status || "needs_review");
    var label = ({ pending: "待同步", partial: "部分确认", confirmed: "整批已确认", needs_review: "需要核对" })[status] || "状态待确认";
    var confirmed = Number(batch && batch.confirmed_file_count);
    var total = Number(batch && batch.total_file_count);
    var shortfall = Number.isFinite(confirmed) && Number.isFinite(total) && total > confirmed ? total - confirmed : null;
    var text = "批次 " + id + "：" + label + " · 已确认 " + feedbackCount(batch && batch.confirmed_file_count) + " / " + feedbackCount(batch && batch.total_file_count) + " 个文件"
      + (shortfall ? " · 还差 " + shortfall + " 个没有有效归档回执" : "");
    var row = make("div", "archive-submit-job-progress", text);
    row.style.minWidth = "0";
    row.style.maxWidth = "100%";
    row.style.overflowWrap = "anywhere";
    var url = ingestBatchUrl(batch && batch.batch_id);
    if (url) {
      var link = make("a", "secondary", "查看拷贝批次");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      link.referrerPolicy = "origin";
      link.style.display = "inline-block";
      link.style.maxWidth = "100%";
      link.style.whiteSpace = "normal";
      link.style.overflowWrap = "anywhere";
      row.appendChild(document.createTextNode(" · "));
      row.appendChild(link);
    }
    if (batch && batch.checked_at) row.appendChild(make("span", "muted", " · 核对 " + formatWhen(batch.checked_at)));
    var revoked = revokedBatches && revokedBatches[String(batch && batch.batch_uid || "")];
    if (revoked && revoked.reason) {
      row.appendChild(make("span", "archive-submit-job-progress",
        "该批次有一条归档回执已被撤回：" + String(revoked.reason)
        + (revoked.at ? "（" + formatWhen(revoked.at) + "）" : "")
        + "。撤回的是机器确认，已在项目目录的文件不受影响。"));
    }
    return row;
  }
  function renderArchiveHandoff(job) {
    var view = archiveHandoffView(job && job.archive_handoff, job && job.status);
    var box = make("div", "archive-submit-handoff " + (view.key === "synced" ? "notice success" : "warning-box"));
    box.style.minWidth = "0";
    box.style.maxWidth = "100%";
    box.style.overflowWrap = "anywhere";
    box.appendChild(make("strong", "", view.label));
    box.appendChild(make("span", "archive-submit-job-progress", view.note));
    if (view.checkedAt) box.appendChild(make("span", "archive-submit-job-progress", "最近核对：" + formatWhen(view.checkedAt)));
    if (!view.batches.length && view.key !== "not_included") box.appendChild(make("span", "archive-submit-job-progress", "暂无逐批反馈。"));
    else view.batches.forEach(function (batch) { box.appendChild(archiveBatchFeedback(batch, view.revoked)); });
    return box;
  }
  function setStatus(message, kind) {
    var node = byId("archive-submit-status");
    if (!node) return;
    node.textContent = message;
    node.className = "notice " + (kind || "");
  }
  function setRefreshFeedback(message) {
    var node = byId("archive-submit-refresh-status");
    if (!node) return;
    if (refreshFeedbackTimer) window.clearTimeout(refreshFeedbackTimer);
    refreshFeedbackTimer = null;
    node.textContent = message || "";
    if (message) refreshFeedbackTimer = window.setTimeout(function () {
      node.textContent = "";
      refreshFeedbackTimer = null;
    }, 1000);
  }
  function appendIssues(node, issues) {
    (Array.isArray(issues) ? issues : []).forEach(function (issue) {
      if (!issue || typeof issue !== "object") return;
      var box = make("div", "panel archive-issue");
      var snapshotChanged = issue.scope === "plan" && !!issue.report_id && !!issue.current_report_id;
      var scopes = { file: "文件检查失败", unit: "素材检查失败", segment: "拍摄段检查失败", project: "项目检查失败", plan: "整份计划检查未通过" };
      box.appendChild(make("strong", "", snapshotChanged ? "页面素材清单已更新" : scopes[issue.scope] || "归档检查未通过"));
      box.appendChild(make("p", "warning", snapshotChanged ? "当前页面的归档计划需要与新清单重新核对，尚未检查或移动素材。" : String(issue.reason || "原因尚未明确")));
      var fields = { dev: "磁盘身份", ino: "文件身份", size: "文件大小", mtime_ns: "内容修改时间", ctime_ns: "属性变更时间" };
      (Array.isArray(issue.changed_signature_fields) ? issue.changed_signature_fields : []).forEach(function (change) {
        box.appendChild(make("p", "", "变化字段：" + String(fields[change.field] || change.field) + " · 检查时：" + String(change.expected) + " · 当前：" + String(change.actual)));
      });
      (Array.isArray(issue.segments) ? issue.segments : []).forEach(function (segment) {
        var line = make("p", "", "拍摄段：" + String(segment.label || "未命名拍摄段") + " · 编号：" + String(segment.segment_id || "未提供"));
        var card = Array.prototype.find.call(document.querySelectorAll("[data-segment-id]"), function (element) {
          return element.getAttribute("data-segment-id") === segment.segment_id;
        });
        if (card) {
          var locate = make("button", "secondary", "定位拍摄段"); locate.type = "button";
          locate.addEventListener("click", function () { card.scrollIntoView({ behavior: "smooth", block: "center" }); });
          line.appendChild(locate);
        } else line.appendChild(make("span", "muted", "（当前页面未显示此段）"));
        box.appendChild(line);
      });
      (Array.isArray(issue.units) ? issue.units : []).forEach(function (unit) {
        box.appendChild(make("p", "", "素材：" + String(unit.unit_id || "未提供") + " · 拍摄时间：" + String(unit.capture_time || unit.capture_date || "未知")));
      });
      var files = Array.isArray(issue.files) ? issue.files : [];
      if (files.length) {
        var details = make("details", ""); details.open = files.length <= 3;
        details.appendChild(make("summary", "", "对应文件 · " + files.length + " 个"));
        files.forEach(function (file) {
          details.appendChild(make("p", "", String(file.name || "未提供文件名") + " · 来源：" + String(file.source_path || "未提供路径")));
        });
        box.appendChild(details);
      }
      if (issue.target_path) box.appendChild(make("p", "", "目标：" + String(issue.target_path)));
      if (issue.report_id || issue.current_report_id) {
        var versions = make("details", "");
        versions.appendChild(make("summary", "", "查看页面与后台的清单编号"));
        versions.appendChild(make("p", "", "页面：" + String(issue.report_id || "未提供")));
        versions.appendChild(make("p", "", "后台：" + String(issue.current_report_id || "未提供")));
        box.appendChild(versions);
      }
      if (issue.guidance) box.appendChild(make("p", "muted", String(issue.guidance)));
      node.appendChild(box);
    });
  }
  function setErrors(errors, issues) {
    var node = byId("archive-submit-errors");
    if (!node) return;
    clear(node);
    var items = Array.isArray(errors) ? errors : [];
    var details = Array.isArray(issues) ? issues : [];
    items.forEach(function (error) {
      if (!details.some(function (issue) { return issue && issue.reason === String(error); })) node.appendChild(make("p", "warning", String(error)));
    });
    appendIssues(node, details);
    node.hidden = !items.length && !details.length;
  }
  function unitFiles(unit) {
    var count = Number(unit && unit.file_count);
    return Number.isFinite(count) && count >= 0 ? count : (Array.isArray(unit && unit.files) ? unit.files.length : 0);
  }
  function planSummary(plan) {
    var summary = { segments: 0, units: 0, files: 0, bytes: 0, pending_units: 0, deferred_units: 0 };
    if (!plan || !Array.isArray(plan.segments)) return summary;
    var archived = archivedUnitSet();
    plan.segments.forEach(function (segment) {
      var ids = (Array.isArray(segment.unit_ids) ? segment.unit_ids : []).filter(function (id) { return !archived[String(id)]; });
      var count = ids.length;
      if (segment.decision === "confirmed") {
        if (!count) return;
        summary.segments += 1;
        summary.units += count;
        ids.forEach(function (id) {
          var unit = unitById[id] || {};
          summary.files += unitFiles(unit);
          summary.bytes += finiteNumber(unit.bytes);
        });
      } else if (segment.decision === "pending") summary.pending_units += count;
      else if (segment.decision === "deferred") summary.deferred_units += count;
    });
    return summary;
  }
  function addFact(parent, label, value) {
    var item = make("div");
    item.appendChild(make("dt", "", label));
    item.appendChild(make("dd", "", value));
    parent.appendChild(item);
  }
  function renderPlanSummary(plan) {
    var node = byId("archive-submit-summary");
    if (!node) return;
    clear(node);
    var summary = planSummary(plan);
    addFact(node, "已确认拍摄段", summary.segments);
    addFact(node, "已确认素材", summary.units);
    addFact(node, "已确认文件", summary.files);
    addFact(node, "已确认大小", humanBytes(summary.bytes));
    addFact(node, "待处理 / 暂缓", summary.pending_units + " / " + summary.deferred_units + " 个素材");
    addFact(node, "本次归档", archiveModeLabel(archiveOptions) + " · " + existingBehaviorLabel(archiveOptions));
  }
  function renderProjects(projects) {
    var node = byId("archive-submit-projects");
    if (!node) return;
    clear(node);
    (Array.isArray(projects) ? projects : []).forEach(function (project) {
      var item = make("div", "archive-submit-project");
      item.appendChild(make("strong", "", String(project && project.name || "未命名项目") + " · " + (project && project.action === "existing" ? "现有项目" : "将创建文件夹")));
      item.appendChild(make("span", "muted", "路径：" + String(project && project.path || "待后端返回")));
      item.appendChild(make("span", "muted", "素材 " + finiteNumber(project && project.units) + " · 文件 " + finiteNumber(project && project.files) + " · " + humanBytes(project && project.bytes)));
      Object.keys(project.destinations || {}).forEach(function (folder) {
        item.appendChild(make("span", "muted", folder + "：" + project.destinations[folder] + " 个文件"));
      });
      if (Array.isArray(project.create_subfolders) && project.create_subfolders.length) item.appendChild(make("span", "muted", "将补齐标准子文件夹：" + project.create_subfolders.join("、")));
      node.appendChild(item);
    });
  }
  function renderJob(job) {
    var existing = jobs.filter(function (item) { return item && item.job_id === job.job_id; })[0];
    if (existing) Object.keys(job).forEach(function (key) { existing[key] = job[key]; });
    else jobs.unshift(job);
    jobs = jobs.filter(function (item) { return item && item.job_id; }).slice(0, 20);
    renderJobs();
  }
  function renderJobs() {
    var node = byId("archive-submit-jobs");
    if (!node) return;
    clear(node);
    if (!jobs.length) { node.appendChild(make("p", "muted", "暂无归档任务。")); return; }
    var requested = requestedJobId();
    var requestedNode = null;
    jobs.forEach(function (job) {
      var item = make("div", "archive-submit-job");
      item.style.minWidth = "0";
      item.style.maxWidth = "100%";
      var jobId = typeof job.job_id === "string" ? job.job_id : "";
      var normalizedJobId = validJobId(jobId) ? jobId.toLowerCase() : null;
      if (normalizedJobId) item.id = "archive-submit-job-" + normalizedJobId;
      var body = make("div");
      body.style.minWidth = "0";
      body.appendChild(make("strong", "", "归档任务 · " + statusText(job.status, job)));
      var summary = job.summary || {};
      var progress = "已完成 " + finiteNumber(summary.completed_units) + " / " + finiteNumber(summary.total_units) + " 个素材 · " + finiteNumber(summary.completed_files) + " / " + finiteNumber(summary.total_files) + " 个文件 · " + humanBytes(summary.processed_bytes) + " / " + humanBytes(summary.total_bytes);
      body.appendChild(make("span", "archive-submit-job-progress", progress));
      var verification = verificationText(job.verification);
      if (verification) body.appendChild(make("span", "archive-submit-job-progress", verification));
      body.appendChild(make("span", "archive-submit-job-progress", "方式：" + summaryOutcomeText(summary, job)));
      if (summary.skipped_files !== undefined || summary.copy_files !== undefined || summary.cleanup_files !== undefined || summary.removed_sources !== undefined || summary.renamed_files !== undefined) {
        body.appendChild(make("span", "archive-submit-job-progress", "结果：直接移动 " + countValue(summary.direct_moved_files) + " · 已复制 " + countValue(summary.copy_files) + " · 跳过相同 " + countValue(summary.skipped_files) + " · 冲突编号 " + countValue(summary.renamed_files) + " · 清理源文件 " + countValue(summary.cleanup_files !== undefined ? summary.cleanup_files : summary.removed_sources) + " 个"));
      }
      body.appendChild(renderArchiveHandoff(job));
      var currentFileBytes = summary.current_file_bytes !== undefined ? summary.current_file_bytes : job.current_file_bytes;
      if (job.status === "running" && currentFileBytes !== undefined) body.appendChild(make("span", "archive-submit-job-progress", "当前文件已处理 " + humanBytes(currentFileBytes)));
      if (job.phase) body.appendChild(make("span", "archive-submit-job-progress", "阶段：" + String(job.phase)));
      (Array.isArray(job.projects) ? job.projects : []).forEach(function (project) {
        body.appendChild(make("span", "archive-submit-job-progress", "项目：" + String(project && project.name || "未命名项目") + " · 路径：" + String(project && project.path || "待后端返回")));
      });
      if (terminal(job.status)) {
        var completedAt = job.completed_at || job.finished_at || job.completedAt;
        if (completedAt) body.appendChild(make("span", "archive-submit-job-progress", "完成时间：" + formatWhen(completedAt)));
      }
      var jobErrors = [];
      (Array.isArray(job.errors) ? job.errors : []).forEach(function (error) { jobErrors.push(String(error)); });
      (Array.isArray(job.outcomes) ? job.outcomes : []).forEach(function (outcome) { if (outcome && outcome.error) jobErrors.push(String(outcome.error)); });
      if (jobErrors.length || (Array.isArray(job.issues) && job.issues.length)) {
        var reasons = make("div", "warning-box");
        jobErrors.forEach(function (error) {
          if (!(job.issues || []).some(function (issue) { return issue && issue.reason === error; })) reasons.appendChild(make("p", "warning", error));
        });
        appendIssues(reasons, job.issues);
        body.appendChild(reasons);
      }
      item.appendChild(body);
      if (job.status === "failed" || job.status === "partial") {
        var retry = make("button", "secondary", "重试失败部分");
        retry.type = "button";
        retry.disabled = !!retrying[job.job_id];
        retry.addEventListener("click", function () { retryJob(job); });
        item.appendChild(retry);
      }
      if (requested && normalizedJobId === requested) {
        item.setAttribute("data-job-location", "true");
        item.setAttribute("tabindex", "-1");
        body.insertBefore(make("span", "muted", "当前 URL 指向的任务"), body.firstChild);
        requestedNode = item;
      }
      node.appendChild(item);
    });
    if (requestedNode && locatedJobId !== requested) {
      locatedJobId = requested;
      if (typeof requestedNode.focus === "function") requestedNode.focus({ preventScroll: false });
    }
  }
  function optionChoice(name, value, labelText, checked, disabled) {
    var label = make("label", "archive-submit-option");
    var input = make("input");
    input.type = "radio";
    input.name = "archive-submit-" + name;
    input.value = value;
    input.checked = checked;
    input.disabled = !!disabled;
    input.addEventListener("change", archiveOptionChanged);
    label.appendChild(input);
    label.appendChild(make("span", "", labelText));
    return label;
  }
  function setOptionControlsDisabled(disabled) {
    var node = byId("archive-submit-options");
    if (!node) return;
    Array.prototype.forEach.call(node.querySelectorAll("input"), function (input) { input.disabled = disabled || (input.value === "move" && !moveEnabled); });
  }
  function archiveOptionChanged(event) {
    if (submitBusy) return;
    var input = event && event.target;
    if (!input || !input.checked) return;
    if (input.value === "move" && !moveEnabled) return;
    if (input.name === "archive-submit-mode") archiveOptions.mode = input.value === "move" ? "move" : "copy";
    if (input.name === "archive-submit-existing") archiveOptions.existing = input.value === "error" ? "error" : "skip_identical";
    renderPlanSummary(review && review.getPlan ? review.getPlan() : null);
    planChanged();
  }
  function updateSubmitButton() {
    var button = byId("archive-submit-button");
    if (button) {
      button.textContent = archiveModeLabel(archiveOptions) === "移动归档" ? "提交并移动归档" : "提交并复制归档";
      button.disabled = !pageIsHttp || !ready || submitBusy || submissionDone || uncertainSubmit ||
        confirmationRecoveryPending || confirmationRecoveryRetryable;
    }
    setOptionControlsDisabled(submitBusy);
  }
  function setPreflightRetryVisible(visible) {
    var button = byId("archive-submit-preflight-retry");
    if (button) button.hidden = !visible;
  }
  function cancelPreflightPoll() {
    if (preflightPollTimer) window.clearTimeout(preflightPollTimer);
    preflightPollTimer = null;
    preflightPollToken += 1;
    preflightPollActive = false;
  }
  function cancelPreflight(checkRevision) {
    if (!pageIsHttp || !checkRevision || checkRevision < 1) return;
    requestJson("/api/preflight-checks/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Muli-Request": "1" },
      body: JSON.stringify({ client_id: clientId, revision: checkRevision })
    }, 15000).catch(function () {});
  }
  function planChanged(force) {
    var current = review && typeof review.getPlan === "function" ? submissionPlan(review.getPlan()) : null;
    var fingerprint = planFingerprintFor(current);
    if (force !== true && fingerprint === planFingerprint) return;
    planFingerprint = fingerprint;
    if (preflightTimer) window.clearTimeout(preflightTimer);
    preflightTimer = null;
    var previousRevision = revision;
    cancelPreflight(previousRevision);
    revision += 1;
    preflightRequest += 1;
    cancelPreflightPoll();
    preflightCheckId = null;
    preflightCheckRevision = 0;
    preflightStatus = null;
    preflightPollFailed = false;
    preflightNeedsRestart = false;
    preflightPlan = null;
    preflightRecoveryPending = false;
    confirmationRecoveryPending = false;
    confirmationRecoveryTicketId = null;
    confirmationRecoveryRetryable = false;
    confirmationRecoveryManualRequired = false;
    setPreflightRetryVisible(false);
    ready = null;
    frozenPlan = null;
    submissionDone = false;
    setErrors([]);
    renderProjects([]);
    renderFileActions([]);
    updateSubmitButton();
    if (!pageIsHttp) {
      setStatus("请从归档工作台打开，此预览页仍可保存草稿和导出计划。", "error");
      return;
    }
    var plan = review && typeof review.getPlan === "function" ? review.getPlan() : null;
    renderPlanSummary(plan);
    if (!review || typeof review.validate !== "function" || typeof review.getPlan !== "function") {
      setStatus("提交界面未能读取当前计划。", "error");
      return;
    }
    var errors = review.validate();
    if (errors.length) {
      setStatus("请先检查当前选择：" + errors[0], "error");
      return;
    }
    if (!planSummary(plan).units) {
      setStatus("先为拍摄段选择项目并确认归属，这里会显示本次归档的目录和文件数量。", "");
      return;
    }
    writeRecoveryState();
    if (uncertainSubmit) {
      setStatus("上次提交结果仍未确认，请先刷新任务状态；当前不会重新发起预检或提交。", "error");
      return;
    }
    setStatus("正在检查本次归档的目录和文件…", "");
    if (preflightTimer) window.clearTimeout(preflightTimer);
    var capturedRevision = revision;
    preflightTimer = window.setTimeout(function () { preflight(capturedRevision); }, 500);
  }
  async function requestJson(url, options, timeoutMs) {
    var requestOptions = Object.assign({}, options || {});
    var controller = null;
    var timeout = null;
    var timedOut = false;
    if (timeoutMs && typeof AbortController === "function") {
      controller = new AbortController();
      if (!requestOptions.signal) requestOptions.signal = controller.signal;
      timeout = window.setTimeout(function () { timedOut = true; controller.abort(); }, timeoutMs);
    }
    var response;
    var data = null;
    try {
      response = await fetch(url, requestOptions);
      try { data = await response.json(); } catch (error) {
        if (timedOut) throw error;
        data = null;
      }
    } catch (error) {
      if (timedOut) {
        var timeoutError = new Error("请求超时");
        timeoutError.timeout = true;
        throw timeoutError;
      }
      throw error;
    } finally {
      if (timeout) window.clearTimeout(timeout);
    }
    var readableError = data && (typeof data.error === "string" ? data.error : data.errors && data.errors[0]);
    if (!response.ok) {
      var failure = new Error(readableError ? String(readableError) : "HTTP " + response.status);
      failure.status = response.status;
      failure.issues = Array.isArray(data && data.issues) ? data.issues : [];
      throw failure;
    }
    return data || {};
  }
  async function refreshArchiveState() {
    if (!pageIsHttp || !review || typeof review.applyArchiveState !== "function") return false;
    archiveStateBlocked = false;
    try {
      var data = await requestJson("/api/archive-state?view=compact&report_id=" + encodeURIComponent(String(model.report_id || "")), { headers: { "X-Muli-Request": "1" } });
      var applied = review.applyArchiveState(data, { silent: true });
      if (!applied) archiveStateBlocked = true;
      if (applied) renderPlanSummary(review.getPlan ? review.getPlan() : null);
      return applied;
    } catch (error) {
      if (error && error.status === 409) {
        archiveStateBlocked = true;
        setStatus("当前页面需要更新；请先保存草稿后刷新页面。", "error");
      }
      return false;
    }
  }
  function preflightProgressText(data) {
    var progress = data && data.progress || {};
    var status = data && data.status;
    var phase = progress.phase ? "阶段：" + String(progress.phase) : "";
    var checked = finiteNumber(progress.checked_files);
    var total = finiteNumber(progress.total_files);
    var files = "已检查 " + checked + " / " + total + " 个文件";
    var bytes = "已读入 " + humanBytes(progress.bytes_read);
    var elapsed = "已用 " + finiteNumber(progress.elapsed_seconds).toFixed(1) + " 秒";
    var current = progress.current_file ? " · 当前：" + String(progress.current_file) : "";
    if (status === "queued") return "预检排队中" + (phase ? " · " + phase : "") + " · " + elapsed;
    return "正在预检" + (phase ? " · " + phase : "") + " · " + files + " · " + bytes + " · " + elapsed + current;
  }
  function preflightErrorText(error) {
    if (error && error.timeout) return "预检请求超时（单次请求最多等待 15 秒）";
    if (error && error.status === 409) return "预检记录已过期或服务已重启";
    if (error && error.status >= 500) return "预检服务暂时不可用（HTTP " + error.status + "）";
    if (error && error.status >= 400) return "预检请求被拒绝（HTTP " + error.status + "）";
    return "预检连接失败，请检查归档工作台网络连接。";
  }
  function validPreflightCheckId(value) {
    return typeof value === "string" && /^[0-9a-f]{64}$/i.test(value);
  }
  function markPreflightNeedsRestart(message, clearCheck) {
    if (clearCheck) {
      preflightCheckId = null;
      preflightCheckRevision = 0;
    }
    preflightNeedsRestart = true;
    preflightPollFailed = true;
    ready = null;
    setErrors([message]);
    setPreflightRetryVisible(true);
    setStatus(message + " 可点击“重新检查”重新发起检查。", "error");
    writeRecoveryState();
    updateSubmitButton();
  }
  function applyPreflightResult(data, capturedRevision, requestId, planForCheck, expectedCheckId) {
    if (capturedRevision !== revision || requestId !== preflightRequest) return false;
    if (expectedCheckId && data && data.check_id && data.check_id !== expectedCheckId) {
      markPreflightNeedsRestart("预检响应编号不一致，请重新检查。", true);
      return false;
    }
    if (data && typeof data.check_id === "string" && data.check_id) {
      preflightCheckId = data.check_id;
      preflightCheckRevision = capturedRevision;
    }
    var status = data && data.status;
    preflightStatus = status;
    preflightRecoveryPending = false;
    if (status === "queued" || status === "checking") {
      if (!validPreflightCheckId(preflightCheckId)) {
        markPreflightNeedsRestart("预检服务返回了无效检查编号，请重新检查。", true);
        return false;
      }
      preflightNeedsRestart = false;
      preflightPollFailed = false;
      setPreflightRetryVisible(false);
      setErrors([]);
      renderProjects([]);
      renderFileActions([]);
      setStatus(preflightProgressText(data), "");
      writeRecoveryState();
      updateSubmitButton();
      schedulePreflightPoll(preflightCheckId, capturedRevision, requestId, planForCheck, 1000);
      return true;
    }
    if (preflightPollTimer) window.clearTimeout(preflightPollTimer);
    preflightPollTimer = null;
    preflightPollFailed = false;
    setPreflightRetryVisible(false);
    var responseErrors = Array.isArray(data && data.errors) ? data.errors : [];
    setErrors(responseErrors, data && data.issues);
    renderProjects(data && data.projects);
    renderFileActions(data && data.file_actions);
    var summary = data && data.summary || {};
    var concrete = Array.isArray(data && data.projects) && data.projects.length > 0 && data.projects.every(function (project) {
      return project && typeof project.path === "string" && project.path.length > 0;
    });
    var summaryFiles = finiteNumber(summary.files);
    if (status === "ready" && typeof data.preview_id === "string" && data.preview_id && validExpiry(data.expires_at) && !responseErrors.length && concrete && summaryFiles > 0) {
      preflightNeedsRestart = false;
      frozenPlan = clone(planForCheck);
      ready = { preview_id: data.preview_id, expires_at: data.expires_at, summary: summary, projects: data.projects || [] };
      writeRecoveryState();
      setStatus("可以提交：" + summaryOutcomeText(summary, planForCheck, true) + "；已确认素材将按上方路径归档，待处理和暂缓的不提交。", "success");
    } else if (status === "blocked") {
      preflightNeedsRestart = false;
      ready = null;
      setStatus("预检未通过，请先查看下方提示。", "error");
    } else if (status === "cancelled") {
      preflightNeedsRestart = true;
      ready = null;
      setStatus("本次预检已取消，请重新检查当前计划。", "error");
      setPreflightRetryVisible(true);
    } else {
      preflightNeedsRestart = true;
      ready = null;
      setStatus("预检结果不完整，请重新检查当前计划。", "error");
      setPreflightRetryVisible(true);
    }
    writeRecoveryState();
    updateSubmitButton();
    return true;
  }
  function requestConfirmationTicket(ticketId, planForTicket) {
    if (!ticketId || !planForTicket || !pageIsHttp || uncertainSubmit) return false;
    var capturedRevision = revision;
    var requestId = ++preflightRequest;
    confirmationRecoveryPending = true;
    confirmationRecoveryRetryable = true;
    confirmationRecoveryManualRequired = false;
    confirmationRecoveryTicketId = ticketId;
    preflightCheckId = null;
    preflightCheckRevision = 0;
    preflightRecoveryPending = false;
    preflightPlan = clone(planForTicket);
    preflightNeedsRestart = false;
    preflightPollFailed = false;
    setPreflightRetryVisible(false);
    setStatus("正在恢复已确认的归档计划…", "");
    updateSubmitButton();
    requestJson("/api/confirmation-tickets/" + encodeURIComponent(ticketId), { headers: { "X-Muli-Request": "1" } }, 15000).then(function (data) {
      if (capturedRevision !== revision || requestId !== preflightRequest || ticketId !== confirmationRecoveryTicketId) return;
      if (!data || data.preview_id !== ticketId) {
        var mismatch = new Error("确认票据编号不一致");
        mismatch.status = 409;
        throw mismatch;
      }
      confirmationRecoveryPending = false;
      confirmationRecoveryRetryable = false;
      applyPreflightResult(data, capturedRevision, requestId, planForTicket, null);
    }).catch(function (error) {
      if (capturedRevision !== revision || requestId !== preflightRequest || ticketId !== confirmationRecoveryTicketId) return;
      confirmationRecoveryPending = false;
      if (error && (error.status === 404 || error.status === 409)) {
        confirmationRecoveryRetryable = false;
        confirmationRecoveryTicketId = null;
        confirmationRecoveryManualRequired = true;
        preflightNeedsRestart = true;
        preflightPollFailed = true;
        preflightPlan = null;
        ready = null;
        frozenPlan = null;
        setErrors(["已确认的归档计划已过期或不存在，请重新检查当前计划。"]);
        setStatus("已确认的归档计划已过期或不存在，请点击“重新检查”。", "error");
      } else {
        confirmationRecoveryRetryable = true;
        confirmationRecoveryManualRequired = false;
        preflightNeedsRestart = false;
        preflightPollFailed = true;
        setErrors(["暂时无法读取已确认的归档计划，请重试。"]);
        setStatus("暂时无法读取已确认的归档计划，可点击“重试”继续读取；不会重新发起预检。", "error");
      }
      setPreflightRetryVisible(true);
      writeRecoveryState();
      updateSubmitButton();
    });
    return true;
  }
  function schedulePreflightPoll(checkId, capturedRevision, requestId, planForCheck, delay) {
    if (!checkId || capturedRevision !== revision || requestId !== preflightRequest || preflightPollTimer || preflightPollActive) return;
    var pollToken = preflightPollToken;
    preflightPollTimer = window.setTimeout(async function () {
      preflightPollTimer = null;
      if (pollToken !== preflightPollToken || capturedRevision !== revision || requestId !== preflightRequest || checkId !== preflightCheckId) return;
      preflightPollActive = true;
      try {
        var data = await requestJson("/api/preflight-checks/" + encodeURIComponent(checkId), { headers: { "X-Muli-Request": "1" } }, 15000);
        if (pollToken !== preflightPollToken || capturedRevision !== revision || requestId !== preflightRequest || checkId !== preflightCheckId) return;
        preflightPollActive = false;
        applyPreflightResult(data, capturedRevision, requestId, planForCheck, checkId);
      } catch (error) {
        if (pollToken !== preflightPollToken || capturedRevision !== revision || requestId !== preflightRequest) return;
        preflightPollActive = false;
        if (error && (error.status === 404 || error.status === 409)) {
          preflightCheckId = null;
          preflightCheckRevision = 0;
          preflightNeedsRestart = true;
          preflightRecoveryPending = false;
        } else {
          preflightNeedsRestart = false;
        }
        preflightPollFailed = true;
        setPreflightRetryVisible(true);
        setStatus(preflightErrorText(error) + " 可点击“重新检查”继续读取本次预检。", "error");
        writeRecoveryState();
        updateSubmitButton();
      }
    }, delay || 0);
  }
  async function preflight(capturedRevision) {
    if (capturedRevision !== revision || !pageIsHttp || !review) return;
    if (uncertainSubmit) {
      setStatus("上次提交结果仍未确认，请先刷新任务状态；当前不会自动重新发起预检。", "error");
      return;
    }
    var errors = review.validate();
    if (errors.length) { setStatus("请先检查当前选择：" + errors[0], "error"); return; }
    var planForCheck = submissionPlan(review.getPlan());
    preflightPlan = clone(planForCheck);
    planFingerprint = planFingerprintFor(planForCheck);
    var requestId = ++preflightRequest;
    writeRecoveryState();
    try {
      var data = await requestJson("/api/preflight-checks", { method: "POST", headers: { "Content-Type": "application/json", "X-Muli-Request": "1" }, body: JSON.stringify({ client_id: clientId, revision: capturedRevision, decisions: clone(planForCheck) }) }, 15000);
      if (capturedRevision !== revision || requestId !== preflightRequest) return;
      preflightCheckId = typeof data.check_id === "string" ? data.check_id : null;
      preflightCheckRevision = capturedRevision;
      writeRecoveryState();
      applyPreflightResult(data, capturedRevision, requestId, planForCheck, null);
    } catch (error) {
      if (capturedRevision !== revision || requestId !== preflightRequest) return;
      preflightCheckId = null;
      preflightCheckRevision = 0;
      preflightStatus = null;
      preflightPollFailed = true;
      preflightNeedsRestart = true;
      ready = null;
      setErrors([preflightErrorText(error)], error && error.issues);
      setPreflightRetryVisible(true);
      setStatus(preflightErrorText(error) + " 可点击“重新检查”重新发起检查。", "error");
      writeRecoveryState();
      updateSubmitButton();
    }
  }
  function retryPreflight() {
    if (uncertainSubmit) {
      setStatus("上次提交结果仍未确认，请先刷新任务状态；不会盲目重试提交。", "error");
      return;
    }
    if (!confirmationRecoveryPending && confirmationRecoveryRetryable && confirmationRecoveryTicketId) {
      requestConfirmationTicket(confirmationRecoveryTicketId, preflightPlan || submissionPlan(review.getPlan()));
      return;
    }
    if (!preflightNeedsRestart && preflightCheckId && preflightCheckRevision === revision && (preflightStatus === "queued" || preflightStatus === "checking")) {
      preflightPollFailed = false;
      setPreflightRetryVisible(false);
      setStatus("正在重新读取本次预检…", "");
      schedulePreflightPoll(preflightCheckId, revision, preflightRequest, preflightPlan || submissionPlan(review.getPlan()), 0);
      return;
    }
    confirmationRecoveryManualRequired = false;
    planChanged(true);
  }
  function refreshPreflightCheck() {
    if (confirmationRecoveryPending || confirmationRecoveryRetryable || confirmationRecoveryManualRequired) return;
    if (!preflightCheckId && preflightNeedsRestart && preflightPollFailed) {
      planChanged(true);
      return;
    }
    if (!preflightCheckId || preflightCheckRevision !== revision || preflightPollActive || preflightPollTimer) return;
    if (preflightStatus !== "queued" && preflightStatus !== "checking" && !preflightPollFailed && !preflightRecoveryPending) return;
    preflightPollFailed = false;
    schedulePreflightPoll(preflightCheckId, revision, preflightRequest, preflightPlan || submissionPlan(review.getPlan()), 0);
  }
  function stopSubmissionPoll() {
    if (submissionPollTimer) window.clearTimeout(submissionPollTimer);
    submissionPollTimer = null;
    submissionPollToken += 1;
    submissionPollActive = false;
  }
  function submissionResultText(data) {
    if (data && data.error) return String(data.error);
    if (data && data.status === "rejected") return "提交被拒绝";
    if (data && (data.status === "unknown" || data.status === "not_found")) return "提交结果仍未知";
    return "提交结果暂不明确";
  }
  function persistSubmissionMarker(id, status) {
    submissionRecoveryId = typeof id === "string" ? id : null;
    submissionRecoveryStatus = status || "checking";
    if (!submissionRecoveryPlanFingerprint) submissionRecoveryPlanFingerprint = planFingerprint;
    writeRecoveryState();
  }
  function clearSubmissionMarker() {
    stopSubmissionPoll();
    submissionRecoveryId = null;
    submissionRecoveryStatus = null;
    submissionRecoveryPlanFingerprint = null;
    writeRecoveryState();
  }
  function acceptedSubmission(data, submissionRevision, submissionPlan) {
    if (!data || typeof data.job_id !== "string" || !data.job_id) {
      uncertainSubmit = true;
      persistSubmissionMarker(submissionRecoveryId, "unknown");
      setStatus("提交已被服务接受但未返回任务编号，请刷新任务状态后再试。", "error");
      updateSubmitButton();
      return;
    }
    uncertainSubmit = false;
    submissionDone = submissionRevision === revision;
    submitBusy = false;
    ready = null;
    frozenPlan = null;
    preflightCheckId = null;
    preflightCheckRevision = 0;
    preflightRecoveryPending = false;
    confirmationRecoveryPending = false;
    confirmationRecoveryTicketId = null;
    confirmationRecoveryRetryable = false;
    confirmationRecoveryManualRequired = false;
    clearSubmissionMarker();
    if (submissionRevision === revision) setStatus("提交已接收，正在读取归档任务状态…", "success");
    updateSubmitButton();
    pollJob(data.job_id, 0, submissionRevision);
  }
  function rejectedSubmission(data) {
    submitBusy = false;
    uncertainSubmit = false;
    clearSubmissionMarker();
    ready = null;
    frozenPlan = null;
    preflightPlan = null;
    preflightCheckId = null;
    preflightCheckRevision = 0;
    preflightRecoveryPending = false;
    confirmationRecoveryPending = false;
    confirmationRecoveryTicketId = null;
    confirmationRecoveryRetryable = false;
    confirmationRecoveryManualRequired = false;
    preflightNeedsRestart = true;
    writeRecoveryState();
    setErrors([submissionResultText(data)], data && data.issues);
    setStatus("提交未执行，请核对当前计划后手动重新检查。", "error");
    setPreflightRetryVisible(true);
    updateSubmitButton();
  }
  function unknownSubmission(data) {
    submitBusy = false;
    uncertainSubmit = true;
    persistSubmissionMarker(submissionRecoveryId, "unknown");
    setPreflightRetryVisible(false);
    setStatus(submissionResultText(data) + "，请刷新任务状态；不会盲目重试提交。", "error");
    updateSubmitButton();
  }
  function handleSubmissionResult(data, submissionRevision, submissionPlan) {
    if (data && typeof data.submission_id === "string" && data.submission_id) submissionRecoveryId = data.submission_id;
    var status = data && data.status;
    submissionRecoveryStatus = status || "unknown";
    if (status === "accepted") return acceptedSubmission(data, submissionRevision, submissionPlan);
    if (status === "rejected") return rejectedSubmission(data);
    if (status === "checking") {
      submitBusy = false;
      uncertainSubmit = true;
      persistSubmissionMarker(submissionRecoveryId, "checking");
      setStatus(data.progress ? "提交请求已记录 · " + preflightProgressText(data).replace(/^正在预检/, "正在确认本次提交") : "提交请求已记录，正在确认归档任务状态…", "");
      scheduleSubmissionPoll(submissionRecoveryId, submissionRevision, submissionPlan, 1000);
      updateSubmitButton();
      return;
    }
    unknownSubmission(data);
  }
  function scheduleSubmissionPoll(submissionId, submissionRevision, submissionPlan, delay) {
    if (!submissionId || submissionPollTimer || submissionPollActive) return;
    var pollToken = submissionPollToken;
    submissionPollTimer = window.setTimeout(async function () {
      submissionPollTimer = null;
      if (pollToken !== submissionPollToken || submissionId !== submissionRecoveryId) return;
      submissionPollActive = true;
      try {
        var data = await requestJson("/api/submissions/" + encodeURIComponent(submissionId), { headers: { "X-Muli-Request": "1" } }, 15000);
        if (pollToken !== submissionPollToken || submissionId !== submissionRecoveryId) return;
        submissionPollActive = false;
        handleSubmissionResult(data, submissionRevision, submissionPlan);
      } catch (error) {
        if (pollToken !== submissionPollToken || submissionId !== submissionRecoveryId) return;
        submissionPollActive = false;
        unknownSubmission({ status: error && error.status === 404 ? "unknown" : "unknown", error: "提交记录暂时无法读取" });
      }
    }, delay || 0);
  }
  async function refreshSubmissionRecovery() {
    if (!submissionRecoveryId || !pageIsHttp || submissionPollActive || submissionPollTimer) return false;
    try {
      var data = await requestJson("/api/submissions/" + encodeURIComponent(submissionRecoveryId), { headers: { "X-Muli-Request": "1" } }, 15000);
      handleSubmissionResult(data, revision, frozenPlan || submissionPlan(review.getPlan()));
      return true;
    } catch (error) {
      unknownSubmission({ status: "unknown", error: error && error.status === 404 ? "提交记录不存在，结果无法确认" : "提交记录暂时无法读取" });
      return false;
    }
  }
  async function submitPlan() {
    if (!ready || !frozenPlan || submitBusy || submissionDone || uncertainSubmit) return;
    if (!validExpiry(ready.expires_at)) {
      ready = null;
      frozenPlan = null;
      writeRecoveryState();
      setStatus("正在重新检查目录和文件，请稍后再提交。", "");
      updateSubmitButton();
      planChanged(true);
      return;
    }
    var submissionRevision = revision;
    var submissionPreviewId = ready.preview_id;
    var submissionPlan = clone(frozenPlan);
    submitBusy = true;
    persistSubmissionMarker(submissionPreviewId, "checking");
    updateSubmitButton();
    setStatus("正在提交归档任务…", "");
    try {
      var data = await requestJson("/api/submissions", { method: "POST", headers: { "Content-Type": "application/json", "X-Muli-Request": "1" }, body: JSON.stringify({ preview_id: submissionPreviewId, decisions: submissionPlan, confirmed: true }) });
      handleSubmissionResult(data, submissionRevision, submissionPlan);
    } catch (error) {
      if (error && error.status === 409) {
        setErrors([String(error.message || "提交请求未确认")], error.issues);
        setStatus("提交请求未确认，正在读取已有提交记录…", "");
        await refreshSubmissionRecovery();
      } else unknownSubmission({ status: "unknown", error: "提交结果暂不明确" });
    }
  }
  function validExpiry(value) {
    var seconds = Number(value);
    return Number.isFinite(seconds) && seconds > Math.floor(Date.now() / 1000);
  }
  function terminal(status) { return ["completed", "partial", "failed", "copied_unverified"].indexOf(status) >= 0; }
  function pollJob(jobId, delay, ownerRevision) {
    if (polling[jobId]) return;
    polling[jobId] = true;
    window.setTimeout(async function () {
      try {
        var data = await requestJson("/api/jobs/" + encodeURIComponent(jobId) + "?view=summary", { headers: { "X-Muli-Request": "1" } });
        renderJob(data);
        if (!terminal(data.status)) { polling[jobId] = false; pollJob(jobId, 2000, ownerRevision); }
        else {
          delete polling[jobId];
          var notifyCompletion = ownerRevision === undefined || ownerRevision === revision;
          await refreshArchiveState();
          if (!archiveStateBlocked && notifyCompletion) {
            if (data.status === "completed") setStatus("归档完成：" + summaryOutcomeText(data.summary, data) + "。", "success");
            else {
              var summary = data.summary || {};
              setStatus("归档任务已结束：" + statusText(data.status, data) + "。已完成 " + finiteNumber(summary.completed_files) + " / " + finiteNumber(summary.total_files) + " 个文件；请查看下方原因，并在最近归档任务中处理。", data.status === "failed" ? "error" : "");
              var errors = Array.isArray(data.errors) ? data.errors.filter(function (error) { return typeof error === "string" && error; }).slice(0, 5) : [];
              setErrors(errors.length ? errors : ["未返回具体原因，请查看最近归档任务的详细记录。"], data.issues);
            }
          }
        }
      } catch (error) {
        delete polling[jobId];
        setStatus("任务状态暂时无法读取，请点击刷新任务状态。", "error");
      }
    }, delay || 0);
  }
  async function refreshJobs(options) {
    if (!pageIsHttp) return;
    var initialLoad = options && options.initial === true;
    var manualRequest = !initialLoad;
    var requestId = manualRequest ? ++refreshRequest : 0;
    if (manualRequest) setRefreshFeedback("");
    var jobsRefreshed = false;
    try {
      var data = await requestJson("/api/jobs?view=summary", { headers: { "X-Muli-Request": "1" } });
      jobsRefreshed = true;
      jobs = Array.isArray(data.jobs) ? data.jobs.slice(0, 20) : [];
      renderJobs();
      jobs.filter(function (job) { return job && !terminal(job.status); }).forEach(function (job) { pollJob(job.job_id, 2000); });
    } catch (error) {
      setStatus("任务列表暂时无法读取，请稍后刷新。", "error");
    }
    if (initialLoad) {
      if (submissionRecoveryId && uncertainSubmit) await refreshSubmissionRecovery();
      refreshPreflightCheck();
      return;
    }
    var archiveRefreshed = await refreshArchiveState();
    if (submissionRecoveryId && uncertainSubmit) await refreshSubmissionRecovery();
    refreshPreflightCheck();
    if (manualRequest && jobsRefreshed && archiveRefreshed && requestId === refreshRequest) setRefreshFeedback("已刷新");
  }
  async function retryJob(job) {
    if (!job || !job.job_id || retrying[job.job_id]) return;
    retrying[job.job_id] = true;
    renderJobs();
    try {
      var data = await requestJson("/api/jobs/" + encodeURIComponent(job.job_id) + "/retry", { method: "POST", headers: { "Content-Type": "application/json", "X-Muli-Request": "1" }, body: JSON.stringify({ confirmed: true }) });
      renderJob(data);
      setStatus("失败部分已重新进入归档队列。", "success");
      pollJob(data.job_id, 0);
    } catch (error) { setStatus("重试请求失败，请刷新任务状态后再试。", "error"); }
    finally { delete retrying[job.job_id]; renderJobs(); }
  }
  function restoreSessionRecovery() {
    if ((!initialRecovery && !initialSubmissionRecovery) || !review || typeof review.getPlan !== "function") return false;
    var currentPlan = submissionPlan(review.getPlan());
    var currentFingerprint = planFingerprintFor(currentPlan);
    var storedSubmission = initialSubmissionRecovery || (initialRecovery && initialRecovery.submission);
    if (storedSubmission && typeof storedSubmission.submission_id === "string" && storedSubmission.submission_id) {
      submissionRecoveryId = storedSubmission.submission_id;
      submissionRecoveryStatus = typeof storedSubmission.status === "string" ? storedSubmission.status : "unknown";
      submissionRecoveryPlanFingerprint = typeof storedSubmission.plan_fingerprint === "string" ? storedSubmission.plan_fingerprint : initialRecovery.plan_fingerprint;
      uncertainSubmit = submissionRecoveryStatus !== "rejected";
    }
    if (initialRecovery && initialRecovery.plan_fingerprint !== currentFingerprint) {
      // Keep the current fingerprint as the recovery baseline. The initial
      // page pass must stop at the manual retry gate instead of immediately
      // launching a new preflight for a changed plan.
      planFingerprint = currentFingerprint;
      if (uncertainSubmit) {
        setStatus("上次提交结果仍未确认，且当前计划已变化；请刷新任务状态后再继续。", "error");
        setPreflightRetryVisible(false);
      } else {
        setStatus("当前计划已变化，旧预检结果不会沿用；请手动重新检查。", "error");
        setPreflightRetryVisible(true);
      }
      return false;
    }
    if (!initialRecovery && uncertainSubmit) {
      planFingerprint = currentFingerprint;
      setStatus("上次提交结果仍未确认，请刷新任务状态；当前不会自动重新检查或提交。", "error");
      setPreflightRetryVisible(false);
      updateSubmitButton();
      return false;
    }
    var hasPreflightRecovery = !!initialRecovery && validPreflightCheckId(initialRecovery.check_id) &&
      initialRecovery.check_revision === initialRecovery.revision &&
      ["queued", "checking"].indexOf(initialRecovery.preflight_status) >= 0;
    planFingerprint = currentFingerprint;
    preflightCheckId = initialRecovery && validPreflightCheckId(initialRecovery.check_id) ? initialRecovery.check_id : null;
    preflightCheckRevision = preflightCheckId && initialRecovery.check_revision === initialRecovery.revision ? initialRecovery.revision : 0;
    preflightStatus = initialRecovery && typeof initialRecovery.preflight_status === "string" ? initialRecovery.preflight_status : "checking";
    var storedPlan = initialRecovery && initialRecovery.frozen_plan;
    var storedPlanMatches = storedPlan && planFingerprintFor(storedPlan) === currentFingerprint;
    preflightPlan = storedPlanMatches ? clone(storedPlan) : clone(currentPlan);
    var storedReady = initialRecovery && initialRecovery.ready;
    var storedTicketId = storedReady && typeof storedReady.preview_id === "string" ? storedReady.preview_id :
      (initialRecovery && typeof initialRecovery.confirmation_ticket_id === "string" ? initialRecovery.confirmation_ticket_id : null);
    var hasDurableReady = !!storedReady && !!storedTicketId && validExpiry(storedReady.expires_at) && storedPlanMatches && !uncertainSubmit;
    preflightRecoveryPending = !!preflightCheckId && preflightCheckRevision === revision;
    if (preflightRecoveryPending) {
      ready = null;
      frozenPlan = null;
      setStatus("正在恢复本次预检，请稍候…", "");
      setPreflightRetryVisible(false);
    }
    if (uncertainSubmit) {
      cancelPreflightPoll();
      preflightRecoveryPending = false;
      preflightCheckId = null;
      preflightCheckRevision = 0;
      setStatus("上次提交结果仍未确认，请刷新任务状态；当前不会自动重新检查或提交。", "error");
      setPreflightRetryVisible(false);
      return false;
    }
    if (hasPreflightRecovery) {
      return preflightRecoveryPending;
    }
    if (hasDurableReady) {
      confirmationRecoveryTicketId = storedTicketId;
      ready = clone(storedReady);
      frozenPlan = clone(preflightPlan);
      return requestConfirmationTicket(storedTicketId, preflightPlan);
    }
    if (initialRecovery && (storedReady || initialRecovery.confirmation_manual_required)) {
      preflightNeedsRestart = true;
      preflightPollFailed = true;
      confirmationRecoveryManualRequired = true;
      ready = null;
      frozenPlan = null;
      setErrors(["已保存的归档计划已过期或不完整，请重新检查当前计划。"]);
      setStatus("已保存的归档计划已过期或不完整，请点击“重新检查”。", "error");
      setPreflightRetryVisible(true);
      writeRecoveryState();
      updateSubmitButton();
    }
    updateSubmitButton();
    return false;
  }
  function createPanel() {
    var main = document.querySelector("main.page");
    if (!main) return;
    var panel = make("section", "panel");
    panel.id = "archive-submit-panel";
    panel.appendChild(make("h2", "", "提交归档"));
    panel.appendChild(make("p", "muted", "仅处理已确认素材；待处理和暂缓的不提交。每次提交前选择归档方式和现有文件处理方式。"));
    var options = make("div", "archive-submit-options");
    options.id = "archive-submit-options";
    var mode = make("fieldset");
    mode.appendChild(make("legend", "", "归档方式"));
    mode.appendChild(optionChoice("mode", "copy", "复制归档（保留中转原片）", true, false));
    mode.appendChild(optionChoice("mode", "move", "同卷直接移动归档（条件不符时停止，不自动复制）", false, !moveEnabled));
    options.appendChild(mode);
    var existing = make("fieldset");
    existing.appendChild(make("legend", "", "现有文件"));
    existing.appendChild(optionChoice("existing", "skip_identical", "相同文件跳过，重名冲突自动编号", true, false));
    existing.appendChild(optionChoice("existing", "error", "遇到任何现有文件即停止", false, false));
    existing.appendChild(make("p", "muted", "相同文件须文件名、大小、内容校验都一致。同名但内容不同，会保留两份并添加 _1、_2 等编号。"));
    options.appendChild(existing);
    if (!moveEnabled) options.appendChild(make("p", "muted", "移动归档已被服务设置禁用，本次仅可复制归档。"));
    panel.appendChild(options);
    var facts = make("dl", "facts");
    facts.id = "archive-submit-summary";
    panel.appendChild(facts);
    panel.appendChild(make("div", "archive-submit-projects"));
    panel.lastChild.id = "archive-submit-projects";
    var fileActions = make("div", "archive-submit-file-actions");
    fileActions.id = "archive-submit-file-actions";
    fileActions.hidden = true;
    panel.appendChild(fileActions);
    var status = make("div", "notice", "正在准备提交预检…");
    status.id = "archive-submit-status";
    panel.appendChild(status);
    var errors = make("div", "warning-box");
    errors.id = "archive-submit-errors";
    panel.appendChild(errors);
    var actions = make("div", "archive-submit-actions");
    var submit = make("button", "primary", "提交并开始归档");
    submit.type = "button";
    submit.id = "archive-submit-button";
    submit.addEventListener("click", submitPlan);
    actions.appendChild(submit);
    var preflightRetry = make("button", "secondary", "重新检查");
    preflightRetry.type = "button";
    preflightRetry.id = "archive-submit-preflight-retry";
    preflightRetry.hidden = true;
    preflightRetry.addEventListener("click", retryPreflight);
    actions.appendChild(preflightRetry);
    var refresh = make("button", "secondary", "刷新任务状态");
    refresh.type = "button";
    refresh.id = "archive-submit-refresh";
    refresh.addEventListener("click", refreshJobs);
    actions.appendChild(refresh);
    var refreshStatus = make("span", "muted", "");
    refreshStatus.id = "archive-submit-refresh-status";
    refreshStatus.setAttribute("role", "status");
    refreshStatus.setAttribute("aria-live", "polite");
    refreshStatus.setAttribute("aria-atomic", "true");
    actions.appendChild(refreshStatus);
    panel.appendChild(actions);
    panel.appendChild(make("h3", "", "最近归档任务"));
    var jobList = make("div", "archive-submit-jobs");
    jobList.id = "archive-submit-jobs";
    panel.appendChild(jobList);
    var exportButton = byId("export-plan");
    var localPlan = exportButton && exportButton.closest ? exportButton.closest("section") : null;
    if (localPlan && localPlan.parentNode === main) main.insertBefore(panel, localPlan);
    else main.appendChild(panel);
  }
  createPanel();
  renderPlanSummary(review && review.getPlan ? review.getPlan() : null);
  renderJobs();
  restoreSessionRecovery();
  if (!pageIsHttp) setStatus("请从归档工作台打开，此预览页仍可保存草稿和导出计划。", "error");
  if (review && typeof review.getPlan === "function") window.addEventListener("muli-review-change", planChanged);
  if (review && typeof review.getPlan === "function") window.addEventListener("muli-review-archive-state", function () { renderPlanSummary(review.getPlan()); });
  if (pageIsHttp) { refreshJobs({ initial: true }); planChanged(); }
  updateSubmitButton();
})();
