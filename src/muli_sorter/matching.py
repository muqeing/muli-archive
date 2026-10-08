"""Explainable candidate grouping; dates alone never confirm a project."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

PHOTOS = {".arw", ".cr2", ".cr3", ".nef", ".dng", ".raf", ".orf", ".rw2", ".jpg", ".jpeg", ".heic", ".tif", ".tiff"}
VIDEOS = {".mp4", ".mov", ".mxf", ".avi", ".mts", ".m2ts"}
AUDIO = {".wav", ".mp3", ".m4a", ".aac"}
SIDECARS = {".lrf", ".thm", ".scr", ".xml", ".xmp"}
SEGMENT_GAP_SECONDS = 20 * 60
LOCAL_ZONE = ZoneInfo("Asia/Shanghai")


def project_index(root: Path):
    """Read only year/month/project directory names, never descend into media."""
    result = []
    for year in sorted(root.iterdir()):
        if year.is_symlink() or not year.is_dir() or not re.fullmatch(r"20\d{2}", year.name):
            continue
        for month in sorted(year.iterdir()):
            if month.is_symlink() or not month.is_dir() or not re.fullmatch(r"(?:0?[1-9]|1[0-2])月", month.name):
                continue
            for p in sorted(month.iterdir()):
                if p.is_symlink() or not p.is_dir():
                    continue
                match = re.match(r"^(20\d{6})(?:[-_\s]|$)", p.name)
                if not match:
                    continue
                try:
                    day = datetime.strptime(match[1], "%Y%m%d").date().isoformat()
                except ValueError:
                    continue
                # Explicit no-order folders must not acquire an order ID from digits in their title.
                manual = p.name.startswith(match[1] + "_自建_")
                order = None if manual else re.search(r"(?:^|_)(\d{5})(?:_|$)", p.name)
                rel = p.relative_to(root).as_posix()
                result.append({"project_id": "dir-" + sha256(rel.encode()).hexdigest()[:16], "order_id": order[1] if order else None, "name": p.name, "path": rel, "dates": [day], "evidence_source": "directory_name_only"})
    return result


def utc_marked(f):
    """True when the file carries an explicit UTC timestamp.

    QuickTime writes 2026-10-05T02:17:41.000000Z for DJI M4ROOT clips. The
    manifest keeps that raw value under video.creation_time_raw but splits the
    capture time into a naive wall clock, so the zone has to be restored here;
    otherwise every clip lands eight hours early and can never line up with the
    photos of the same shoot.
    """
    raw = ((f.get("metadata") or {}).get("video") or {}).get("creation_time_raw")
    return isinstance(raw, str) and raw.rstrip().endswith(("Z", "z"))


def capture(f):
    info = (f.get("metadata") or {}).get("capture_time") or {}
    raw = info.get("normalized") or info.get("wall_time")
    if not raw:
        return None, False
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None, False
    if dt.tzinfo is None and info.get("confidence") == "wall_time" and utc_marked(f):
        # An explicit UTC marker outranks the wall-clock label: the instant is
        # known, only its display zone was dropped.
        return dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_ZONE), True
    return dt, dt.tzinfo is not None and info.get("confidence") == "timezone_aware"


def _binding_projects(bundle, projects, bindings, manifest_id):
    """Only explicit, previously confirmed device/time-window bindings can qualify."""
    dt, trusted = bundle["capture"], bundle["time_trusted"]
    if not trusted or not dt or bundle["kind"] not in ("photo", "video", "audio"):
        return set()
    known = {p["project_id"] for p in projects}
    matched = set()
    for rule in bindings:
        if rule.get("confirmed") is not True or not rule.get("confirmation_reference") or rule.get("manifest_id") != manifest_id or rule.get("source_id") != bundle["source_id"] or rule.get("project_id") not in known:
            continue
        try:
            start, end = datetime.fromisoformat(rule["start"]), datetime.fromisoformat(rule["end"])
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                continue
            if start <= dt < end:
                matched.add(rule["project_id"])
        except (KeyError, ValueError, TypeError):
            continue
    return matched


def group_files(files, projects, bindings, manifest_id):
    by_stem = defaultdict(list)
    for f in files:
        p = PurePosixPath(f["relative_path"])
        stem = p.stem if p.suffix.lower() in PHOTOS | VIDEOS | AUDIO | SIDECARS else p.name + "#auxiliary"
        by_stem[(f.get("resolved_source_id"), str(p.parent), stem)].append(f)
    bundles = []
    for (source, parent, stem), items in sorted(by_stem.items(), key=lambda x: str(x[0])):
        photos = [f for f in items if PurePosixPath(f["relative_path"]).suffix.lower() in PHOTOS]
        videos = [f for f in items if PurePosixPath(f["relative_path"]).suffix.lower() in VIDEOS]
        audios = [f for f in items if PurePosixPath(f["relative_path"]).suffix.lower() in AUDIO]
        primaries = photos or videos or audios
        kind = "photo" if photos else "video" if videos else "audio" if audios else "proxy_only" if any(PurePosixPath(f["relative_path"]).suffix.lower() == ".lrf" for f in items) else "auxiliary"
        principal = sorted(primaries or items, key=lambda f: (capture(f)[0] is None, f["relative_path"]))[0]
        dt, trusted = capture(principal)
        reasons = []
        if sum(bool(x) for x in (photos, videos, audios)) > 1:
            trusted = False
            reasons.append("同名组混有多种主素材，需要确认配对关系")
        timestamps = [capture(f)[0] for f in primaries if capture(f)[0] is not None]
        # Compare wall times as well as offsets; inconsistent pairs never inherit trust.
        if dt and any(t != dt for t in timestamps):
            trusted = False
            reasons.append("同名主文件拍摄时间不一致")
        if dt is None:
            reasons.append("缺少可用拍摄时间")
        elif not trusted:
            reasons.append("拍摄时间缺少可信时区或存在冲突")
        if kind == "proxy_only":
            reasons.append("仅有 LRF 代理文件，当前批次没有对应主视频")
        if kind == "auxiliary":
            reasons.append("设备辅助文件或关系未确认的伴随文件，保留待核对")
        camera = (principal.get("metadata") or {}).get("camera") or {}
        device = " ".join(str(camera[k]) for k in ("make", "model") if camera.get(k)) or "设备未识别"
        bundle = {"source_id": source, "parent": parent, "stem": stem, "items": items, "capture": dt, "time_trusted": trusted, "kind": kind, "device": device, "reasons": reasons}
        bundle["bound"] = _binding_projects(bundle, projects, bindings, manifest_id)
        bundles.append(bundle)
    partitions = defaultdict(list)
    for b in bundles:
        day = b["capture"].date().isoformat() if b["capture"] else None
        partitions[(b["source_id"], b["parent"], day, b["kind"], b["device"])].append(b)
    groups = []
    for key, items in sorted(partitions.items(), key=lambda x: str(x[0])):
        ordered = sorted(items, key=lambda b: (b["capture"].isoformat() if b["capture"] else "", b["stem"]))
        segments = []
        for b in ordered:
            if not segments:
                segments.append([b])
                continue
            prev = segments[-1][-1]
            a, z = b["capture"], prev["capture"]
            gap = abs((a.replace(tzinfo=None) - z.replace(tzinfo=None)).total_seconds()) if a and z else 0
            if gap > SEGMENT_GAP_SECONDS or b["bound"] != prev["bound"]:
                segments.append([b])
            else:
                segments[-1].append(b)
        for segment in segments:
            selected = [f for b in segment for f in b["items"]]
            identity = json.dumps([manifest_id, sorted(f["file_id"] for f in selected)], ensure_ascii=False)
            group_id = "group-" + sha256(identity.encode()).hexdigest()[:16]
            bound_sets = [b["bound"] for b in segment]
            bound = bound_sets[0] if all(s == bound_sets[0] for s in bound_sets) else set()
            ready = len(bound) == 1 and all(b["time_trusted"] for b in segment)
            day, kind = key[2], key[3]
            candidates = []
            for p in projects:
                if p["project_id"] in bound or day in p.get("dates", []):
                    evidence = ["已有人工确认的设备与拍摄时段对应关系"] if p["project_id"] in bound else ["目录名日期相符，仅作为候选"]
                    candidates.append({"project_id": p["project_id"], "order_id": p.get("order_id"), "name": p["name"], "path": p["path"], "evidence": evidence})
            reasons = list(dict.fromkeys(r for b in segment for r in b["reasons"]))
            if not ready and kind != "auxiliary":
                reasons.append("尚无唯一、已确认的设备与项目对应关系")
            if not candidates and kind != "auxiliary":
                reasons.append("现有项目目录没有对应日期的候选；不自动新建项目")
            if len(bound) > 1:
                reasons.append("已确认的时间窗口互相重叠，需消除冲突")
            if ready:
                candidates = [p for p in candidates if p["project_id"] in bound]
            units = []
            for bundle in segment:
                descriptors = [{"source_path": f["resolved_path"], "name": f["relative_path"], "size_bytes": f["size_bytes"], "blake3": (f.get("hash") or {}).get("source")} for f in bundle["items"]]
                units.append({"files": descriptors, "capture_time": bundle["capture"].isoformat() if bundle["capture"] else None,
                              "capture_date": bundle["capture"].date().isoformat() if bundle["capture"] else None,
                              "timezone_trusted": bundle["time_trusted"], "device": bundle["device"], "kind": bundle["kind"],
                              "warnings": bundle["reasons"], "candidate_project_ids": [p["project_id"] for p in candidates]})
            groups.append({"group_id": group_id, "capture_date": day, "device": key[4], "kind": kind,
                           "file_count": len(selected), "bytes": sum(f["size_bytes"] for f in selected),
                           "status": "ready" if ready else "auxiliary" if kind == "auxiliary" else "review",
                           "reasons": reasons, "candidates": candidates, "file_names": [f["relative_path"] for f in selected],
                           "resolved_paths": [f["resolved_path"] for f in selected], "units": units})
    return groups
