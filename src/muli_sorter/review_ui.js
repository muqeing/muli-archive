/* Offline confirmation page. The renderer supplies the JSON model in #review-model. */
(function () {
  "use strict";

  var modelNode = document.getElementById("review-model");
  if (!modelNode) return;

  var model;
  try {
    model = JSON.parse(modelNode.textContent || "{}");
  } catch (error) {
    showGlobalError("模型无法读取，页面未初始化。");
    return;
  }

  var previewNode = document.getElementById("review-previews");
  var previews = Object.create(null);
  if (previewNode) {
    try {
      var parsedPreviews = JSON.parse(previewNode.textContent || "{}");
      if (parsedPreviews && typeof parsedPreviews === "object" && !Array.isArray(parsedPreviews)) previews = parsedPreviews;
    } catch (error) {
      previews = Object.create(null);
    }
  }

  var settings = { project_root_label: "拍摄项目" };
  var settingsNode = document.getElementById("review-settings");
  if (settingsNode) {
    try {
      var parsedSettings = JSON.parse(settingsNode.textContent || "{}");
      if (parsedSettings && typeof parsedSettings === "object" && !Array.isArray(parsedSettings) && typeof parsedSettings.project_root_label === "string" && parsedSettings.project_root_label.trim()) {
        settings.project_root_label = parsedSettings.project_root_label.trim().slice(0, 200);
        settings.submission_enabled = parsedSettings.submission_enabled === true;
        settings.photo_previews_enabled = parsedSettings.photo_previews_enabled === true;
        settings.photo_preview_state = parsedSettings.photo_preview_state || {};
        settings.archive_routes = parsedSettings.archive_routes || {};
        settings.archive_state = parsedSettings.archive_state && typeof parsedSettings.archive_state === "object" && !Array.isArray(parsedSettings.archive_state) ? parsedSettings.archive_state : {};
        settings.material_state = parsedSettings.material_state && typeof parsedSettings.material_state === "object" && !Array.isArray(parsedSettings.material_state) ? parsedSettings.material_state : {};
      }
    } catch (error) {
      settings = { project_root_label: "拍摄项目" };
    }
  }

  try { window.muliExpandReviewPresentation(model, settings.material_state); }
  catch (error) { showGlobalError("页面索引不完整，请重新加载。"); return; }
  var projects = Array.isArray(model.projects) ? model.projects : [];
  var units = Array.isArray(model.units) ? model.units : [];
  var sourceSegments = Array.isArray(model.initial_segments) ? model.initial_segments : [];
  var unitById = Object.create(null);
  var projectById = Object.create(null);
  units.forEach(function (unit) { unitById[String(unit.unit_id)] = unit; });
  projects.forEach(function (project) { projectById[String(project.project_id)] = project; });

  var materialByUnit = Object.create(null);
  var materialState = null;
  var state = {
    segments: [],
    manualProjects: [],
    manualDrafts: Object.create(null),
    manualOpen: Object.create(null),
    date: "",
    batch: "",
    projectQuery: "",
    visibleUnitLimits: Object.create(null),
    unitDetailsOpen: Object.create(null),
    selectedUnits: Object.create(null),
    selectedSegments: Object.create(null),
    archiveState: { report_id: String(model.report_id || ""), archived_units: [], warnings: [] },
    archivedByUnit: Object.create(null),
    retainedByUnit: Object.create(null),
    archiveHistoryLimit: 20,
    materialExceptionLimit: 50,
    companionArchiveSelections: Object.create(null),
    materialPanelOpen: Object.create(null)
  };

  function normalizeMaterialState(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    if (String(raw.version || "") !== "material-triage/1" || String(raw.report_id || "") !== String(model.report_id || "")) return null;
    if (!raw.units || typeof raw.units !== "object" || Array.isArray(raw.units)) return null;
    var normalized = Object.create(null);
    var allowed = ["shoot", "support", "companion", "exception", "discarded"];
    var keys = Object.keys(raw.units);
    for (var index = 0; index < keys.length; index += 1) {
      var unitId = String(keys[index]);
      var entry = raw.units[keys[index]];
      if (!entry || typeof entry !== "object" || Array.isArray(entry) || allowed.indexOf(entry.category) < 0) return null;
      if (!unitById[unitId]) return null;
      var parent = entry.parent_unit_id;
      if (parent !== undefined && parent !== null && (typeof parent !== "string" || !unitById[parent])) return null;
      if (entry.category === "companion" && (!parent || parent === unitId || unitById[parent].kind !== "video")) return null;
      normalized[unitId] = {
        category: entry.category,
        reason_code: String(entry.reason_code || ""),
        reason: String(entry.reason || ""),
        parent_unit_id: parent === undefined || parent === null ? null : String(parent),
        action: entry.action === undefined || entry.action === null ? "" : String(entry.action)
      };
    }
    if (keys.length !== units.length) return null;
    for (var unitIndex = 0; unitIndex < units.length; unitIndex += 1) {
      if (!normalized[String(units[unitIndex] && units[unitIndex].unit_id || "")]) return null;
    }
    return { version: "material-triage/1", report_id: String(model.report_id || ""), units: normalized };
  }

  function materialRole(unitId) {
    var entry = materialByUnit[String(unitId)];
    return entry ? entry.category : "shoot";
  }

  function materialEntry(unitId) { return materialByUnit[String(unitId)] || null; }

  function materialUnitSegmentId(unitId) {
    var text = String(unitId || "");
    var hash = 2166136261;
    for (var index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    var digest = (hash >>> 0).toString(16).padStart(8, "0");
    var prefix = text.replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 92) || "unit";
    return (prefix + "--material-companion-" + digest).slice(0, 120);
  }

  function materialGroupSegmentId(base, role, index) {
    var suffix = "--material-" + role + (index ? "-" + index : "");
    return String(base).slice(0, Math.max(1, 120 - suffix.length)) + suffix;
  }

  function normalizeSegment(segment, index) {
    var ids = Array.isArray(segment && segment.unit_ids) ? segment.unit_ids.map(String) : [];
    return {
      segment_id: String(segment && segment.segment_id || "segment-" + (index + 1)),
      label: String(segment && segment.label || "拍摄段 " + (index + 1)),
      unit_ids: ids,
      project_id: null,
      decision: "pending",
      acknowledge_date_mismatch: false
    };
  }

  function materialSegmentFrom(source, index, role, unitIds, parentSegmentId) {
    var segment = normalizeSegment({
      segment_id: role === "shoot" ? source.segment_id : role === "companion" ? materialUnitSegmentId(unitIds[0]) : materialGroupSegmentId(source.segment_id, role, index),
      label: role === "shoot" ? source.label : (role === "support" ? "设备与系统附属文件" : role === "exception" ? "异常待核对" : role === 'discarded' ? '已弃用（可恢复）' : "跟随附属文件"),
      unit_ids: unitIds
    }, index);
    segment._materialRole = role;
    segment._materialParentSegmentId = parentSegmentId || (role === "shoot" ? segment.segment_id : source.segment_id);
    segment._materialDeferred = role !== "shoot";
    if (role !== "shoot") {
      segment.decision = "deferred";
      segment.project_id = null;
      segment.acknowledge_date_mismatch = false;
    }
    return segment;
  }

  function partitionMaterialSegments(rawSegments) {
    var source = Array.isArray(rawSegments) ? rawSegments : [];
    if (!materialState) return source.map(normalizeSegment);
    var result = [];
    source.forEach(function (rawSegment, segmentIndex) {
      var normalizedSource = normalizeSegment(rawSegment, segmentIndex);
      var shootIds = [];
      var byRole = { support: [], exception: [], companion: [], discarded: [] };
      normalizedSource.unit_ids.forEach(function (unitId) {
        var role = materialRole(unitId);
        if (role === "shoot") shootIds.push(unitId);
        else if (byRole[role]) byRole[role].push(unitId);
      });
      if (shootIds.length) {
        var shoot = materialSegmentFrom(normalizedSource, segmentIndex, "shoot", shootIds, normalizedSource.segment_id);
        if (rawSegment && rawSegment.project_id !== undefined && rawSegment.project_id !== null) shoot.project_id = String(rawSegment.project_id);
        if (rawSegment && ["pending", "confirmed", "deferred"].indexOf(rawSegment.decision) >= 0) shoot.decision = rawSegment.decision;
        if (rawSegment && typeof rawSegment.acknowledge_date_mismatch === "boolean") shoot.acknowledge_date_mismatch = rawSegment.acknowledge_date_mismatch;
        result.push(shoot);
      }
      ["support", "exception", "companion", "discarded"].forEach(function (role) {
        if (!byRole[role].length) return;
        var roleUnits = role === "companion" ? byRole[role].map(function (unitId) { return [unitId]; }) : [byRole[role]];
        roleUnits.forEach(function (roleUnitIds) {
          var internal = materialSegmentFrom(normalizedSource, segmentIndex, role, roleUnitIds, normalizedSource.segment_id);
          if (role === "companion") {
            internal.segment_id = materialUnitSegmentId(roleUnitIds[0]);
            var priorDecision = rawSegment && rawSegment.decision;
            if (["confirmed", "deferred"].indexOf(priorDecision) >= 0 && rawSegment.project_id) {
              internal.project_id = String(rawSegment.project_id);
              internal.decision = priorDecision;
              internal.acknowledge_date_mismatch = priorDecision === "confirmed";
            }
          }
          result.push(internal);
        });
      });
    });
    return result;
  }

  function materialSegments() { return state.segments.filter(function (segment) { return segment._materialRole; }); }
  function materialSegmentForUnit(unitId) {
    return state.segments.filter(function (segment) { return segment.unit_ids.indexOf(String(unitId)) >= 0; })[0] || null;
  }
  function companionSegmentsForParent(parentUnitId) {
    return materialSegments().filter(function (segment) {
      return segment._materialRole === "companion" && segment.unit_ids.some(function (unitId) {
        var entry = materialEntry(unitId);
        return entry && entry.parent_unit_id === String(parentUnitId) && !isArchivedUnit(unitId) && !isRetainedUnit(unitId);
      });
    });
  }

  materialState = normalizeMaterialState(settings.material_state);
  if (model.presentation && !materialState) { showGlobalError("页面来源索引不完整，请重新加载。"); return; }
  if (materialState) {
    materialByUnit = materialState.units;
    state.segments = partitionMaterialSegments(sourceSegments);
  } else {
    state.segments = sourceSegments.map(normalizeSegment);
  }

  function archiveUnitId(entry) {
    return entry && typeof entry.unit_id === "string" ? entry.unit_id : "";
  }
  function archiveEntrySafe(entry) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
    var unitId = archiveUnitId(entry);
    if (!unitId) return null;
    var files = Array.isArray(entry.files) ? entry.files.map(function (file) {
      if (!file || typeof file !== "object" || Array.isArray(file)) return null;
      return { name: String(file.name || "未命名文件"), target_path: String(file.target_path || ""), size_bytes: Number(file.size_bytes) >= 0 ? Number(file.size_bytes) : 0 };
    }).filter(Boolean) : [];
    var project = entry.project && typeof entry.project === "object" && !Array.isArray(entry.project) ? {
      name: String(entry.project.name || "未命名项目"),
      path: String(entry.project.path || "")
    } : { name: "未命名项目", path: "" };
    return {
      unit_id: unitId,
      job_id: String(entry.job_id || ""),
      mode: entry.mode === "move" ? "move" : "copy",
      origin: entry.origin === "existing_project_files" ? "existing_project_files" : "service",
      completed_at: String(entry.completed_at || ""),
      project: project,
      files: files,
      file_count: Number(entry.file_count) >= 0 ? Number(entry.file_count) : files.length,
      in_current_model: entry.in_current_model === true
    };
  }
  function archiveDataSafe(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) return null;
    var retainedIds = data.retained_unit_ids === undefined ? [] : data.retained_unit_ids;
    var retainedCurrentFiles = data.retained_current_files === undefined ? 0 : data.retained_current_files;
    var retainedJobs = data.retained_jobs === undefined ? [] : data.retained_jobs;
    if (!Array.isArray(retainedIds) || new Set(retainedIds).size !== retainedIds.length ||
        retainedIds.some(function (id) { return typeof id !== "string" || !unitById[id]; }) ||
        !Number.isInteger(retainedCurrentFiles) || retainedCurrentFiles < 0 || !Array.isArray(retainedJobs)) return null;
    var normalizedRetainedJobs = retainedJobs.map(function (job) {
      if (!job || typeof job !== "object" || Array.isArray(job) || typeof job.job_id !== "string" || !job.job_id || job.job_id.length > 255 ||
          !Number.isInteger(job.unit_count) || job.unit_count < 0 || !Number.isInteger(job.file_count) || job.file_count < 0 ||
          !Array.isArray(job.project_names) || job.project_names.some(function (name) { return typeof name !== "string" || name.length > 255; }) ||
          job.status !== "copied_unverified" || job.source_preserved !== true || job.content_verified !== false) return null;
      return { job_id: job.job_id, unit_count: job.unit_count, file_count: job.file_count, project_names: job.project_names.slice(),
        status: "copied_unverified", source_preserved: true, content_verified: false };
    });
    if (normalizedRetainedJobs.some(function (job) { return !job; })) return null;
    if (data.version === "archive-summary/1") {
      if (!Array.isArray(data.archived_unit_ids) || new Set(data.archived_unit_ids).size !== data.archived_unit_ids.length ||
          data.archived_unit_ids.some(function (id) { return typeof id !== "string" || !unitById[id]; }) ||
          !Number.isInteger(data.current_files) || data.current_files < data.archived_unit_ids.length ||
          !Number.isInteger(data.history_total) || data.history_total < data.archived_unit_ids.length ||
          typeof data.history_generation !== "string" || !data.history_generation) return null;
      var parentProjects = data.parent_projects || {};
      if (typeof parentProjects !== "object" || Array.isArray(parentProjects)) return null;
      data = Object.assign({}, data, {archived_units:data.archived_unit_ids.map(function (id) { return {unit_id:id,in_current_model:true,project:parentProjects[id]}; })});
    }
    if (!Array.isArray(data.archived_units) || !Array.isArray(data.warnings)) return null;
    var expected = String(model.report_id || "");
    if (String(data.report_id || "") !== expected) return null;
    var entries = Array.isArray(data.archived_units) ? data.archived_units.map(archiveEntrySafe).filter(Boolean) : [];
    var warnings = Array.isArray(data.warnings) ? data.warnings.map(function (warning) { return String(warning); }) : [];
    var byUnit = Object.create(null);
    entries.forEach(function (entry) {
      if (!entry.in_current_model || !unitById[entry.unit_id]) return;
      byUnit[entry.unit_id] = entry;
    });
    if (retainedIds.some(function (id) { return !!byUnit[id]; })) return null;
    var retainedByUnit = Object.create(null);
    retainedIds.forEach(function (id) { retainedByUnit[id] = true; });
    return { report_id: expected, archived_units: entries, warnings: warnings, byUnit: byUnit,
      paged: data.version === "archive-summary/1", current_files:data.current_files,
      history_total:data.history_total, history_generation:data.history_generation,
      retained_unit_ids: retainedIds.slice(), retained_current_files: retainedCurrentFiles,
      retained_jobs: normalizedRetainedJobs, retainedByUnit: retainedByUnit };
  }
  function isArchivedUnit(unitId) { return !!state.archivedByUnit[String(unitId)]; }
  function isRetainedUnit(unitId) { return !!state.retainedByUnit[String(unitId)]; }
  function archivedUnitCount() { return Object.keys(state.archivedByUnit).length; }
  function retainedUnitCount() { return Object.keys(state.retainedByUnit).length; }
  function activeUnitIds(segment) { return segment.unit_ids.filter(function (id) { return !isArchivedUnit(id) && !isRetainedUnit(id); }); }
  function deferredSegmentId(segment, suffix) {
    var base = String(segment.segment_id).slice(0, 96) + "--archived";
    var candidate = base;
    var index = 2;
    if (suffix) candidate = base + "-" + String(suffix);
    while (state.segments.some(function (item) { return item.segment_id === candidate; })) candidate = base + "-" + index++;
    if (candidate.length > 120) candidate = candidate.slice(0, 120);
    return candidate;
  }
  function restoredSegmentId(segment, suffix) {
    var base = String(segment.segment_id).replace(/--archived(?:-\d+)?$/, "").slice(0, 106) + "--restored";
    var candidate = base + (suffix ? "-" + String(suffix) : "");
    var index = 2;
    while (state.segments.some(function (item) { return item.segment_id === candidate; })) candidate = base + "-" + index++;
    return candidate.slice(0, 120);
  }
  function retainedSegmentId(segment, suffix) {
    var base = String(segment.segment_id).slice(0, 92) + "--retained";
    var candidate = base + (suffix ? "-" + String(suffix) : "");
    var index = 2;
    while (state.segments.some(function (item) { return item.segment_id === candidate; })) candidate = base + "-" + index++;
    return candidate.slice(0, 120);
  }
  function reconcileArchiveSegments() {
    Object.keys(state.archivedByUnit).forEach(function (unitId) { delete state.selectedUnits[unitId]; });
    Object.keys(state.retainedByUnit).forEach(function (unitId) { delete state.selectedUnits[unitId]; });
    state.segments.forEach(function (segment) {
      if (!segment._archiveDeferred && !segment._retainedDeferred && segment.decision === "deferred" && !segment.project_id && segment.unit_ids.length) {
        if (segment.label.indexOf("已归档 · ") === 0 || segment.unit_ids.every(isArchivedUnit)) segment._archiveDeferred = true;
        else if (segment.label.indexOf("已复制，来源保留（未完整校验） · ") === 0 || segment.unit_ids.every(isRetainedUnit)) segment._retainedDeferred = true;
      }
    });
    var priorExcluded = state.segments.filter(function (segment) { return segment._archiveDeferred || segment._retainedDeferred; });
    var base = state.segments.filter(function (segment) { return !segment._archiveDeferred && !segment._retainedDeferred; });
    var released = [];
    var next = [];
    base.forEach(function (segment) {
      var active = activeUnitIds(segment);
      var archived = segment.unit_ids.filter(function (id) { return isArchivedUnit(id); });
      var retained = segment.unit_ids.filter(function (id) { return isRetainedUnit(id); });
      if (active.length) {
        segment.unit_ids = active;
        next.push(segment);
      }
      if (archived.length) {
        next.push({ segment_id: deferredSegmentId(segment, next.length), label: ("已归档 · " + segment.label).slice(0, 160), unit_ids: archived, project_id: null, decision: "deferred", acknowledge_date_mismatch: false, _archiveDeferred: true });
      }
      if (retained.length) {
        next.push({ segment_id: retainedSegmentId(segment, next.length), label: ("已复制，来源保留（未完整校验） · " + segment.label).slice(0, 160), unit_ids: retained, project_id: null, decision: "deferred", acknowledge_date_mismatch: false, _retainedDeferred: true });
      }
    });
    priorExcluded.forEach(function (segment) {
      var stillArchived = segment.unit_ids.filter(function (id) { return isArchivedUnit(id); });
      var stillRetained = segment.unit_ids.filter(function (id) { return isRetainedUnit(id); });
      segment.unit_ids.forEach(function (id) { if (!isArchivedUnit(id) && !isRetainedUnit(id)) released.push(id); });
      if (stillArchived.length) next.push({ segment_id: deferredSegmentId(segment, next.length), label: ("已归档 · " + segment.label.replace(/^已复制，来源保留（未完整校验） · /, "").replace(/^已归档 · /, "")).slice(0, 160), unit_ids: stillArchived, project_id: null, decision: "deferred", acknowledge_date_mismatch: false, _archiveDeferred: true });
      if (stillRetained.length) next.push({ segment_id: retainedSegmentId(segment, next.length), label: ("已复制，来源保留（未完整校验） · " + segment.label.replace(/^已复制，来源保留（未完整校验） · /, "").replace(/^已归档 · /, "")).slice(0, 160), unit_ids: stillRetained, project_id: null, decision: "deferred", acknowledge_date_mismatch: false, _retainedDeferred: true });
    });
    if (released.length) {
      var restoredIds = released.filter(function (id, index) { return released.indexOf(id) === index; });
      next.push({ segment_id: restoredSegmentId(priorExcluded[0] || { segment_id: "archive" }, next.length), label: "恢复待处理素材", unit_ids: restoredIds, project_id: null, decision: "pending", acknowledge_date_mismatch: false });
    }
    state.segments = next;
    Object.keys(state.selectedSegments).forEach(function (segmentId) {
      if (!state.segments.some(function (segment) { return segment.segment_id === segmentId && !segment._archiveDeferred && !segment._retainedDeferred; })) delete state.selectedSegments[segmentId];
    });
  }

  function byId(id) { return document.getElementById(id); }
  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function attr(node, name, value) { node.setAttribute(name, String(value)); return node; }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function validPreviewSrc(value) {
    if (typeof value !== "string") return false;
    var relative = value.indexOf("../../") === 0 ? value.slice(6) : value;
    return /^(?:video-previews|photo-previews)\/[0-9a-f]{64}\.jpg$/i.test(relative);
  }
  function previewForUnit(unit) {
    var value = previews[String(unit && unit.unit_id || "")];
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  }
  function previewSourceLabel(unit, preview) {
    var fallback = unit && unit.kind === "proxy_only" ? "代理视频" : "主视频";
    return preview && (preview.source_label === "主视频" || preview.source_label === "代理视频") ? preview.source_label : fallback;
  }
  function previewSourceName(preview) {
    return preview && typeof preview.source_name === "string" && preview.source_name ? preview.source_name : "未提供";
  }
  function previewTime(value) {
    var seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "时间未知";
    var whole = Math.floor(seconds);
    return Math.floor(whole / 60) + ":" + String(whole % 60).padStart(2, "0");
  }
  function previewMessage(preview, fallback) {
    return preview && typeof preview.message === "string" && preview.message ? preview.message : fallback;
  }
  var previewOverlay = null;
  var previewOverlayImage = null;
  var previewOverlayMessage = null;
  var previewOverlayReturnFocus = null;
  function closePreviewOverlay() {
    if (!previewOverlay) return;
    previewOverlay.hidden = true;
    if (previewOverlayReturnFocus && typeof previewOverlayReturnFocus.focus === "function") previewOverlayReturnFocus.focus();
    previewOverlayReturnFocus = null;
  }
  function ensurePreviewOverlay() {
    if (previewOverlay) return;
    previewOverlay = make("div", "preview-overlay");
    previewOverlay.hidden = true;
    previewOverlay.tabIndex = -1;
    var dialog = make("div", "preview-dialog");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "preview-dialog-title");
    var title = make("h2", "eyebrow", "素材预览");
    title.id = "preview-dialog-title";
    dialog.appendChild(title);
    var close = make("button", "preview-dialog-close", "关闭");
    close.type = "button";
    close.setAttribute("aria-label", "关闭素材预览");
    close.addEventListener("click", closePreviewOverlay);
    dialog.appendChild(close);
    previewOverlayImage = make("img");
    previewOverlayImage.alt = "放大截图";
    previewOverlayImage.addEventListener("error", function () {
      previewOverlayImage.hidden = true;
      previewOverlayMessage.textContent = "截图加载失败，原素材确认仍可继续。";
    });
    dialog.appendChild(previewOverlayImage);
    previewOverlayMessage = make("p", "preview-dialog-message");
    dialog.appendChild(previewOverlayMessage);
    previewOverlay.appendChild(dialog);
    previewOverlay.addEventListener("click", function (event) {
      if (event.target === previewOverlay) closePreviewOverlay();
    });
    document.body.appendChild(previewOverlay);
    document.addEventListener("keydown", function (event) {
      if (!previewOverlay || previewOverlay.hidden) return;
      if (event.key === "Escape") { event.preventDefault(); closePreviewOverlay(); }
      if (event.key === "Tab") {
        event.preventDefault();
        previewOverlay.querySelector(".preview-dialog-close").focus();
      }
    });
  }
  function openPreviewOverlay(src, alt, caption, trigger) {
    if (!validPreviewSrc(src)) return;
    ensurePreviewOverlay();
    previewOverlayReturnFocus = trigger;
    previewOverlayImage.hidden = false;
    previewOverlayImage.src = src;
    previewOverlayImage.alt = alt;
    previewOverlayMessage.textContent = caption;
    previewOverlay.hidden = false;
    var closeButton = previewOverlay.querySelector(".preview-dialog-close");
    if (closeButton) closeButton.focus();
  }
  function makePreviewCard(frame, sourceLabel, index, unit) {
    var card = make("div", "preview-card");
    if (!frame || !validPreviewSrc(frame.src)) {
      card.appendChild(make("p", "preview-placeholder", "第 " + (index + 1) + " 张截图不可用"));
      return card;
    }
    var trigger = make("button", "preview-trigger");
    trigger.type = "button";
    var img = make("img");
    img.loading = "lazy";
    img.src = frame.src;
    img.alt = String(sourceLabel) + "截图，第 " + (index + 1) + " 张，时间 " + previewTime(frame.time_seconds);
    var failure = make("span", "preview-placeholder", "截图加载失败，确认仍可继续。");
    failure.hidden = true;
    img.addEventListener("error", function () {
      img.hidden = true;
      failure.hidden = false;
      trigger.disabled = true;
      trigger.setAttribute("aria-label", "截图加载失败，确认仍可继续");
    });
    trigger.appendChild(img);
    trigger.appendChild(failure);
    trigger.appendChild(make("span", "preview-caption", previewTime(frame.time_seconds) + " · " + sourceLabel));
    trigger.addEventListener("click", function () {
      openPreviewOverlay(frame.src, img.alt, previewSourceName(previewForUnit(unit)) + " · " + previewTime(frame.time_seconds) + " · " + sourceLabel, trigger);
    });
    card.appendChild(trigger);
    return card;
  }
  function makeUnitPreview(unit) {
    var preview = previewForUnit(unit);
    var sourceLabel = previewSourceLabel(unit, preview);
    var section = make("section", "unit-preview");
    section.setAttribute("aria-label", "视频截图预览");
    var sourceName = previewSourceName(preview);
    section.appendChild(make("div", "preview-heading", "截图 · " + sourceLabel + " · " + sourceName));
    var grid = make("div", "preview-grid");
    var state = preview && preview.state;
    if (state === "error") {
      grid.appendChild(make("p", "preview-placeholder", previewMessage(preview, "截图生成失败；可继续确认拍摄段。")));
    } else if (state !== "ready") {
      grid.appendChild(make("p", "preview-placeholder", previewMessage(preview, "截图尚未生成；可继续确认拍摄段。")));
    } else {
      var frames = preview && Array.isArray(preview.frames) ? preview.frames : [];
      for (var index = 0; index < 3; index += 1) grid.appendChild(makePreviewCard(frames[index], sourceLabel, index, unit));
    }
    section.appendChild(grid);
    return section;
  }
  var photoObserver = typeof IntersectionObserver !== "undefined" ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting && entry.target.isConnected) {
        photoObserver.unobserve(entry.target);
        entry.target.loadPhotos();
      }
    });
  }, { rootMargin: "200px" }) : null;
  var photoMemory = Object.assign(Object.create(null), settings.photo_preview_state || {});
  try {
    var photoReadyNode = document.getElementById("photo-preview-ready");
    if (photoReadyNode) Object.assign(photoMemory, JSON.parse(photoReadyNode.textContent || "{}"));
  } catch (_) { /* Missing display cache uses the bounded preview endpoint. */ }
  var photoNetworkActive = 0;
  function makeSegmentPhotos(segment) {
    var selection = window.muliSamplePhotos(segmentUnits(segment));
    if (!selection.units.length) return null;
    var section = make("section", "segment-photos");
    section.setAttribute("aria-label", "拍摄段照片预览");
    section.dataset.photoIds = selection.units.map(function (u) { return u.unit_id; }).join(",");
    section.appendChild(make("h4", "preview-heading", "照片预览 · " + selection.units.length + " / " + selection.total + " 张"));
    section.appendChild(make("p", "muted", selection.total <= 6 ? "本段照片全部显示，点击可放大。" : (selection.timed ? "沿拍摄时间均匀选取，包含开头与结尾。点击可放大。" : "部分拍摄时间缺失，暂按素材顺序均匀选取。点击可放大。")));
    var grid = make("div", "photo-grid");
    section.appendChild(grid);
    var message = make("p", "muted");
    section.appendChild(message);
    function paint() {
      clear(grid);
      selection.units.forEach(function (unit) {
        var card = make("div", "photo-card");
        card.dataset.photoUnit = unit.unit_id;
        var entry = photoMemory[unit.unit_id];
        var caption = readableDateTime(unit.capture_time) + " · " + (entry && entry.source_name || (unit.file_names || [unit.unit_id])[0]);
        if (entry && entry.state === "ready" && validPreviewSrc(entry.src)) {
          var button = make("button", "photo-trigger");
          button.type = "button";
          button.setAttribute("aria-label", "放大照片：" + caption);
          var img = make("img"); img.src = entry.src; img.alt = caption; img.loading = "lazy";
          img.addEventListener("error", function () { delete photoMemory[unit.unit_id]; img.hidden = true; button.disabled = true; button.appendChild(make("span", "preview-placeholder", "照片预览加载失败")); if (retry) retry.hidden = false; });
          button.appendChild(img);
          button.appendChild(make("span", "photo-caption", caption));
          button.appendChild(make("span", "photo-caption", entry.source_label));
          button.addEventListener("click", function () { openPreviewOverlay(entry.src, caption, caption + " · " + entry.source_label, button); });
          card.appendChild(button);
        } else {
          card.appendChild(make("div", "preview-placeholder", entry && entry.state === "error" ? entry.message : "照片预览准备中…"));
          card.appendChild(make("p", "photo-caption", caption));
        }
        grid.appendChild(card);
      });
    }
    paint();
    if (!settings.photo_previews_enabled) {
      message.textContent = "请在 NAS 归档工作台查看照片预览。";
      return section;
    }
    var tries = 0;
    var photoLoading = false;
    var retry = make("button", "secondary", "重新加载照片预览"); retry.type = "button"; retry.hidden = true;
    section.appendChild(retry);
    retry.addEventListener("click", function () { tries = 0; retry.hidden = true; load(); });
    function load() {
      if (!section.isConnected || photoLoading) return;
      if (selection.units.every(function (u) { return photoMemory[u.unit_id] && photoMemory[u.unit_id].state === "ready"; })) return;
      if (photoNetworkActive >= 2) { setTimeout(load, 150); return; }
      photoNetworkActive += 1;
      photoLoading = true;
      tries += 1;
      var controller = new AbortController();
      var timeout = setTimeout(function () { controller.abort(); }, 15000);
      fetch("api/photo-previews", { method: "POST", credentials: "same-origin", signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-Muli-Request": "1" },
        body: JSON.stringify({ report_id: model.report_id, unit_ids: selection.units.map(function (u) { return u.unit_id; }) })
      }).then(function (response) {
        return response.json().then(function (value) { if (!response.ok) throw new Error(value.error || "预览暂不可用"); return value; });
      }).then(function (value) {
        if (!section.isConnected) return;
        selection.units.forEach(function (u) { photoMemory[u.unit_id] = value.previews[u.unit_id]; });
        paint();
        var pending = selection.units.some(function (u) { return !photoMemory[u.unit_id] || photoMemory[u.unit_id].state === "pending"; });
        var failed = selection.units.some(function (u) { return photoMemory[u.unit_id] && photoMemory[u.unit_id].state === "error"; });
        message.textContent = pending ? "正在生成本段照片预览，可先继续确认其他拍摄段。" : "";
        if (pending && tries < 90) setTimeout(load, 2000);
        else if (pending || failed) { retry.hidden = false; message.textContent = pending ? "照片预览仍在处理中，可稍后重新加载。" : "部分照片暂时无法预览，原素材不受影响。"; }
      }).catch(function (error) {
        if (!section.isConnected) return;
        message.textContent = error.name === "AbortError" ? "预览请求暂时超时，已准备好的照片会保留，可稍后重新加载。" : "照片预览未加载：" + error.message;
        retry.hidden = false;
      }).finally(function () { clearTimeout(timeout); photoLoading = false; photoNetworkActive -= 1; });
    }
    section.loadPhotos = load;
    if (photoObserver) photoObserver.observe(section); else setTimeout(load, 0);
    return section;
  }

  function showGlobalError(message) {
    var target = byId("review-status");
    if (target) { target.textContent = message; target.className = "notice error"; }
  }
  function setStatus(message, kind) {
    var target = byId("review-status");
    if (!target) return;
    target.textContent = message;
    target.className = "notice " + (kind || "");
  }
  function allDateValues() {
    var values = Object.create(null);
    units.forEach(function (unit) {
      var date = unit && unit.capture_date;
      if (date) values[String(date)] = true;
    });
    return Object.keys(values).sort();
  }
  function unitDate(unit) {
    return unit && unit.capture_date || "";
  }
  function captureLabel(unit) {
    var date = unit && unit.capture_date || "日期未知";
    var time = unit && unit.capture_time ? String(unit.capture_time).replace("T", " ") : "时间未知";
    return date + " · " + time;
  }
  function readableDateTime(value) {
    var parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return String(value || "未提供");
    return parsed.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }
  function humanBytes(value) {
    var amount = Number(value);
    if (!Number.isFinite(amount) || amount < 0) amount = 0;
    var units = ["B", "KB", "MB", "GB", "TB"], index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return (index ? amount.toFixed(1) : Math.round(amount)) + " " + units[index];
  }
  function kindLabel(kind) {
    return ({ photo: "照片", video: "主视频", audio: "录音", proxy_only: "代理视频", auxiliary: "辅助文件" })[kind] || kind || "类型未知";
  }
  function segmentUnits(segment) {
    return segment.unit_ids.map(function (id) { return unitById[id]; }).filter(Boolean);
  }
  function segmentSortKey(segment) {
    var times = segmentUnits(segment).map(function (unit) {
      var parsed = new Date(unit.capture_time || (unit.capture_date ? unit.capture_date + "T00:00:00" : ""));
      return parsed.getTime();
    }).filter(function (time) { return Number.isFinite(time); });
    return times.length ? Math.min.apply(Math, times) : Number.POSITIVE_INFINITY;
  }
  function segmentDateInfo(segment, manualProjects) {
    var known = Object.create(null);
    var unknown = false;
    segmentUnits(segment).forEach(function (unit) {
      var date = unitDate(unit);
      if (date) known[date] = true; else unknown = true;
    });
    var dates = Object.keys(known).sort();
    var project = segment.project_id ? projectForIdWithList(segment.project_id, manualProjects || state.manualProjects) : null;
    var projectDates = project && Array.isArray(project.dates) ? project.dates.map(String) : [];
    var mismatch = false;
    if (project && projectDates.length) {
      mismatch = dates.some(function (date) { return projectDates.indexOf(date) < 0; });
    }
    if (project && !projectDates.length && dates.length) mismatch = true;
    return { dates: dates, unknown: unknown, mismatch: mismatch, needsAck: unknown || mismatch };
  }
  function unitMatchesBatch(unit) {
    return !state.batch || !!(unit && Array.isArray(unit.provenance) && unit.provenance.some(function (r) { return r.batch_id === state.batch; }));
  }
  function visible(segment) {
    return segmentUnits(segment).some(function (unit) { return unitMatchesBatch(unit) && (!state.date || unitDate(unit) === state.date); });
  }
  function projectMatches(project) {
    var query = state.projectQuery.trim().toLowerCase();
    if (!query) return true;
    return [project.name, project.order_id]
      .concat(Array.isArray(project.dates) ? project.dates : [])
      .some(function (value) { return String(value || "").toLowerCase().indexOf(query) >= 0; });
  }
  function normalizeNfc(value) {
    var text = String(value == null ? "" : value);
    return typeof text.normalize === "function" ? text.normalize("NFC") : text;
  }
  function manualNameKey(name) { return normalizeNfc(name).toLowerCase(); }
  function pathKey(path) { return normalizeNfc(path).toLowerCase(); }
  function manualPath(name, shootDate) {
    var month = String(Number(shootDate.slice(5, 7)));
    return shootDate.slice(0, 4) + "/" + month + "月/" + shootDate.replace(/-/g, "") + "_自建_" + name;
  }
  function displayPath(path) {
    return settings.project_root_label + "/" + path;
  }
  function manualProjectMetadata(descriptor) {
    return {
      project_id: descriptor.project_id,
      name: "【待新建】" + descriptor.name,
      path: manualPath(descriptor.name, descriptor.shoot_date),
      dates: [descriptor.shoot_date],
      order_id: null,
      exists: false,
      evidence_source: "manual_no_order",
      folder_action: "create_manual_after_confirmed_assignment"
    };
  }
  function manualProjectList() { return state.manualProjects.map(manualProjectMetadata); }
  function projectForId(projectId) {
    if (projectById[projectId]) return projectById[projectId];
    var descriptor = state.manualProjects.filter(function (item) { return item.project_id === projectId; })[0];
    return descriptor ? manualProjectMetadata(descriptor) : null;
  }
  function projectForIdWithList(projectId, manualProjects) {
    if (projectById[projectId]) return projectById[projectId];
    var descriptor = (Array.isArray(manualProjects) ? manualProjects : []).filter(function (item) { return item && item.project_id === projectId; })[0];
    return descriptor ? manualProjectMetadata(descriptor) : null;
  }
  function validShootDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    var year = Number(value.slice(0, 4));
    if (year < 2000 || year > 2099) return false;
    var month = Number(value.slice(5, 7));
    var day = Number(value.slice(8, 10));
    var parsed = new Date(Date.UTC(year, month - 1, day));
    return month >= 1 && month <= 12 && day >= 1 && parsed.getUTCFullYear() === year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
  }
  function utf8Length(value) {
    if (typeof TextEncoder === "function") return new TextEncoder().encode(value).length;
    return unescape(encodeURIComponent(value)).length;
  }
  function validManualName(value) {
    if (typeof value !== "string") return { ok: false, message: "项目名必须是文本。" };
    var name = normalizeNfc(value).trim();
    if (!name) return { ok: false, message: "项目名不能为空。" };
    if (name === "." || name === "..") return { ok: false, message: "项目名不能是 . 或 ..。" };
    if (Array.from(name).length > 80 || utf8Length(name) > 200) return { ok: false, message: "项目名太长，请缩短后再试。" };
    if (/[\/\\<>:"|?*]/.test(name)) return { ok: false, message: "项目名不能包含 / \\ < > : \" | ? *。" };
    for (var index = 0; index < name.length; index += 1) {
      var code = name.charCodeAt(index);
      if ((code <= 0x1f) || (code >= 0x7f && code <= 0x9f) || (code >= 0x200b && code <= 0x200f) || (code >= 0x202a && code <= 0x202e) || (code >= 0x2060 && code <= 0x206f) || code === 0xfeff) {
        return { ok: false, message: "项目名包含控制符、不可见字符或双向文本控制符。" };
      }
    }
    if (/[.\s]$/.test(name)) return { ok: false, message: "项目名不能以点或空白结尾。" };
    return { ok: true, value: name };
  }
  function manualDescriptorValid(descriptor, seenIds, errors) {
    if (!descriptor || typeof descriptor !== "object" || Array.isArray(descriptor)) { errors.push("手动项目描述无效"); return false; }
    var keys = Object.keys(descriptor).sort();
    if (keys.join("|") !== "name|project_id|shoot_date|unit_ids") { errors.push("手动项目字段不符"); return false; }
    if (typeof descriptor.project_id !== "string" || !/^manual-[0-9a-f]{32}$/.test(descriptor.project_id) || (seenIds && seenIds[descriptor.project_id])) errors.push("手动项目编号无效或重复");
    if (seenIds) seenIds[descriptor.project_id] = true;
    var nameCheck = validManualName(descriptor.name);
    if (!nameCheck.ok || nameCheck.value !== descriptor.name) errors.push(nameCheck.ok ? "手动项目名必须已规范化" : nameCheck.message);
    if (!validShootDate(descriptor.shoot_date)) errors.push("手动项目日期必须是 2000—2099 年的 YYYY-MM-DD");
    if (!Array.isArray(descriptor.unit_ids) || !descriptor.unit_ids.length || descriptor.unit_ids.some(function (id) { return typeof id !== "string" || !id || !unitById[id]; }) || new Set(descriptor.unit_ids).size !== descriptor.unit_ids.length) errors.push("手动项目素材单元无效或重复");
    return !errors.length;
  }
  function manualDuplicateError(name, shootDate, ignoreProjectId) {
    var key = manualNameKey(name) + "\u0000" + shootDate;
    var planned = state.manualProjects.some(function (descriptor) { return descriptor.project_id !== ignoreProjectId && manualNameKey(descriptor.name) + "\u0000" + descriptor.shoot_date === key; });
    if (planned) return "已在本地计划中，请在候选项目中选择。";
    var targetPath = pathKey(manualPath(name, shootDate));
    if (projects.some(function (project) { return pathKey(project && project.path || "") === targetPath; })) return "生成的项目路径与现有项目冲突。";
    if (state.manualProjects.some(function (descriptor) { return descriptor.project_id !== ignoreProjectId && pathKey(manualPath(descriptor.name, descriptor.shoot_date)) === targetPath; })) return "生成的项目路径与待新建项目冲突。";
    return "";
  }
  function projectLabel(project) {
    var dates = Array.isArray(project.dates) && project.dates.length ? project.dates.join(", ") : "日期未登记";
    return String(project.name || "未命名项目") + " · " + dates;
  }
  function segmentCandidateIds(segment) {
    var candidateIds = Object.create(null);
    segmentUnits(segment).forEach(function (unit) {
      if (Array.isArray(unit.candidate_project_ids)) unit.candidate_project_ids.forEach(function (id) { candidateIds[String(id)] = true; });
    });
    state.manualProjects.forEach(function (descriptor) {
      if (descriptor.unit_ids.some(function (id) { return segment.unit_ids.indexOf(id) >= 0; }) || segmentUnits(segment).some(function (unit) { return unitDate(unit) === descriptor.shoot_date; })) candidateIds[descriptor.project_id] = true;
    });
    return candidateIds;
  }
  function isSegmentCandidate(segment, projectId) {
    return !!projectId && !!projectForId(projectId) && !!segmentCandidateIds(segment)[projectId];
  }
  function isSegmentCandidateForList(segment, projectId, manualProjects) {
    if (!projectId || !projectForIdWithList(projectId, manualProjects)) return false;
    if (segmentUnits(segment).some(function (unit) { return Array.isArray(unit.candidate_project_ids) && unit.candidate_project_ids.map(String).indexOf(projectId) >= 0; })) return true;
    var descriptor = (Array.isArray(manualProjects) ? manualProjects : []).filter(function (item) { return item && item.project_id === projectId; })[0];
    return !!descriptor && (descriptor.unit_ids.some(function (id) { return segment.unit_ids.indexOf(id) >= 0; }) || segmentUnits(segment).some(function (unit) { return unitDate(unit) === descriptor.shoot_date; }));
  }
  function projectOptions(select, selected, segment) {
    var candidateIds = segmentCandidateIds(segment);
    var candidates = projects.filter(function (project) { return !!candidateIds[String(project.project_id)]; }).concat(manualProjectList().filter(function (project) { return !!candidateIds[String(project.project_id)]; }));
    var matches = candidates.filter(projectMatches);
    if (selected && candidateIds[selected] && projectForId(selected) && matches.every(function (project) { return String(project.project_id) !== selected; })) matches.unshift(projectForId(selected));
    var placeholder = !candidates.length ? "此拍摄段暂无候选项目" : !matches.length ? "候选项目中没有搜索结果" : "选择本段候选项目（必须显式确认）";
    var empty = make("option", "", placeholder);
    empty.value = "";
    select.appendChild(empty);
    select.disabled = !candidates.length;
    matches.forEach(function (project) {
      var label = projectLabel(project);
      var option = make("option", "", label);
      option.value = String(project.project_id || "");
      if (selected && option.value === selected) option.selected = true;
      select.appendChild(option);
    });
    return candidates.length;
  }
  function statusText(decision) {
    return decision === "confirmed" ? "已确认" : decision === "deferred" ? "已暂缓" : "待处理";
  }

  function archiveProjectId(entry) {
    if (!entry || !entry.project || typeof entry.project.path !== "string") return null;
    var archivePath = entry.project.path;
    var found = projects.filter(function (project) {
      if (!project || typeof project.path !== "string") return false;
      var expected = String(settings.project_root_label || "拍摄项目") + "/" + project.path.replace(/^\/+/, "");
      return archivePath === expected;
    })[0];
    return found ? String(found.project_id) : null;
  }

  function archivedParentEntry(parentUnitId) {
    return state.archiveState.archived_units.filter(function (entry) {
      return entry.in_current_model && entry.unit_id === String(parentUnitId);
    })[0] || null;
  }

  function syncCompanionSegments() {
    if (!materialState) return;
    materialSegments().filter(function (segment) { return segment._materialRole === "companion"; }).forEach(function (segment) {
      if (segment._retainedDeferred || segment.unit_ids.some(isRetainedUnit)) {
        segment.project_id = null;
        segment.decision = "deferred";
        segment.acknowledge_date_mismatch = false;
        return;
      }
      var parentIds = [];
      segment.unit_ids.forEach(function (unitId) {
        var entry = materialEntry(unitId);
        if (entry && entry.parent_unit_id && parentIds.indexOf(entry.parent_unit_id) < 0) parentIds.push(entry.parent_unit_id);
      });
      var inherited = null;
      if (parentIds.some(isRetainedUnit)) {
        segment.project_id = null;
        segment.decision = "deferred";
        segment.acknowledge_date_mismatch = false;
        return;
      }
      parentIds.some(function (parentId) {
        var archivedEntry = archivedParentEntry(parentId);
        if (archivedEntry) {
          var archivedProject = archiveProjectId(archivedEntry);
          if (state.companionArchiveSelections[segment.segment_id] && archivedProject) inherited = archivedProject;
          return !!inherited;
        }
        var parentSegment = state.segments.filter(function (candidate) {
          return candidate._materialRole !== "companion" && candidate.unit_ids.indexOf(parentId) >= 0;
        })[0];
        if (parentSegment && parentSegment.decision === "confirmed" && parentSegment.project_id) {
          inherited = String(parentSegment.project_id);
          return true;
        }
        return false;
      });
      if (inherited) {
        segment.project_id = inherited;
        segment.decision = "confirmed";
        segment.acknowledge_date_mismatch = true;
      } else {
        segment.project_id = null;
        segment.decision = "deferred";
        segment.acknowledge_date_mismatch = false;
      }
    });
  }

  function materialUnitLabel(unit) {
    var names = unit && Array.isArray(unit.file_names) && unit.file_names.length ? unit.file_names.join("、") : ((unit && unit.unit_id) || "未命名素材");
    return String(names) + " · " + String((unit && unit.device) || "设备未知");
  }

  function materialGroups(role) {
    var groups = Object.create(null);
    materialSegments().filter(function (segment) { return segment._materialRole === role; }).forEach(function (segment) {
      segment.unit_ids.forEach(function (unitId) {
        if (isArchivedUnit(unitId) || isRetainedUnit(unitId) || !unitMatchesBatch(unitById[unitId])) return;
        var entry = materialEntry(unitId) || {};
        var key = String(entry.reason_code || "unknown");
        if (!groups[key]) groups[key] = { reason_code: key, reason: String(entry.reason || "待核对原因"), action: String(entry.action || ""), units: [] };
        var unit = unitById[unitId];
        groups[key].units.push(unit || { unit_id: unitId });
      });
    });
    return Object.keys(groups).map(function (key) { return groups[key]; }).sort(function (left, right) { return left.reason_code.localeCompare(right.reason_code); });
  }

  function makeMaterialGroup(group, limit, offset) {
    var item = make("div", "material-triage-group");
    item.appendChild(make("strong", "", group.reason + " · " + group.units.length + " 个素材"));
    if (group.action) item.appendChild(make("span", "material-triage-note", "处理建议：" + group.action));
    group.units.slice(offset || 0, (offset || 0) + limit).forEach(function (unit) {
      item.appendChild(make("span", "muted", materialUnitLabel(unit)));
    });
    return item;
  }

  function makeMaterialPanel(role, title, note) {
    var groups = materialGroups(role);
    if (!groups.length) return null;
    var details = make("details");
    details.open = !!state.materialPanelOpen[role];
    details.addEventListener("toggle", function () { state.materialPanelOpen[role] = details.open; });
    var total = groups.reduce(function (sum, group) { return sum + group.units.length; }, 0);
    details.appendChild(make("summary", "", title + "（" + total + " 个素材）"));
    details.appendChild(make("p", "material-triage-note", note));
    var list = make("div", "material-triage-list");
    var remaining = role === "exception" ? state.materialExceptionLimit : Number.POSITIVE_INFINITY;
    groups.forEach(function (group) {
      var count = Math.min(group.units.length, remaining);
      if (count > 0) list.appendChild(makeMaterialGroup(group, count, 0));
      remaining -= count;
    });
    details.appendChild(list);
    if (role === "exception" && total > state.materialExceptionLimit) {
      var more = make("button", "secondary", "再显示 50 个异常素材");
      more.type = "button";
      more.addEventListener("click", function () {
        state.materialPanelOpen[role] = true;
        state.materialExceptionLimit += 50;
        renderMaterialPanels();
      });
      details.appendChild(more);
    }
    return details;
  }

  function renderCompanionArchivePanel(container) {
    var rows = [];
    materialSegments().filter(function (segment) { return segment._materialRole === "companion"; }).forEach(function (segment) {
      segment.unit_ids.forEach(function (unitId) {
        if (isArchivedUnit(unitId) || isRetainedUnit(unitId) || !unitMatchesBatch(unitById[unitId])) return;
        var entry = materialEntry(unitId);
        if (!entry || !entry.parent_unit_id) return;
        var parent = archivedParentEntry(entry.parent_unit_id);
        if (!parent) return;
        rows.push({ segment: segment, unit: unitById[unitId], parent: parent, projectId: archiveProjectId(parent) });
      });
    });
    if (!rows.length) return;
    var details = make("details");
    details.open = !!state.materialPanelOpen.companionArchive;
    details.addEventListener("toggle", function () { state.materialPanelOpen.companionArchive = details.open; });
    details.appendChild(make("summary", "", "附属文件待补齐（" + rows.length + " 个素材）"));
    details.appendChild(make("p", "material-triage-note", "主视频已归档；请勾选本次要补齐的附属文件。项目按归档路径精确匹配。"));
    var list = make("div", "material-triage-list");
    rows.forEach(function (row) {
      var label = make("label", "unit-check");
      var checkbox = make("input"); checkbox.type = "checkbox";
      checkbox.checked = !!state.companionArchiveSelections[row.segment.segment_id];
      checkbox.disabled = !row.projectId;
      checkbox.addEventListener("change", function () {
        if (checkbox.checked) state.companionArchiveSelections[row.segment.segment_id] = true;
        else delete state.companionArchiveSelections[row.segment.segment_id];
        syncCompanionSegments(); notifyPlanChange();
      });
      label.appendChild(checkbox);
      label.appendChild(make("span", "unit-content", materialUnitLabel(row.unit) + (row.projectId ? " · 项目路径已匹配" : " · 需刷新候选")));
      list.appendChild(label);
    });
    details.appendChild(list);
    var actions = make("div", "material-triage-action");
    var add = make("button", "primary", "加入本次归档"); add.type = "button";
    add.addEventListener("click", function () { syncCompanionSegments(); render(); setStatus("已将勾选的附属文件加入本次归档计划；尚未执行归档。", "success"); });
    actions.appendChild(add);
    var undo = make("button", "secondary", "撤销"); undo.type = "button";
    undo.addEventListener("click", function () {
      rows.forEach(function (row) { delete state.companionArchiveSelections[row.segment.segment_id]; });
      syncCompanionSegments(); render(); setStatus("已撤销附属文件补齐选择。", "success");
    });
    actions.appendChild(undo);
    details.appendChild(actions);
    container.appendChild(details);
  }

  function renderCompanionOrphanPanel(container) {
    var rows = [];
    materialSegments().filter(function (segment) { return segment._materialRole === "companion"; }).forEach(function (segment) {
      segment.unit_ids.forEach(function (unitId) {
        if (isArchivedUnit(unitId) || isRetainedUnit(unitId) || !unitMatchesBatch(unitById[unitId])) return;
        var entry = materialEntry(unitId);
        if (!entry || !entry.parent_unit_id || archivedParentEntry(entry.parent_unit_id)) return;
        var parentShoot = state.segments.some(function (candidate) {
          return (candidate._materialRole === "shoot" || !candidate._materialRole) && candidate.unit_ids.indexOf(entry.parent_unit_id) >= 0;
        });
        if (!parentShoot) rows.push({ unit: unitById[unitId] });
      });
    });
    if (!rows.length) return;
    var details = make("details");
    details.open = !!state.materialPanelOpen.companionOrphan;
    details.addEventListener("toggle", function () { state.materialPanelOpen.companionOrphan = details.open; });
    details.appendChild(make("summary", "", "附属文件待核对（" + rows.length + " 个素材）"));
    details.appendChild(make("p", "material-triage-warning", "等待主视频恢复/核对；当前不选择项目归属。"));
    var list = make("div", "material-triage-list");
    rows.slice(0, 50).forEach(function (row) { list.appendChild(make("span", "muted", materialUnitLabel(row.unit))); });
    details.appendChild(list);
    container.appendChild(details);
  }

  function renderMaterialPanels() {
    var container = byId("material-triage");
    if (!container) return;
    clear(container);
    if (!materialState) { container.hidden = true; return; }
    var refresh = null;
    if (settings.submission_enabled === true) refresh = make("button", "secondary", "重新检查来源");
    if (refresh) refresh.type = "button";
    if (refresh) refresh.addEventListener("click", function () {
      refresh.disabled = true;
      fetch("/api/material-state?report_id=" + encodeURIComponent(String(model.report_id || "")), { method: "GET", credentials: "same-origin", headers: { "X-Muli-Request": "1" } }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (value) {
          if (!response.ok) { var error = new Error(response.status === 404 ? "来源检查接口暂不可用。" : response.status === 409 ? "来源状态已变化，请稍后重新打开报告。" : "来源检查失败。"); error.status = response.status; throw error; }
          return value;
        });
      }).then(function (value) {
        if (!applyMaterialState(value)) return;
        setStatus("来源状态已重新检查；当前选择和草稿计划已保留。", "success");
      }).catch(function (error) {
        setStatus(error && error.message ? error.message + "当前选择未改变。" : "来源检查失败，当前选择未改变。", "error");
      }).finally(function () { refresh.disabled = false; });
    });
    if (refresh) container.appendChild(refresh);
    syncCompanionSegments();
    var support = makeMaterialPanel("support", "设备与系统附属文件", "已识别为设备或系统附属文件；无需选择项目归属。具体原因和名称仅供核对。");
    var exception = makeMaterialPanel("exception", "异常待核对", "来源不可用、孤立或不明文件单独暂缓；无需选择项目归属。");
    if (support) container.appendChild(support);
    if (exception) container.appendChild(exception);
    renderCompanionOrphanPanel(container);
    renderCompanionArchivePanel(container);
    var discarded = materialGroups('discarded').reduce(function (sum, group) { return sum + group.units.length; }, 0);
    if (discarded) container.appendChild(make('p', 'muted', '已弃用 ' + discarded + ' 个素材，已从待处理列表隐藏；文件保留在可恢复弃用区。'));
    if (Array.isArray(settings.material_state.warnings) && settings.material_state.warnings.length) container.appendChild(make('p', 'warning', settings.material_state.warnings.map(function (warning) { return typeof warning === 'string' ? warning : String(warning.reason || '弃用记录暂时无法核对'); }).join('；')));
    container.hidden = !container.childNodes.length;
  }
  function batchStatusText(status) {
    return ({ waiting: "等待拷贝完成", blocked: "已阻塞", verified: "已核验", copied_unverified: "已复制，来源保留（未完整校验）" })[status] || status || "已排除";
  }
  function makeUnitCard(unit, selected) {
    var item = make("li", "unit-row");
    attr(item, "data-unit-id", unit.unit_id);
    if (isRetainedUnit(unit.unit_id)) {
      item.className += " retained-unit-stub";
      item.appendChild(make("span", "unit-content", captureLabel(unit) + " · 已复制，来源保留（未完整校验）"));
      return item;
    }
    var label = make("label", "unit-check");
    var checkbox = make("input");
    checkbox.type = "checkbox";
    checkbox.checked = !!selected;
    checkbox.dataset.unitId = String(unit.unit_id);
    checkbox.addEventListener("change", function () {
      if (checkbox.checked) state.selectedUnits[unit.unit_id] = true;
      else delete state.selectedUnits[unit.unit_id];
      updateActionState();
    });
    label.appendChild(checkbox);
    var content = make("span", "unit-content");
    var title = make("strong", "unit-title", captureLabel(unit) + " · " + (unit.device || "设备未知") + " · " + kindLabel(unit.kind));
    content.appendChild(title);
    var detail = make("span", "unit-detail", "文件 " + (unit.file_count || 0) + " · " + humanBytes(unit.bytes));
    content.appendChild(detail);
    var route = (settings.archive_routes || {})[unit.unit_id];
    if (route && (!materialState || materialRole(unit.unit_id) !== "shoot" || !route.blocked)) content.appendChild(make("span", route.blocked ? "warning" : "unit-detail", route.blocked ? "暂缓归档：" + route.reason : "归档去向：" + route.folders.join("、") + " · " + route.reason));
    if (!unit.capture_date) content.appendChild(make("span", "warning", "日期未知，需核对"));
    if (Array.isArray(unit.warnings)) unit.warnings.forEach(function (warning) { content.appendChild(make("span", "warning", warning)); });
    if (Array.isArray(unit.file_names) && unit.file_names.length) content.appendChild(make("span", "muted", unit.file_names.join("、")));
    if (Array.isArray(unit.candidate_project_ids) && unit.candidate_project_ids.length) {
      var candidateLabels = unit.candidate_project_ids.map(function (id) {
        var project = projectById[String(id)];
        return project ? projectLabel(project) : "候选项目待核对";
      });
      content.appendChild(make("span", "muted", "候选项目：" + candidateLabels.join("、")));
    }
    if (Array.isArray(unit.provenance) && unit.provenance.length > 1) content.appendChild(make("span", "muted", "来源重复引用：" + unit.provenance.length + " 条（已合并）"));
    label.appendChild(content);
    item.appendChild(label);
    if (unit.kind === "video" || unit.kind === "proxy_only") item.appendChild(makeUnitPreview(unit));
    return item;
  }
  function manualDraftFor(segment) {
    var draft = state.manualDrafts[segment.segment_id];
    if (draft) return draft;
    var info = segmentDateInfo(segment);
    draft = { name: "", shoot_date: info.dates.length === 1 && !info.unknown && validShootDate(info.dates[0]) ? info.dates[0] : "", error: "" };
    state.manualDrafts[segment.segment_id] = draft;
    return draft;
  }
  function makeManualProjectForm(segment) {
    var draft = state.manualDrafts[segment.segment_id];
    if (!draft) {
      var info = segmentDateInfo(segment);
      draft = { name: "", shoot_date: info.dates.length === 1 && !info.unknown && validShootDate(info.dates[0]) ? info.dates[0] : "", error: "" };
      if (state.manualOpen[segment.segment_id]) state.manualDrafts[segment.segment_id] = draft;
    }
    var form = make("div", "manual-project-form");
    form.hidden = !state.manualOpen[segment.segment_id];
    form.dataset.manualForm = segment.segment_id;
    var title = make("strong", "", "新建无订单项目（仅生成本地计划）");
    form.appendChild(title);
    var grid = make("div", "manual-project-grid");
    var nameLabel = make("label", "field-label", "项目名");
    var nameInput = make("input");
    nameInput.type = "text";
    nameInput.maxLength = 80;
    nameInput.autocomplete = "off";
    nameInput.value = draft.name;
    nameInput.dataset.manualName = segment.segment_id;
    nameLabel.appendChild(nameInput);
    grid.appendChild(nameLabel);
    var dateLabel = make("label", "field-label", "拍摄日期");
    var dateInput = make("input");
    dateInput.type = "date";
    dateInput.min = "2000-01-01";
    dateInput.max = "2099-12-31";
    dateInput.value = draft.shoot_date;
    dateInput.dataset.manualDate = segment.segment_id;
    dateLabel.appendChild(dateInput);
    grid.appendChild(dateLabel);
    form.appendChild(grid);
    var preview = make("p", "manual-project-preview");
    function updatePreview() {
      draft.name = nameInput.value;
      draft.shoot_date = dateInput.value;
      draft.error = "";
      if (errorNode) errorNode.textContent = "";
      var check = validManualName(draft.name);
      if (check.ok && validShootDate(draft.shoot_date)) {
        preview.textContent = "计划路径：" + displayPath(manualPath(check.value, draft.shoot_date)) + " · 状态：待新建";
      } else {
        preview.textContent = "计划路径：填写合法项目名和拍摄日期后显示 · 状态：待新建";
      }
    }
    nameInput.addEventListener("input", updatePreview);
    dateInput.addEventListener("input", updatePreview);
    form.appendChild(preview);
    var errorNode = make("p", "manual-project-error");
    errorNode.setAttribute("role", "alert");
    errorNode.textContent = draft.error || "";
    form.appendChild(errorNode);
    var actions = make("div", "manual-project-actions");
    var create = make("button", "primary", "加入本段计划");
    create.type = "button";
    create.dataset.createManual = segment.segment_id;
    create.addEventListener("click", function () { createManualProject(segment, draft, errorNode); });
    actions.appendChild(create);
    var cancel = make("button", "secondary", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", function () {
      delete state.manualDrafts[segment.segment_id];
      state.manualOpen[segment.segment_id] = false;
      renderSegment(segment);
    });
    actions.appendChild(cancel);
    form.appendChild(actions);
    updatePreview();
    return form;
  }
  function createManualProject(segment, draft, errorNode) {
    var nameCheck = validManualName(draft.name);
    if (!nameCheck.ok) { errorNode.textContent = nameCheck.message; draft.error = nameCheck.message; return; }
    var shootDate = String(draft.shoot_date || "");
    if (!validShootDate(shootDate)) { errorNode.textContent = "拍摄日期必须是 2000—2099 年的有效日期。"; draft.error = errorNode.textContent; return; }
    var duplicate = manualDuplicateError(nameCheck.value, shootDate, null);
    if (duplicate) { errorNode.textContent = duplicate; draft.error = duplicate; return; }
    var descriptor = { project_id: newManualProjectId(), name: nameCheck.value, shoot_date: shootDate, unit_ids: segment.unit_ids.slice() };
    var descriptorErrors = [];
    manualDescriptorValid(descriptor, Object.create(null), descriptorErrors);
    if (descriptorErrors.length) { errorNode.textContent = descriptorErrors[0]; draft.error = descriptorErrors[0]; return; }
    state.manualProjects.push(descriptor);
    segment.project_id = descriptor.project_id;
    segment.decision = "pending";
    segment.acknowledge_date_mismatch = false;
    delete state.manualDrafts[segment.segment_id];
    state.manualOpen[segment.segment_id] = false;
    render();
    setStatus("已加入本段待新建项目计划；仍需显式确认归属，未创建文件夹。", "success");
  }
  function newManualProjectId() {
    var bytes = [];
    if (window.crypto && typeof window.crypto.getRandomValues === "function") {
      var buffer = new Uint8Array(16);
      window.crypto.getRandomValues(buffer);
      bytes = Array.prototype.slice.call(buffer);
    } else {
      for (var index = 0; index < 16; index += 1) bytes.push(Math.floor(Math.random() * 256));
    }
    var hex = bytes.map(function (value) { return value.toString(16).padStart(2, "0"); }).join("").slice(0, 32);
    while (hex.length < 32) hex += "0";
    return "manual-" + hex;
  }
  function makeCompactVideos(allUnits) {
    var videos = allUnits.filter(function (unit) { return unit.kind === "video" || unit.kind === "proxy_only"; });
    if (!videos.length) return null;
    var strip = make("div", "segment-videos");
    var count = Math.min(3, videos.length);
    strip.appendChild(make("p", "preview-heading", "视频概览 · 按素材顺序选取 " + count + " / " + videos.length + " 个，更多截图可展开明细查看"));
    for (var index = 0; index < count; index += 1) {
      var position = count === 1 ? 0 : Math.round(index * (videos.length - 1) / (count - 1));
      strip.appendChild(makeUnitPreview(videos[position]));
    }
    return strip;
  }
  function makeUnitDetails(segment, allUnits, compactVideos) {
    var details = make("details", "unit-details");
    details.dataset.unitDetails = segment.segment_id;
    details.open = !!state.unitDetailsOpen[segment.segment_id];
    var summary = make("summary");
    summary.dataset.detailsToggle = segment.segment_id;
    details.appendChild(summary);
    var fileCount = allUnits.reduce(function (sum, unit) { return sum + (Number(unit.file_count) || (unit.file_names || []).length); }, 0);
    var warningCount = allUnits.filter(function (unit) { return (unit.warnings || []).length; }).length;
    details.updateSummary = function () {
      var selectedCount = allUnits.filter(function (unit) { return !!state.selectedUnits[unit.unit_id]; }).length;
      summary.textContent = (details.open ? "收起素材明细" : "展开素材明细") + " · " + allUnits.length + " 个素材 / " + fileCount + " 个文件" + (selectedCount ? " · 已勾选 " + selectedCount + " 个待拆分" : "") + (warningCount ? " · " + warningCount + " 个素材有提示" : "");
    };
    var populated = false;
    function populate() {
      if (populated) return;
      populated = true;
      details.appendChild(make("p", "muted", "勾选仅用于拆分素材；确认归属会覆盖本段全部 " + allUnits.length + " 个素材，不受展开数量影响。"));
      var unitsList = make("ul", "unit-list");
      var shownCount = Math.min(allUnits.length, state.visibleUnitLimits[segment.segment_id] || 50);
      allUnits.slice(0, shownCount).forEach(function (unit) { unitsList.appendChild(makeUnitCard(unit, state.selectedUnits[unit.unit_id])); });
      details.appendChild(unitsList);
      if (allUnits.length > 50) {
        var detailCount = make("p", "muted");
        detailCount.dataset.detailCount = segment.segment_id;
        var more = make("button", "secondary", "再显示 50 个素材");
        more.type = "button";
        more.dataset.moreUnits = segment.segment_id;
        function updateDetails() {
          detailCount.textContent = "已显示 " + shownCount + " / " + allUnits.length + " 个素材。";
          more.hidden = shownCount >= allUnits.length;
        }
        more.addEventListener("click", function () {
          var nextCount = Math.min(allUnits.length, shownCount + 50);
          allUnits.slice(shownCount, nextCount).forEach(function (unit) { unitsList.appendChild(makeUnitCard(unit, state.selectedUnits[unit.unit_id])); });
          shownCount = nextCount;
          state.visibleUnitLimits[segment.segment_id] = shownCount;
          updateDetails();
        });
        updateDetails();
        details.appendChild(detailCount);
        details.appendChild(more);
      }
      var close = make("button", "secondary", "收起素材明细");
      close.type = "button";
      close.dataset.closeDetails = segment.segment_id;
      close.addEventListener("click", function () {
        details.open = false;
        sync();
        summary.focus({ preventScroll: true });
        summary.scrollIntoView({ block: "nearest" });
      });
      details.appendChild(close);
    }
    function sync() {
      state.unitDetailsOpen[segment.segment_id] = details.open;
      if (details.open) populate();
      if (compactVideos) compactVideos.hidden = details.open;
      details.updateSummary();
    }
    details.addEventListener("toggle", function () { if (details.isConnected) sync(); });
    sync();
    return details;
  }
  function makeSegmentCard(segment) {
    var card = make("article", "segment-card");
    attr(card, "data-segment-id", segment.segment_id);
    var info = segmentDateInfo(segment);
    var head = make("div", "segment-head");
    var mergeLabel = make("label", "merge-check");
    var merge = make("input");
    merge.type = "checkbox";
    merge.dataset.segmentId = segment.segment_id;
    merge.checked = !!state.selectedSegments[segment.segment_id];
    merge.addEventListener("change", function () {
      if (merge.checked) state.selectedSegments[segment.segment_id] = true;
      else delete state.selectedSegments[segment.segment_id];
      updateActionState();
    });
    mergeLabel.appendChild(merge);
    mergeLabel.appendChild(make("span", "muted", "选择合并"));
    head.appendChild(mergeLabel);
    var heading = make("div", "segment-title");
    heading.appendChild(make("h3", "", segment.label));
    heading.appendChild(make("span", "status status-" + segment.decision, statusText(segment.decision)));
    head.appendChild(heading);
    card.appendChild(head);
    var meta = make("p", "segment-meta", "素材 " + segment.unit_ids.length + " 个 · " + (info.dates.length ? info.dates.join(", ") : "日期未知"));
    card.appendChild(meta);
    if (materialState) {
      var videoIds = segment.unit_ids.filter(function (unitId) { return unitById[unitId] && (unitById[unitId].kind === "video" || unitById[unitId].kind === "proxy_only"); });
      var companionSeen = Object.create(null);
      var companionCount = videoIds.reduce(function (sum, videoId) {
        return sum + companionSegmentsForParent(videoId).reduce(function (inner, companion) {
          if (companionSeen[companion.segment_id]) return inner;
          companionSeen[companion.segment_id] = true;
          return inner + companion.unit_ids.length;
        }, 0);
      }, 0);
      if (companionCount) card.appendChild(make("p", "material-triage-note", "跟随附属文件 " + companionCount + " 个；主视频确认后将继承同一项目。"));
    }
    if (info.unknown) card.appendChild(make("p", "warning-box", "此段含日期未知素材，确认前必须勾选已核对。"));
    if (info.mismatch) card.appendChild(make("p", "warning-box", "此段素材日期与所选项目不一致，确认前必须勾选跨日期核对。"));

    var photoStrip = makeSegmentPhotos(segment);
    if (photoStrip) card.appendChild(photoStrip);
    var allUnits = segmentUnits(segment);
    var compactVideos = makeCompactVideos(allUnits);
    if (compactVideos) card.appendChild(compactVideos);
    var projectLine = make("div", "project-line");
    var projectLabelNode = make("label", "field-label", "项目归属");
    var select = make("select");
    select.dataset.segmentId = segment.segment_id;
    select.dataset.projectSelect = "true";
    var candidateCount = projectOptions(select, segment.project_id, segment);
    select.addEventListener("change", function () {
      var next = select.value || null;
      if (next !== segment.project_id || segment.decision === "confirmed") {
        segment.decision = "pending";
        segment.acknowledge_date_mismatch = false;
      }
      segment.project_id = next;
      renderSegment(segment);
    });
    projectLabelNode.appendChild(select);
    projectLine.appendChild(projectLabelNode);
    var manualButton = make("button", "secondary", "新建无订单项目");
    manualButton.type = "button";
    manualButton.dataset.openManual = segment.segment_id;
    manualButton.addEventListener("click", function () {
      state.manualOpen[segment.segment_id] = true;
      manualDraftFor(segment);
      var updated = renderSegment(segment);
      var nameInput = updated && updated.querySelector("[data-manual-name]");
      if (nameInput) nameInput.focus({ preventScroll: true });
    });
    projectLine.appendChild(manualButton);
    card.appendChild(projectLine);
    card.appendChild(make("p", "muted", candidateCount ? "本段有 " + candidateCount + " 个候选项目；搜索仅在这些候选中筛选。也可准备一个待新建无订单项目。" : "尚无候选项目；可准备一个待新建无订单项目。这里先保存待建项目；执行时由程序创建文件夹。"));
    card.appendChild(makeManualProjectForm(segment));
    var selectedProject = segment.project_id ? projectForId(segment.project_id) : null;
    if (selectedProject && selectedProject.evidence_source === "manual_no_order") {
      card.appendChild(make("p", "pending-project", "项目状态：待新建 · 计划路径：" + displayPath(selectedProject.path) + " · 不存在文件夹，不会在此页面创建。"));
    }

    card.appendChild(makeUnitDetails(segment, allUnits, compactVideos));
    var actions = make("div", "segment-actions");
    if (info.needsAck) {
      var ackLabel = make("label", "ack-label");
      var ack = make("input");
      ack.type = "checkbox";
      ack.checked = !!segment.acknowledge_date_mismatch;
      ack.dataset.ackSegmentId = segment.segment_id;
      ack.addEventListener("change", function () {
        segment.acknowledge_date_mismatch = ack.checked;
        if (!ack.checked && segment.decision === "confirmed") segment.decision = "pending";
        renderSegment(segment);
      });
      ackLabel.appendChild(ack);
      ackLabel.appendChild(make("span", "", info.mismatch ? "我已核对跨日期归属" : "我已核对日期未知素材"));
      actions.appendChild(ackLabel);
    }
    var confirm = make("button", "primary", allUnits.length > 50 ? "确认整段归属" : "确认归属");
    confirm.type = "button";
    confirm.dataset.confirmSegment = segment.segment_id;
    var routeBlocked = allUnits.some(function (unit) { var route = (settings.archive_routes || {})[unit.unit_id]; return route && route.blocked && (!materialState || materialRole(unit.unit_id) !== "shoot"); });
    if (routeBlocked) {
      var routeReasons = allUnits.map(function (unit) { var route = (settings.archive_routes || {})[unit.unit_id]; return route && route.blocked ? route.reason : ""; }).filter(Boolean);
      card.appendChild(make("p", "warning-box", "本段有待核对的来源关系：" + routeReasons.join("；")));
    }
    confirm.disabled = !isSegmentCandidate(segment, segment.project_id) || (info.needsAck && !segment.acknowledge_date_mismatch) || routeBlocked;
    confirm.addEventListener("click", function () { confirmSegment(segment); });
    actions.appendChild(confirm);
    var defer = make("button", "secondary", "暂缓处理");
    defer.type = "button";
    defer.dataset.deferSegment = segment.segment_id;
    defer.addEventListener("click", function () { segment.decision = "deferred"; renderSegment(segment); setStatus("已暂缓此段；如需保留，请点击保存草稿。", "success"); });
    actions.appendChild(defer);
    if (segment.decision === "confirmed" || segment.decision === "deferred") {
      var withdraw = make("button", "link-button", "撤回确认");
      withdraw.type = "button";
      withdraw.dataset.withdrawSegment = segment.segment_id;
      withdraw.addEventListener("click", function () {
        segment.project_id = null;
        segment.decision = "pending";
        segment.acknowledge_date_mismatch = false;
        renderSegment(segment);
        setStatus("已撤回此段状态，请重新选择并显式确认。", "success");
      });
      actions.appendChild(withdraw);
    }
    card.appendChild(actions);
    return card;
  }
  function confirmSegment(segment) {
    if (!isSegmentCandidate(segment, segment.project_id)) {
      setStatus("请先为此段选择一个候选项目，再点击确认归属。", "error");
      return;
    }
    var info = segmentDateInfo(segment);
    if (info.needsAck && !segment.acknowledge_date_mismatch) {
      setStatus("日期不一致或未知，必须先勾选核对后才能确认。", "error");
      return;
    }
    segment.decision = "confirmed";
    renderSegment(segment);
    setStatus(settings.submission_enabled ? "已确认此段归属；请在“提交归档”中核对范围并提交。" : "已确认此段归属；仍需导出计划才会生成本地文件。", "success");
  }
  function splitSelected() {
    var selected = Object.keys(state.selectedUnits);
    if (!selected.length) { setStatus("请先勾选要拆分的素材单元。", "error"); return; }
    var selectedSet = Object.create(null);
    selected.forEach(function (id) { selectedSet[id] = true; });
    var additions = [];
    state.segments.forEach(function (segment) {
      var picked = segment.unit_ids.filter(function (id) { return selectedSet[id]; });
      if (!picked.length || picked.length === segment.unit_ids.length) return;
      segment.unit_ids = segment.unit_ids.filter(function (id) { return !selectedSet[id]; });
      segment.project_id = null; segment.decision = "pending"; segment.acknowledge_date_mismatch = false;
      additions.push({ segment_id: newSegmentId("split", additions), label: segment.label + " · 拆分", unit_ids: picked, project_id: null, decision: "pending", acknowledge_date_mismatch: false });
    });
    if (!additions.length) { setStatus("选中的素材必须只占某个拍摄段的一部分才能拆分。", "error"); return; }
    state.segments = state.segments.concat(additions);
    state.selectedUnits = Object.create(null);
    state.selectedSegments = Object.create(null);
    render();
    setStatus("已按不可拆素材单元拆分拍摄段，原确认已清除。", "success");
  }
  function mergeSelected() {
    var ids = Object.keys(state.selectedSegments);
    if (ids.length < 2) { setStatus("请至少选择两个拍摄段后再合并。", "error"); return; }
    var chosen = state.segments.filter(function (segment) { return ids.indexOf(segment.segment_id) >= 0; });
    var unitIds = [];
    chosen.forEach(function (segment) { segment.unit_ids.forEach(function (id) { if (unitIds.indexOf(id) < 0) unitIds.push(id); }); });
    state.segments = state.segments.filter(function (segment) { return ids.indexOf(segment.segment_id) < 0; });
    state.segments.push({ segment_id: newSegmentId("merged"), label: "合并拍摄段", unit_ids: unitIds, project_id: null, decision: "pending", acknowledge_date_mismatch: false });
    state.selectedSegments = Object.create(null);
    state.selectedUnits = Object.create(null);
    render();
    setStatus("已合并拍摄段；合并后的归属需要重新选择并确认。", "success");
  }
  function newSegmentId(prefix, reserved) {
    var base = prefix + "-" + Date.now().toString(36);
    var id = base, n = 2;
    reserved = reserved || [];
    while (state.segments.some(function (segment) { return segment.segment_id === id; }) || reserved.some(function (segment) { return segment.segment_id === id; })) id = base + "-" + n++;
    return id;
  }
  function validatePlan(candidateSegments, candidateManualProjects) {
    var segments = candidateSegments || state.segments;
    var manualProjects = candidateManualProjects || state.manualProjects;
    var seenSegments = Object.create(null);
    var seenUnits = Object.create(null);
    var errors = [];
    var sourceUnitIds = Object.create(null);
    units.forEach(function (unit) {
      var id = String(unit && unit.unit_id || "");
      if (!id || sourceUnitIds[id]) errors.push("模型素材单元标识重复或为空");
      sourceUnitIds[id] = true;
    });
    var sourceProjectIds = Object.create(null);
    projects.forEach(function (project) {
      var id = String(project && project.project_id || "");
      if (!id || sourceProjectIds[id]) errors.push("模型项目标识重复或为空");
      sourceProjectIds[id] = true;
    });
    var manualIds = Object.create(null);
    var manualNameKeys = Object.create(null);
    var manualPathKeys = Object.create(null);
    if (!Array.isArray(manualProjects)) errors.push("手动项目列表无效");
    (Array.isArray(manualProjects) ? manualProjects : []).forEach(function (descriptor) {
      var descriptorErrors = [];
      manualDescriptorValid(descriptor, manualIds, descriptorErrors);
      descriptorErrors.forEach(function (message) { errors.push(message); });
      if (descriptor && typeof descriptor === "object" && typeof descriptor.name === "string" && validShootDate(descriptor.shoot_date)) {
        var nameKey = manualNameKey(descriptor.name) + "\u0000" + descriptor.shoot_date;
        var generatedPathKey = pathKey(manualPath(descriptor.name, descriptor.shoot_date));
        if (manualNameKeys[nameKey]) errors.push("手动项目名称和日期重复");
        if (manualPathKeys[generatedPathKey]) errors.push("手动项目路径重复");
        if (projects.some(function (project) { return pathKey(project && project.path || "") === generatedPathKey; })) errors.push("手动项目路径与现有项目冲突");
        manualNameKeys[nameKey] = true;
        manualPathKeys[generatedPathKey] = true;
      }
    });
    Object.keys(manualIds).forEach(function (id) { if (sourceProjectIds[id]) errors.push("手动项目编号与现有项目冲突"); });
    segments.forEach(function (segment) {
      if (typeof segment.segment_id !== "string" || !segment.segment_id || segment.segment_id.length > 120 || seenSegments[segment.segment_id]) errors.push("拍摄段 ID 重复、过长或为空");
      seenSegments[segment.segment_id] = true;
      if (typeof segment.label !== "string" || segment.label.length > 160) errors.push("拍摄段名称过长或无效");
      if (!Array.isArray(segment.unit_ids) || !segment.unit_ids.length) errors.push("拍摄段素材不能为空");
      if (Array.isArray(segment.unit_ids) && new Set(segment.unit_ids).size !== segment.unit_ids.length) errors.push("同一拍摄段内素材重复");
      (Array.isArray(segment.unit_ids) ? segment.unit_ids : []).forEach(function (id) {
        if (typeof id !== "string" || !id || id.length > 255) errors.push("素材单元标识无效");
        if (!unitById[id]) errors.push("包含未知素材单元：" + id);
        if (seenUnits[id]) errors.push("素材单元重复出现：" + id);
        seenUnits[id] = true;
      });
      if (segment.project_id !== null && (typeof segment.project_id !== "string" || !segment.project_id || segment.project_id.length > 255 || !projectForIdWithList(segment.project_id, manualProjects))) errors.push("项目不在当前项目目录中");
      if (segment.project_id !== null && !(materialState && segment._materialRole === "companion") && !isSegmentCandidateForList(segment, segment.project_id, manualProjects)) errors.push("项目不在此拍摄段的候选范围内");
      if (materialState && segment._materialRole && segment._materialRole !== "shoot" && segment._materialRole !== "companion" && (segment.project_id !== null || segment.decision !== "deferred")) errors.push("辅助或异常素材必须保持暂缓且无项目");
      if (["pending", "confirmed", "deferred"].indexOf(segment.decision) < 0 || typeof segment.acknowledge_date_mismatch !== "boolean") errors.push("拍摄段状态无效：" + segment.segment_id);
      if (segment.decision === "confirmed") {
        if (segment.unit_ids.some(function (id) { return unitById[id] && unitById[id]._identityOnly && !isArchivedUnit(id) && !isRetainedUnit(id); })) errors.push("请先刷新页面读取此段完整详情");
        if (!segment.project_id) errors.push("已确认拍摄段没有项目：" + segment.segment_id);
        var info = segmentDateInfo(segment, manualProjects);
        if (info.needsAck && !segment.acknowledge_date_mismatch) errors.push("已确认拍摄段未完成日期核对：" + segment.segment_id);
      }
    });
    units.forEach(function (unit) { if (!seenUnits[unit.unit_id]) errors.push("素材单元未分配：" + unit.unit_id); });
    return errors;
  }
  function exportPlan() {
    var errors = validatePlan();
    if (errors.length) { setStatus("无法导出：" + errors[0], "error"); return; }
    var payload = currentPlan();
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = "classification-plan-" + String(model.report_id || "report").replace(/[^A-Za-z0-9._-]+/g, "_") + ".json";
    document.body.appendChild(link); link.click(); link.remove();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 0);
    setStatus("已发起下载本地归档计划（不执行）；浏览器未确认文件已保存。不会移动原片，也不表示允许真实写入。", "success");
  }
  function currentPlan() {
    syncCompanionSegments();
    return {
      schema_version: "0.3",
      mode: "classification_confirmation_only",
      media_write_authorized: false,
      report_id: model.report_id,
      created_at: new Date().toISOString(),
      segments: state.segments.map(function (segment) {
        return { segment_id: segment.segment_id, label: segment.label, unit_ids: segment.unit_ids.slice(), project_id: segment.project_id || null, decision: segment.decision, acknowledge_date_mismatch: !!segment.acknowledge_date_mismatch };
      }),
      manual_projects: state.manualProjects.map(function (descriptor) {
        return { project_id: descriptor.project_id, name: descriptor.name, shoot_date: descriptor.shoot_date, unit_ids: descriptor.unit_ids.slice() };
      })
    };
  }
  function draftKey() { return "muli-sorter:review-draft:" + String(model.report_id || ""); }
  function setDraftStatus(message, kind) {
    setStatus(message, kind);
    var target = byId("draft-status");
    if (!target) return;
    target.textContent = message;
    target.className = "notice " + (kind || "");
    target.hidden = false;
    var bounds = target.getBoundingClientRect();
    if (bounds.top < 0 || bounds.bottom > window.innerHeight) target.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  function parseHistoricalDraft(raw, key) {
    if (typeof raw !== "string" || raw.length > 8 * 1024 * 1024) throw new Error("草稿内容过大或不可读");
    var value = JSON.parse(raw), total = 0, seen = new Set(), segments = new Set(), manuals = new Set();
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("草稿格式无效");
    var fields = value.schema_version === "0.2" ? "report_id|saved_at|schema_version|segments" : value.schema_version === "0.3" ? "manual_projects|report_id|saved_at|schema_version|segments" : "";
    if (!fields || Object.keys(value).sort().join("|") !== fields || typeof value.report_id !== "string" || !value.report_id || value.report_id.length > 200 || key !== "muli-sorter:review-draft:" + value.report_id) throw new Error("草稿编号或版本不符");
    if (typeof value.saved_at !== "string" || value.saved_at.length > 64 || !Number.isFinite(Date.parse(value.saved_at))) throw new Error("草稿时间无效");
    if (!Array.isArray(value.segments) || value.segments.length > 50000) throw new Error("草稿拍摄段过多");
    value.segments.forEach(function (s) {
      if (!s || Object.keys(s).sort().join("|") !== "acknowledge_date_mismatch|decision|label|project_id|segment_id|unit_ids" || typeof s.segment_id !== "string" || !s.segment_id || s.segment_id.length > 120 || segments.has(s.segment_id) || typeof s.label !== "string" || s.label.length > 160) throw new Error("草稿拍摄段格式无效");
      segments.add(s.segment_id);
      if (["pending", "confirmed", "deferred"].indexOf(s.decision) < 0 || typeof s.acknowledge_date_mismatch !== "boolean" || (s.project_id !== null && (typeof s.project_id !== "string" || !s.project_id || s.project_id.length > 255)) || (s.decision === "confirmed" && !s.project_id)) throw new Error("草稿归属状态无效");
      if (!Array.isArray(s.unit_ids) || !s.unit_ids.length || (total += s.unit_ids.length) > 200000) throw new Error("草稿素材范围无效");
      s.unit_ids.forEach(function (id) { if (typeof id !== "string" || !id || id.length > 255 || seen.has(id)) throw new Error("草稿素材标识无效或重复"); seen.add(id); });
    });
    if (value.schema_version === "0.3") {
      if (!Array.isArray(value.manual_projects) || value.manual_projects.length > Math.max(total, 1)) throw new Error("草稿自建项目过多");
      value.manual_projects.forEach(function (p) {
        if (!p || Object.keys(p).sort().join("|") !== "name|project_id|shoot_date|unit_ids" || typeof p.project_id !== "string" || !/^manual-[0-9a-f]{32}$/.test(p.project_id) || manuals.has(p.project_id)) throw new Error("草稿自建项目编号无效");
        manuals.add(p.project_id);
        var name = validManualName(p.name);
        if (!name.ok || name.value !== p.name || !validShootDate(p.shoot_date) || !Array.isArray(p.unit_ids) || !p.unit_ids.length || p.unit_ids.length > total || new Set(p.unit_ids).size !== p.unit_ids.length || p.unit_ids.some(function (id) { return !seen.has(id); })) throw new Error("草稿自建项目范围无效");
      });
    }
    return value;
  }
  function recoveryFingerprint() {
    var plan = currentPlan();
    return JSON.stringify([plan.segments, plan.manual_projects, materialState, Object.keys(state.archivedByUnit).sort(), Object.keys(state.retainedByUnit).sort()]);
  }
  function prepareHistoricalRecovery(draft) {
    if (settings.submission_enabled && !materialState) throw new Error("当前素材状态尚未核对，暂不能恢复旧报告；请先重新检查来源。");
    var byUnit = Object.create(null), eligible = new Set(), oldIds = new Set(), taken = new Set();
    var counts = { missing: 0, archived: 0, retained: 0, unavailable: 0, protected: 0, matching: 0, projects: 0, unresolved: 0, segments: 0, added: 0, manualConflicts: 0 };
    state.segments.forEach(function (s) { s.unit_ids.forEach(function (id) { byUnit[id] = s; }); });
    draft.segments.forEach(function (s) { s.unit_ids.forEach(function (id) {
      oldIds.add(id);
      if (!unitById[id]) counts.missing += 1;
      else if (isArchivedUnit(id)) counts.archived += 1;
      else if (isRetainedUnit(id)) counts.retained += 1;
      else if (materialRole(id) !== "shoot") counts.unavailable += 1;
      else if (!byUnit[id] || byUnit[id].decision !== "pending" || byUnit[id].project_id) counts.protected += 1;
      else eligible.add(id);
    }); });
    counts.added = units.filter(function (u) { return !oldIds.has(u.unit_id); }).length;
    var recoveredManuals = state.manualProjects.map(function (p) { return Object.assign({}, p, { unit_ids: p.unit_ids.slice() }); });
    var projectMap = Object.create(null), manualIds = Object.create(null);
    recoveredManuals.forEach(function (p) { manualIds[p.project_id] = true; });
    (draft.manual_projects || []).forEach(function (p) {
      projectMap[p.project_id] = null;
      var scope = p.unit_ids.filter(function (id) { return eligible.has(id); });
      var generatedPath = manualPath(p.name, p.shoot_date);
      var matches = projects.filter(function (project) { return pathKey(project.path) === pathKey(generatedPath); });
      if (matches.length === 1 && normalizeNfc(matches[0].path) === generatedPath) { projectMap[p.project_id] = matches[0].project_id; return; }
      if (matches.length || !scope.length || projectById[p.project_id]) { counts.manualConflicts += 1; return; }
      var sameId = recoveredManuals.find(function (x) { return x.project_id === p.project_id; });
      if (sameId && sameId.name === p.name && sameId.shoot_date === p.shoot_date) { projectMap[p.project_id] = sameId.project_id; return; }
      if (sameId || recoveredManuals.some(function (x) { return pathKey(manualPath(x.name, x.shoot_date)) === pathKey(generatedPath); })) { counts.manualConflicts += 1; return; }
      var candidate = { project_id: p.project_id, name: p.name, shoot_date: p.shoot_date, unit_ids: scope }, errors = [];
      if (!manualDescriptorValid(candidate, manualIds, errors)) { counts.manualConflicts += 1; return; }
      recoveredManuals.push(candidate); projectMap[p.project_id] = p.project_id;
    });
    var additions = [], prefix = newSegmentId("recovered") + "-";
    draft.segments.forEach(function (source, index) {
      var ids = source.unit_ids.filter(function (id) { return eligible.has(id); });
      if (!ids.length) return;
      var pid = Object.prototype.hasOwnProperty.call(projectMap, source.project_id) ? projectMap[source.project_id] : source.project_id;
      var s = { segment_id: prefix + index, label: source.label, unit_ids: ids, project_id: null, decision: source.decision === "deferred" ? "deferred" : "pending", acknowledge_date_mismatch: false };
      if (pid && isSegmentCandidateForList(s, pid, recoveredManuals)) { s.project_id = pid; counts.projects += ids.length; }
      else if (source.project_id) counts.unresolved += ids.length;
      ids.forEach(function (id) { taken.add(id); }); additions.push(s);
    });
    var remaining = state.segments.map(function (s) { return Object.assign({}, s, { unit_ids: s.unit_ids.filter(function (id) { return !taken.has(id); }) }); }).filter(function (s) { return s.unit_ids.length; });
    var all = additions.concat(remaining);
    var validation = validatePlan(all, recoveredManuals);
    if (validation.length) throw new Error("恢复计划未通过检查：" + validation[0]);
    counts.matching = taken.size; counts.segments = additions.length;
    return { segments: all, manualProjects: recoveredManuals, additions: additions, counts: counts };
  }
  function downloadDraftBackup(key) {
    try {
      var raw = localStorage.getItem(key); parseHistoricalDraft(raw, key);
      var url = URL.createObjectURL(new Blob([raw], { type: "application/json" })), a = make("a");
      a.href = url; a.download = "muli-review-draft-" + key.slice("muli-sorter:review-draft:".length).replace(/[^A-Za-z0-9._-]/g, "_") + ".json";
      document.body.appendChild(a); a.click(); a.remove(); window.setTimeout(function () { URL.revokeObjectURL(url); }, 0);
      setDraftStatus("已发起旧草稿备份下载；请在浏览器下载列表确认。原草稿仍保留。", "success");
    } catch (error) { setDraftStatus("旧草稿备份未完成：" + error.message, "error"); }
  }
  function previewHistoricalDraft(key) {
    var target = byId("draft-recovery-preview"); clear(target);
    try {
      var raw = localStorage.getItem(key), draft = parseHistoricalDraft(raw, key);
      var fingerprint = recoveryFingerprint(), prepared = prepareHistoricalRecovery(draft), c = prepared.counts;
      target.appendChild(make("h3", "", "恢复内容预览"));
      target.appendChild(make("p", "", "可恢复 " + c.segments + " 段 / " + c.matching + " 个素材的分组，其中 " + c.projects + " 个素材可保留项目选择。"));
      target.appendChild(make("p", "muted", "已归档不恢复 " + c.archived + " 个 · 已复制但未完整校验不恢复 " + c.retained + " 个 · 来源异常或附属素材暂不恢复 " + c.unavailable + " 个 · 不在当前报告 " + c.missing + " 个 · 保留当前已有操作 " + c.protected + " 个 · 新报告额外素材 " + c.added + " 个保持原状态。"));
      var conflicts = [];
      if (c.unresolved) conflicts.push(c.unresolved + " 个素材的项目已不在候选范围");
      if (c.manualConflicts) conflicts.push(c.manualConflicts + " 个自建项目存在范围或目录冲突");
      if (conflicts.length) target.appendChild(make("p", "warning-box", conflicts.join("；") + "，需要重新选择项目。"));
      target.appendChild(make("p", "warning-box", "跨报告恢复只找回分组和项目选择，已确认状态与日期核对需重新确认。旧草稿保留；此操作不会提交归档。"));
      var list = make("ul");
      prepared.additions.slice(0, 20).forEach(function (s) {
        var project = s.project_id && projectForIdWithList(s.project_id, prepared.manualProjects);
        list.appendChild(make("li", "muted", s.label + " · " + s.unit_ids.length + " 个素材 · " + (project ? project.name : "需选择项目") + (s.decision === "deferred" ? " · 保持暂缓" : " · 待确认")));
      });
      target.appendChild(list);
      if (prepared.additions.length > 20) target.appendChild(make("p", "muted", "以上展示前 20 段；恢复范围为全部 " + c.segments + " 段。"));
      var apply = make("button", "primary", "恢复到当前页面（需重新确认）"); apply.type = "button"; apply.id = "draft-recovery-apply"; apply.disabled = !c.matching;
      apply.addEventListener("click", function () {
        try {
          if (localStorage.getItem(key) !== raw || recoveryFingerprint() !== fingerprint) throw new Error("草稿或当前选择已经变化，请重新预览后恢复。");
          var errors = validatePlan(prepared.segments, prepared.manualProjects);
          if (errors.length) throw new Error(errors[0]);
          state.segments = prepared.segments; state.manualProjects = prepared.manualProjects;
          state.manualDrafts = Object.create(null); state.manualOpen = Object.create(null); state.selectedUnits = Object.create(null); state.selectedSegments = Object.create(null);
          reconcileArchiveSegments(); syncCompanionSegments(); render(); apply.disabled = true;
          setDraftStatus("已恢复 " + c.segments + " 段的分组和项目选择；请保存当前草稿，再重新确认归属。原草稿仍保留，尚未执行归档。", "success");
        } catch (error) { setDraftStatus("尚未恢复：" + error.message, "error"); }
      });
      target.appendChild(apply); target.hidden = false; target.scrollIntoView({ block: "nearest" });
    } catch (error) { target.hidden = true; setDraftStatus("无法预览旧草稿：" + error.message, "error"); }
  }
  function showDraftRecovery() {
    var target = byId("draft-recovery"), list = byId("draft-recovery-list"), preview = byId("draft-recovery-preview");
    if (!target || !list) return;
    clear(list); clear(preview); preview.hidden = true; target.hidden = false;
    try {
      var rows = [], invalid = 0, scanned = 0, totalLength = 0, prefix = "muli-sorter:review-draft:";
      if (localStorage.length > 10000) throw new Error("本地存储记录过多，无法完整列出草稿。");
      for (var index = 0; index < localStorage.length; index += 1) {
        var key = localStorage.key(index);
        if (!key || key.indexOf(prefix) !== 0 || key === draftKey()) continue;
        scanned += 1; if (scanned > 100) throw new Error("旧报告草稿超过 100 份，请先保留浏览器记录并联系维护人员。");
        var raw = localStorage.getItem(key);
        totalLength += raw ? raw.length : 0;
        if (totalLength > 16 * 1024 * 1024) throw new Error("旧草稿总量过大，暂不能完整列出。");
        try {
          var d = parseHistoricalDraft(raw, key);
          var matched = d.segments.reduce(function (n, s) { return n + s.unit_ids.filter(function (id) { return !!unitById[id]; }).length; }, 0);
          rows.push({ key: key, draft: d, matched: matched });
        } catch (_) { invalid += 1; }
      }
      rows.sort(function (a, b) { return Date.parse(b.draft.saved_at) - Date.parse(a.draft.saved_at); });
      list.appendChild(make("p", "muted", "找到 " + rows.length + " 份旧报告草稿" + (invalid ? "；另有 " + invalid + " 份格式不可读，已保留原记录。" : "。") + "仅查找当前浏览器、当前网站地址保存的草稿。"));
      if (!rows.length) list.appendChild(make("p", "warning-box", "这里未找到可读取的旧草稿。请确认使用的是当时保存草稿的浏览器与地址；不要清除网站数据。"));
      rows.forEach(function (row) {
        var box = make("div", "panel"); box.appendChild(make("strong", "", new Date(row.draft.saved_at).toLocaleString("zh-CN", { hour12: false })));
        var selected = row.draft.segments.filter(function (s) { return !!s.project_id; }).length;
        box.appendChild(make("p", "muted", "已选项目 " + selected + " 段 · 与当前报告匹配 " + row.matched + " 个素材"));
        var actions = make("div", "toolbar-actions"), inspect = make("button", "", "查看恢复内容"), backup = make("button", "secondary", "下载原草稿备份");
        inspect.type = backup.type = "button"; inspect.disabled = !row.matched;
        inspect.addEventListener("click", function () { previewHistoricalDraft(row.key); }); backup.addEventListener("click", function () { downloadDraftBackup(row.key); });
        actions.appendChild(inspect); actions.appendChild(backup); box.appendChild(actions); list.appendChild(box);
      });
      list.scrollIntoView({ block: "nearest" });
    } catch (error) { clear(list); list.appendChild(make("p", "warning-box", "旧草稿查找未完成：" + error.message + " 当前选择和草稿记录未改变。")); }
  }

  function draftValue() {
    syncCompanionSegments();
    return { schema_version: "0.3", report_id: model.report_id, saved_at: new Date().toISOString(), segments: state.segments.map(function (segment) {
      return { segment_id: segment.segment_id, label: segment.label, unit_ids: segment.unit_ids.slice(), project_id: segment.project_id, decision: segment.decision, acknowledge_date_mismatch: !!segment.acknowledge_date_mismatch };
    }), manual_projects: state.manualProjects.map(function (descriptor) {
      return { project_id: descriptor.project_id, name: descriptor.name, shoot_date: descriptor.shoot_date, unit_ids: descriptor.unit_ids.slice() };
    }) };
  }
  function saveDraft() {
    var errors = validatePlan();
    if (errors.length) { setDraftStatus("草稿未保存：" + errors[0], "error"); return false; }
    try {
      var draft = draftValue(), encoded = JSON.stringify(draft), key = draftKey();
      localStorage.setItem(key, encoded);
      if (localStorage.getItem(key) !== encoded) throw new Error("draft_readback");
      var confirmed = draft.segments.filter(function (segment) { return segment.decision === "confirmed"; }).length;
      setDraftStatus("草稿已保存（" + new Date(draft.saved_at).toLocaleTimeString("zh-CN", { hour12: false }) + "），已确认 " + confirmed + " 段。保存在当前浏览器；刷新同一报告后可点击“恢复本报告草稿”。", "success");
      return true;
    } catch (error) { setDraftStatus("未能确认草稿已保存，请保留当前页面，不要刷新。可尝试“导出归档计划（不执行）”作为备份。", "error"); return false; }
  }
  function restoreDraft() {
    var raw;
    try { raw = localStorage.getItem(draftKey()); } catch (error) { setDraftStatus("读取本机草稿失败，未恢复任何状态。", "error"); return; }
    if (!raw) { setDraftStatus("当前报告没有草稿，正在查找旧报告草稿。", ""); showDraftRecovery(); return; }
    try {
      var draft = JSON.parse(raw);
      if (!draft || typeof draft !== "object" || Array.isArray(draft)) throw new Error("shape");
      var draftKeys = Object.keys(draft).sort();
      if (draft.schema_version !== "0.2" && draft.schema_version !== "0.3") throw new Error("version");
      var expectedDraftKeys = draft.schema_version === "0.2" ? "report_id|saved_at|schema_version|segments" : "manual_projects|report_id|saved_at|schema_version|segments";
      if (draftKeys.join("|") !== expectedDraftKeys) throw new Error("keys");
      if (typeof draft.report_id !== "string" || draft.report_id !== model.report_id || draft.report_id.length > 200) throw new Error("report");
      if (typeof draft.saved_at !== "string" || draft.saved_at.length < 1 || draft.saved_at.length > 64 || !Number.isFinite(Date.parse(draft.saved_at))) throw new Error("time");
      if (!Array.isArray(draft.segments) || draft.segments.length > Math.max(units.length, 1)) throw new Error("segments");
      var restoredManualProjects = [];
      if (draft.schema_version === "0.3") {
        if (!Array.isArray(draft.manual_projects) || draft.manual_projects.length > Math.max(units.length, 1)) throw new Error("manual_projects");
        draft.manual_projects.forEach(function (descriptor) {
          var descriptorErrors = [];
          manualDescriptorValid(descriptor, Object.create(null), descriptorErrors);
          if (descriptorErrors.length) throw new Error("manual_project");
          restoredManualProjects.push({ project_id: descriptor.project_id, name: descriptor.name, shoot_date: descriptor.shoot_date, unit_ids: descriptor.unit_ids.slice() });
        });
      }
      var restored = draft.segments.map(function (source, index) {
        if (!source || typeof source.segment_id !== "string" || !source.segment_id || !Array.isArray(source.unit_ids)) throw new Error("shape");
        if (source.segment_id.length > 120 || typeof source.label !== "string" || source.label.length > 160 || source.unit_ids.length < 1) throw new Error("length");
        var segmentKeys = Object.keys(source).sort();
        if (segmentKeys.join("|") !== "acknowledge_date_mismatch|decision|label|project_id|segment_id|unit_ids") throw new Error("fields");
        if (source.unit_ids.some(function (id) { return typeof id !== "string" || !id || id.length > 255; }) || new Set(source.unit_ids).size !== source.unit_ids.length) throw new Error("units");
        if (source.project_id !== null && source.project_id !== undefined && !projectForIdWithList(String(source.project_id), restoredManualProjects)) throw new Error("project");
        if (source.project_id !== null && source.project_id !== undefined && (typeof source.project_id !== "string" || !source.project_id || source.project_id.length > 255)) throw new Error("project");
        if (["pending", "confirmed", "deferred"].indexOf(source.decision) < 0) throw new Error("decision");
        var segment = normalizeSegment(source, index);
        segment.project_id = source.project_id === null || source.project_id === undefined ? null : String(source.project_id);
        segment.decision = source.decision;
        if (typeof source.acknowledge_date_mismatch !== "boolean") throw new Error("ack");
        segment.acknowledge_date_mismatch = source.acknowledge_date_mismatch;
        return segment;
      });
      var partitionedRestored = materialState ? partitionMaterialSegments(restored) : restored;
      if (validatePlan(partitionedRestored, restoredManualProjects).length) throw new Error("invalid");
      state.segments = partitionedRestored;
      state.manualProjects = restoredManualProjects;
      state.manualDrafts = Object.create(null);
      state.manualOpen = Object.create(null);
      state.selectedUnits = Object.create(null);
      state.selectedSegments = Object.create(null);
      state.companionArchiveSelections = Object.create(null);
      state.segments.forEach(function (segment) {
        if (segment._materialRole === "companion" && segment.decision === "confirmed" && segment.project_id) state.companionArchiveSelections[segment.segment_id] = true;
      });
      reconcileArchiveSegments();
      syncCompanionSegments();
      render();
      setDraftStatus("已恢复当前报告草稿。", "success");
    } catch (error) { setDraftStatus("草稿格式无效或不属于当前报告，已拒绝恢复。", "error"); }
  }
  function updateSummary() {
    var userSegments = state.segments.filter(function (segment) { return !segment._archiveDeferred && !segment._retainedDeferred && !segment._materialDeferred; });
    var confirmed = userSegments.filter(function (segment) { return segment.decision === "confirmed"; }).length;
    var pending = userSegments.filter(function (segment) { return segment.decision === "pending"; }).length;
    var deferred = userSegments.filter(function (segment) { return segment.decision === "deferred"; }).length;
    var pendingUnits = userSegments.filter(function (segment) { return segment.decision === "pending"; }).reduce(function (sum, segment) { return sum + segment.unit_ids.length; }, 0);
    var archived = archivedUnitCount();
    var companions = state.segments.filter(function (segment) { return segment._materialRole === "companion" && segment.decision === "confirmed"; }).reduce(function (sum, segment) { return sum + segment.unit_ids.length; }, 0);
    byId("summary").textContent = "总段 " + userSegments.length + " · 已确认 " + confirmed + " · 待处理 " + pending + " 段 / " + pendingUnits + " 个素材" + (deferred ? " · 已暂缓 " + deferred : "") + (companions ? " · 附属素材待归档 " + companions + " 项" : "") + " · 已归档 " + archived + " 个素材";
    var summary = byId("archive-state-summary");
    if (summary) {
      clear(summary);
      var totalFiles = state.archiveState.paged ? state.archiveState.current_files : state.archiveState.archived_units.filter(function (entry) { return entry.in_current_model; }).reduce(function (sum, entry) { return sum + (Number(entry.file_count) || entry.files.length); }, 0);
      summary.appendChild(make("span", "", "当前报告已归档 " + archived + " 个素材 / " + totalFiles + " 个文件"));
      if (retainedUnitCount()) summary.appendChild(make("span", "warning", "已复制，来源保留（未完整校验） " + retainedUnitCount() + " 个素材 / " + Number(state.archiveState.retained_current_files || 0) + " 个文件"));
      summary.appendChild(make("span", "", pendingUnits ? "待处理 " + pendingUnits + " 个素材" : (archived >= units.length ? "已完成：没有待处理素材。" : "当前没有待处理段；已确认或暂缓素材仍保留。")));
      if (state.archiveState.warnings.length) summary.appendChild(make("span", "warning", state.archiveState.warnings.join("；")));
    }
  }

  function archiveHistoryEntries() {
    if (state.archiveState.paged) return historyRows;
    return state.archiveState.archived_units.slice().sort(function (left, right) {
      return String(right.completed_at || "").localeCompare(String(left.completed_at || ""));
    });
  }
  var historyRows = [], historyNext = 0, historyOffset = 0, historyGeneration = "", historyBusy = false, historyMessage = "", historyRevision = 0;
  function resetHistory() {
    historyRevision += 1; historyRows = []; historyNext = 0; historyOffset = 0;
    historyGeneration = ""; historyMessage = ""; historyBusy = false;
  }
  async function loadHistory(offset) {
    if (historyBusy || !state.archiveState.paged) return;
    var requested = offset === undefined ? historyNext : offset;
    if (requested === null) return;
    var revision = historyRevision, started = Date.now();
    historyBusy = true; historyMessage = "正在读取本页历史…"; renderArchiveHistory();
    try {
      while (Date.now() - started < 120000) {
        var controller = new AbortController(), timer = setTimeout(function () { controller.abort(); }, 15000);
        var response, data;
        try {
          response = await fetch("/api/archive-history?report_id=" + encodeURIComponent(model.report_id) +
            "&offset=" + requested + "&limit=50&generation=" + encodeURIComponent(requested ? historyGeneration : ""),
            {cache:"no-store",credentials:"same-origin",signal:controller.signal});
          data = await response.json();
        } finally { clearTimeout(timer); }
        if (revision !== historyRevision) return;
        if (response.status === 202) {
          historyMessage = "正在更新历史记录…"; renderArchiveHistory();
          await new Promise(function (resolve) { setTimeout(resolve, 2000); });
          continue;
        }
        if (!response.ok || data.version !== "archive-history-page/1" || data.report_id !== model.report_id || data.offset !== requested ||
            !Array.isArray(data.archived_units) || data.archived_units.length > 50 ||
            (requested && data.generation !== historyGeneration)) throw new Error("历史记录需要重新读取；点击下一页重试。");
        historyRows = data.archived_units.map(archiveEntrySafe).filter(Boolean);
        historyOffset = requested; historyNext = data.next_offset; historyGeneration = data.generation;
        state.archiveState.history_total = data.total; historyMessage = "第 " + (Math.floor(requested / 50) + 1) + " 页，每页最多 50 条";
        return;
      }
      throw new Error("读取历史超时，点击下一页重试。");
    } catch (error) {
      if (revision !== historyRevision) return;
      historyRows = []; historyNext = 0; historyGeneration = "";
      historyMessage = "暂时无法读取历史，点击重试。";
    } finally {
      if (revision === historyRevision) { historyBusy = false; renderArchiveHistory(); }
    }
  }
  function renderArchiveHistory() {
    var panel = byId("archive-state-panel");
    var details = byId("archive-history");
    var list = byId("archive-history-list");
    var more = byId("archive-history-more");
    var summary = byId("archive-history-summary");
    var entries = archiveHistoryEntries();
    if (!panel || !details || !list || !more || !summary) return;
    var total = state.archiveState.paged ? state.archiveState.history_total : entries.length;
    panel.hidden = !total && !state.archiveState.warnings.length && !retainedUnitCount();
    renderRetainedPanel();
    summary.textContent = "已归档历史（" + total + " 条）";
    if (!details.open) {
      clear(list);
      more.hidden = true;
      return;
    }
    clear(list);
    state.archiveState.warnings.forEach(function (warning) { list.appendChild(make("p", "warning", String(warning))); });
    var visibleEntries = state.archiveState.paged ? entries : entries.slice(0, state.archiveHistoryLimit);
    if (state.archiveState.paged && historyMessage) list.appendChild(make("p", "muted", historyMessage));
    visibleEntries.forEach(function (entry) {
      var item = make("article", "archive-history-item");
      item.title = entry.unit_id;
      item.appendChild(make("strong", "", String(entry.project.name || "未命名项目") + " · " + (entry.origin === "existing_project_files" ? "已核对既有归档" : (entry.mode === "move" ? "移动归档" : "复制归档"))));
      item.appendChild(make("span", "muted", "目标路径：" + String(entry.project.path || "未提供")));
      item.appendChild(make("span", "muted", "完成时间：" + readableDateTime(entry.completed_at) + " · 文件 " + (entry.file_count || entry.files.length)));
      if (entry.files.length) {
        var files = make("span", "muted", "文件去向：" + entry.files.map(function (file) { return file.target_path || file.name; }).join("、"));
        item.appendChild(files);
      }
      list.appendChild(item);
    });
    more.hidden = state.archiveState.paged ? historyNext === null : entries.length <= visibleEntries.length;
    more.disabled = historyBusy;
    more.textContent = state.archiveState.paged ? (historyMessage.indexOf("暂时无法") === 0 ? "重试读取历史" : "下一页") : "显示更多已归档记录";
    var previous = byId("archive-history-previous");
    if (previous) { previous.hidden = !state.archiveState.paged || !historyOffset; previous.disabled = historyBusy; }
    if (state.archiveState.paged && !historyRows.length && !historyBusy && !historyMessage && total) loadHistory();
  }
  function renderRetainedPanel() {
    var panel = byId("archive-state-panel");
    if (!panel) return;
    var previous = byId("archive-retained-panel");
    if (previous && previous.parentNode) previous.parentNode.removeChild(previous);
    if (!retainedUnitCount()) return;
    var box = make("div", "archive-retained-panel notice");
    box.id = "archive-retained-panel";
    box.style.display = "flex";
    box.style.flexDirection = "column";
    box.style.gap = ".35rem";
    box.appendChild(make("strong", "", "历史记录 · 已复制，来源保留（未完整校验）"));
    box.appendChild(make("span", "muted", retainedUnitCount() + " 个素材 · " + Number(state.archiveState.retained_current_files || 0) + " 个文件"));
    box.appendChild(make("p", "muted", "这是历史记录，不需要处理，也不影响当前待处理的素材。这些素材仅确认已复制、内容尚未完整校验；来源文件仍保留，它们不会再次加入新归档任务；可在下方查看原任务记录。"));
    (state.archiveState.retained_jobs || []).slice(0, 20).forEach(function (job) {
      var projects = job.project_names && job.project_names.length ? " · 项目：" + job.project_names.join("、") : "";
      var row = make("span", "muted", "任务 " + String(job.job_id).slice(0, 12) + "… · " + job.unit_count + " 个素材 · " + job.file_count + " 个文件" + projects);
      row.title = String(job.job_id);
      row.style.display = "block";
      row.style.marginTop = ".25rem";
      row.style.overflowWrap = "anywhere";
      box.appendChild(row);
    });
    var history = byId("archive-history");
    if (history && history.parentNode === panel) panel.insertBefore(box, history);
    else panel.appendChild(box);
  }
  function applyArchiveState(data, options) {
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      setStatus("归档状态格式无效，未应用。", "error");
      return false;
    }
    if (String(data.report_id || "") !== String(model.report_id || "")) {
      setStatus("归档状态来自其他报告，未应用；当前选择和页面状态未改变。", "error");
      return false;
    }
    var normalized = archiveDataSafe(data);
    if (!normalized) {
      setStatus("归档状态格式无效，未应用。", "error");
      return false;
    }
    if (units.some(function (unit) { return unit._identityOnly && !normalized.byUnit[unit.unit_id] && !normalized.retainedByUnit[unit.unit_id] && materialRole(unit.unit_id) !== "discarded"; })) {
      setStatus("部分素材需要重新读取详情；请保存草稿后刷新页面。", "error");
      return false;
    }
    if (normalized.history_generation !== state.archiveState.history_generation) resetHistory();
    var unchanged = JSON.stringify(state.archiveState.archived_units || []) === JSON.stringify(normalized.archived_units) && JSON.stringify(state.archiveState.warnings || []) === JSON.stringify(normalized.warnings) && JSON.stringify(state.archiveState.retained_unit_ids || []) === JSON.stringify(normalized.retained_unit_ids) && Number(state.archiveState.retained_current_files || 0) === Number(normalized.retained_current_files || 0) && JSON.stringify(state.archiveState.retained_jobs || []) === JSON.stringify(normalized.retained_jobs || []);
    var position = rememberPosition();
    state.archiveState = normalized;
    state.archivedByUnit = normalized.byUnit;
    state.retainedByUnit = normalized.retainedByUnit;
    if (unchanged) {
      updateSummary();
      renderArchiveHistory();
      return true;
    }
    reconcileArchiveSegments();
    render();
    renderArchiveHistory();
    if (typeof window.CustomEvent === "function") window.dispatchEvent(new CustomEvent("muli-review-archive-state"));
    if (!(options && options.silent)) setStatus("归档状态已更新；已完成的素材已从待处理范围排除。", "success");
    if (position) restorePosition(position);
    return true;
  }

  function sourceSegmentsFromCurrentState() {
    var grouped = Object.create(null);
    state.segments.forEach(function (segment, index) {
      var baseId = segment._materialRole === "shoot" ? (segment._materialParentSegmentId || segment.segment_id) : segment.segment_id;
      if (grouped[baseId]) baseId = segment.segment_id;
      grouped[baseId] = {
        segment_id: baseId,
        label: segment.label,
        unit_ids: segment.unit_ids.slice(),
        project_id: segment._materialRole === "support" || segment._materialRole === "exception" ? null : segment.project_id,
        decision: segment._materialRole === "support" || segment._materialRole === "exception" ? "deferred" : segment.decision,
        acknowledge_date_mismatch: segment._materialRole === "companion" ? !!segment.acknowledge_date_mismatch : segment.acknowledge_date_mismatch,
        index: index
      };
    });
    sourceSegments.forEach(function (source, index) {
      var baseId = String(source && source.segment_id || "segment-" + (index + 1));
      if (!grouped[baseId]) grouped[baseId] = { segment_id: baseId, label: String(source && source.label || "拍摄段 " + (index + 1)), unit_ids: [], project_id: null, decision: "pending", acknowledge_date_mismatch: false, index: index };
      var target = grouped[baseId];
      (Array.isArray(source && source.unit_ids) ? source.unit_ids : []).forEach(function (unitId) { if (target.unit_ids.indexOf(String(unitId)) < 0 && !state.segments.some(function (segment) { return segment.unit_ids.indexOf(String(unitId)) >= 0; })) target.unit_ids.push(String(unitId)); });
    });
    return Object.keys(grouped).sort(function (left, right) { return grouped[left].index - grouped[right].index; }).map(function (key) { return grouped[key]; });
  }

  function applyMaterialState(data) {
    var normalized = normalizeMaterialState(data);
    if (!normalized) {
      setStatus("来源状态格式无效或不属于当前报告，未应用；当前选择和草稿计划未改变。", "error");
      return false;
    }
    if (units.some(function (unit) { return unit._identityOnly && !isArchivedUnit(unit.unit_id) && !isRetainedUnit(unit.unit_id) && normalized.units[unit.unit_id].category !== "discarded"; })) {
      setStatus("已恢复素材需要重新读取详情；请保存草稿后刷新页面。", "error");
      return false;
    }
    var position = rememberPosition();
    var priorCompanionDecisions = Object.create(null);
    state.segments.forEach(function (segment) {
      if (segment._materialRole !== "companion") return;
      segment.unit_ids.forEach(function (unitId) {
        priorCompanionDecisions[unitId] = { project_id: segment.project_id, decision: segment.decision, acknowledge_date_mismatch: segment.acknowledge_date_mismatch };
      });
    });
    var previousMaterials = materialByUnit;
    materialState = normalized;
    settings.material_state = data;
    materialByUnit = normalized.units;
    state.segments = partitionMaterialSegments(sourceSegmentsFromCurrentState());
    var withRecovered = [];
    state.segments.forEach(function (segment) {
      var recovered = segment._materialRole === "shoot" ? segment.unit_ids.filter(function (id) {
        return previousMaterials[id] && previousMaterials[id].category !== "shoot" && !isArchivedUnit(id) && !isRetainedUnit(id);
      }) : [];
      if (!recovered.length) { withRecovered.push(segment); return; }
      var retained = segment.unit_ids.filter(function (id) { return recovered.indexOf(id) < 0; });
      if (retained.length) { segment.unit_ids = retained; withRecovered.push(segment); }
      recovered.forEach(function (id) {
        var restored = materialSegmentFrom({ segment_id: ("recovered-" + id).slice(0, 120), label: "来源已恢复，请重新确认" }, 0, "shoot", [id]);
        withRecovered.push(restored);
      });
    });
    state.segments = withRecovered;
    state.segments.forEach(function (segment) {
      if (segment._materialRole !== "companion") return;
      var prior = priorCompanionDecisions[segment.unit_ids[0]];
      if (prior && prior.decision === "confirmed" && prior.project_id) {
        segment.project_id = prior.project_id;
        segment.decision = "confirmed";
        segment.acknowledge_date_mismatch = true;
      }
    });
    reconcileArchiveSegments();
    render();
    if (position) restorePosition(position);
    return true;
  }
  function updateActionState() {
    byId("timeline").querySelectorAll("[data-unit-details]").forEach(function (details) { details.updateSummary(); });
    var mergeButton = byId("merge-segments");
    if (mergeButton) mergeButton.disabled = Object.keys(state.selectedSegments).length < 2;
    var splitButton = byId("split-units");
    if (splitButton) splitButton.disabled = !canSplitSelected();
  }
  function canSplitSelected() {
    var selected = Object.keys(state.selectedUnits);
    if (!selected.length) return false;
    return state.segments.some(function (segment) {
      var count = segment.unit_ids.filter(function (id) { return selected.indexOf(id) >= 0; }).length;
      return count > 0 && count < segment.unit_ids.length;
    });
  }
  function pruneSelections() {
    if (!state.date) return;
    state.segments.forEach(function (segment) {
      if (visible(segment)) return;
      delete state.selectedSegments[segment.segment_id];
      segment.unit_ids.forEach(function (id) { delete state.selectedUnits[id]; });
    });
  }
  function segmentCard(id) {
    return Array.prototype.find.call(byId("timeline").children, function (node) { return node.dataset.segmentId === id; });
  }
  function rememberPosition(card) {
    var active = document.activeElement;
    card = card || (active && active.closest ? active.closest(".segment-card") : null);
    if (!card) card = Array.prototype.find.call(byId("timeline").children, function (node) {
      var rect = node.getBoundingClientRect(); return rect.bottom > 0 && rect.top < window.innerHeight;
    });
    var key = card && active && card.contains(active) ? ["projectSelect", "ackSegmentId", "confirmSegment", "deferSegment", "withdrawSegment", "openManual", "manualName", "manualDate", "createManual", "moreUnits", "detailsToggle", "closeDetails", "unitId"].find(function (name) { return active.dataset[name] !== undefined; }) : null;
    return { x: window.scrollX, y: window.scrollY, segmentId: card && card.dataset.segmentId,
      cardTop: card && card.getBoundingClientRect().top, key: key, value: key && active.dataset[key],
      tag: key && active.tagName, controlTop: key && active.getBoundingClientRect().top };
  }
  function restorePosition(position) {
    var card = position.segmentId && segmentCard(position.segmentId);
    if (!card) { window.scrollTo(position.x, position.y); return; }
    var control = position.key && Array.prototype.find.call(card.querySelectorAll("input, select, button, summary"), function (node) {
      return node.tagName === position.tag && node.dataset[position.key] === position.value;
    });
    if (control && !control.disabled) control.focus({ preventScroll: true });
    var anchor = control || card;
    var oldTop = control ? position.controlTop : position.cardTop;
    window.scrollBy(0, anchor.getBoundingClientRect().top - oldTop);
  }
  function notifyPlanChange() {
    updateSummary(); updateActionState();
    if (typeof window.CustomEvent === "function") window.dispatchEvent(new CustomEvent("muli-review-change"));
  }
  function renderSegment(segment) {
    var previous = segmentCard(segment.segment_id);
    if (!previous) { render(); return segmentCard(segment.segment_id); }
    var position = rememberPosition(previous);
    if (photoObserver) previous.querySelectorAll(".segment-photos").forEach(function (node) { photoObserver.unobserve(node); });
    var replacement = makeSegmentCard(segment);
    // Replace only this card: other previews and the document height stay intact.
    previous.replaceWith(replacement);
    notifyPlanChange();
    restorePosition(position);
    return replacement;
  }
  function render(presentationOnly) {
    var position = rememberPosition();
    if (photoObserver) photoObserver.disconnect();
    pruneSelections();
    var timeline = byId("timeline");
    var fragment = document.createDocumentFragment();
    var shown = state.segments.filter(function (segment) { return !segment._archiveDeferred && !segment._retainedDeferred && !segment._materialDeferred && visible(segment); });
    shown.sort(function (left, right) { return segmentSortKey(left) - segmentSortKey(right); });
    var shownPendingUnits = shown.filter(function (segment) { return segment.decision === "pending"; }).reduce(function (sum, segment) { return sum + segment.unit_ids.length; }, 0);
    if (!shown.length) {
      fragment.appendChild(make("p", "empty", archivedUnitCount() && !state.segments.some(function (segment) { return !segment._archiveDeferred && !segment._retainedDeferred; }) ? "当前报告的素材均已归档，暂无待处理素材。" : "当前筛选没有拍摄段。"));
    } else if (!shownPendingUnits && !state.date && !state.projectQuery && archivedUnitCount() + retainedUnitCount() >= units.length) {
      fragment.appendChild(make("p", "empty", "已完成：没有待处理素材。"));
    }
    shown.forEach(function (segment) { fragment.appendChild(makeSegmentCard(segment)); });
    // One replacement avoids briefly collapsing the entire long page to zero.
    timeline.replaceChildren(fragment);
    renderMaterialPanels();
    if (presentationOnly === true) { updateSummary(); updateActionState(); } else notifyPlanChange();
    restorePosition(position);
  }
  function initControls() {
    var dateSelect = byId("date-filter");
    allDateValues().forEach(function (date) { var option = make("option", "", date); option.value = date; dateSelect.appendChild(option); });
    dateSelect.addEventListener("change", function () { state.date = dateSelect.value; render(true); });
    var projectSearch = byId("project-search");
    projectSearch.addEventListener("input", function () { state.projectQuery = projectSearch.value; render(true); });
    byId("split-units").addEventListener("click", splitSelected);
    byId("merge-segments").addEventListener("click", mergeSelected);
    var jumpSubmit = byId("jump-submit");
    if (jumpSubmit) jumpSubmit.addEventListener("click", function () {
      var panel = byId("archive-submit-panel");
      if (panel && typeof panel.scrollIntoView === "function") panel.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    byId("save-draft").addEventListener("click", saveDraft);
    byId("restore-draft").addEventListener("click", restoreDraft);
    byId("recover-other-drafts").addEventListener("click", showDraftRecovery);
    byId("export-plan").addEventListener("click", exportPlan);
    var history = byId("archive-history");
    if (history) history.addEventListener("toggle", renderArchiveHistory);
    var moreHistory = byId("archive-history-more");
    var previousHistory = byId("archive-history-previous");
    if (previousHistory) previousHistory.addEventListener("click", function () { loadHistory(Math.max(0, historyOffset - 50)); });
    if (moreHistory) moreHistory.addEventListener("click", function () {
      if (state.archiveState.paged) { historyMessage = ""; loadHistory(); }
      else { state.archiveHistoryLimit += 20; renderArchiveHistory(); }
    });
  }
  function showModelMeta() {
    var fullReportId = String(model.report_id || "未提供");
    var reportNode = byId("report-id");
    reportNode.textContent = fullReportId.length > 28 ? fullReportId.slice(0, 18) + "…" + fullReportId.slice(-8) : fullReportId;
    reportNode.title = fullReportId;
    var snapshotNode = byId("snapshot-at");
    snapshotNode.textContent = readableDateTime(model.snapshot_at);
    snapshotNode.title = String(model.snapshot_at || "未提供");
    if (model.example_data === true) byId("example-banner").hidden = false;
    var excluded = Array.isArray(model.excluded_batches) ? model.excluded_batches : [];
    if (excluded.length) {
      var box = byId("excluded-box"), list = byId("excluded-batches");
      var historical = [], actionable = [];
      excluded.forEach(function (batch) {
        var reasons = Array.isArray(batch.reasons) ? batch.reasons.filter(function (text) { return typeof text === "string" && text; }).join("；") : "";
        var superseded = String(batch.status || "") === "superseded" || reasons.indexOf("当前完整列表已无此版本") >= 0;
        (superseded ? historical : actionable).push({ batch: batch, reasons: reasons });
      });
      var line = function (row) {
        return String(row.batch.batch_id || "未命名批次") + " · " + batchStatusText(row.batch.status)
          + (row.reasons ? " · " + row.reasons : "");
      };
      list.appendChild(make("li", "muted", actionable.length
        ? "已排除 " + excluded.length + " 个批次，其中 " + actionable.length + " 个需要注意，"
          + historical.length + " 个已被更新版本取代（历史记录，不影响当前待处理素材）。"
        : "已排除 " + excluded.length + " 个批次，全部已被更新版本取代；这里是历史记录，不影响当前待处理素材。"));
      actionable.forEach(function (row) { list.appendChild(make("li", "", line(row))); });
      if (historical.length) {
        var details = document.createElement("details");
        details.appendChild(make("summary", "muted", "展开查看已被取代的历史批次 " + historical.length + " 个"));
        var inner = document.createElement("ul");
        historical.forEach(function (row) { inner.appendChild(make("li", "muted", line(row))); });
        details.appendChild(inner);
        box.appendChild(details);
      }
      box.hidden = false;
    }
  }
  window.muliReview = {
    setBatchFilter: function (batch) {
      if (typeof batch !== "string" || (batch && !/^BATCH_\d{8}_\d{6,}$/.test(batch))) return false;
      state.batch = batch; render(true); return true;
    },
    getBatchFilter: function () { return state.batch; },
    getPlan: function () { return currentPlan(); },
    saveDraft: saveDraft,
    validate: function () { return validatePlan().slice(); },
    getArchiveState: function () { return state.archiveState; },
    getArchivedUnitIds: function () { return Object.keys(state.archivedByUnit); },
    getRetainedUnitIds: function () { return Object.keys(state.retainedByUnit); },
    applyArchiveState: function (data, options) { return applyArchiveState(data, options); },
    applyMaterialState: function (data) { return applyMaterialState(data); }
  };
  initControls(); showModelMeta();
  var initialArchive = archiveDataSafe(settings.archive_state);
  if (model.presentation && (!initialArchive || units.some(function (unit) {
    return unit._identityOnly && !initialArchive.byUnit[unit.unit_id] && !initialArchive.retainedByUnit[unit.unit_id] && materialRole(unit.unit_id) !== "discarded";
  }))) {
    delete window.muliReview;
    showGlobalError("页面归档索引不完整，请重新加载。");
    return;
  }
  if (initialArchive) {
    state.archiveState = initialArchive;
    state.archivedByUnit = initialArchive.byUnit;
    state.retainedByUnit = initialArchive.retainedByUnit;
    reconcileArchiveSegments();
  }
  render(); renderArchiveHistory();
  if (settings.archive_state && settings.archive_state.report_id && String(settings.archive_state.report_id) !== String(model.report_id || "")) {
    setStatus("归档状态来自其他报告，未应用；当前选择和页面状态未改变。", "error");
  }
})();
