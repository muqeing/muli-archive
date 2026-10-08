"""Single-writer ingest service. Web requests never perform independent file writes."""

import fcntl
import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from blake3 import blake3

from .copier import RcloneCopier
from .metadata import extract_metadata
from .reports import retention_snapshot, write_report
from .safeio import Root, SafetyError, atomic_json, digest_fd, digest_file, signature, sync_dir
from .settings import UNSUPPORTED_SETTINGS, Settings, settings_schema
from .sources import (
    check_source,
    disconnected_source_record,
    linux_external_mounts,
    mounted_volume_identity,
    scan_source,
    source_record,
)
from .store import Store
from .staging_coordination import staging_guard


def utcnow():
    return datetime.now(UTC).isoformat()


class Engine:
    def __init__(
        self,
        state,
        staging,
        sources=(),
        *,
        rclone,
        mode="local",
        allow_cleanup=False,
        source_labels=(),
        source_uuids=(),
    ):
        self.mode = mode
        self.allow_cleanup = allow_cleanup
        self.state = Path(state).absolute()
        self.staging = Path(staging).absolute()
        if (
            self.state == self.staging
            or self.state in self.staging.parents
            or self.staging in self.state.parents
        ):
            raise SafetyError("overlapping_state_and_staging")
        for path in [self.state, self.staging]:
            if path.resolve() != path:
                raise SafetyError("symlink_root")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with staging_guard(self.staging,create=True):
            pass
        self.archive = self.state / "archive"
        self.archive.mkdir(exist_ok=True, mode=0o700)
        # The descriptor owns the process-lifetime advisory lock and is closed in shutdown().
        self.lock_file = open(self.state / "service.lock", "a")  # noqa: SIM115
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise SafetyError("service_already_running")
        self.store = Store(self.state / "ingest.sqlite3")
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ingest")
        self.run_condition = threading.Condition(self.lock)
        self.active_runs = set()
        self.cancels = {}
        self.futures = {}
        self._sources = {}
        self._explicit_paths = [Path(os.path.abspath(x)) for x in sources]
        labels = list(source_labels)
        uuids = list(source_uuids)
        self._explicit_labels = {
            str(path): labels[index] if index < len(labels) else None
            for index, path in enumerate(self._explicit_paths)
        }
        self._explicit_uuids = {
            str(path): uuids[index] if index < len(uuids) else None
            for index, path in enumerate(self._explicit_paths)
        }
        self.copier = RcloneCopier(rclone)
        with Root(self.staging) as root:
            self.target_identity = root.identity
        self.settings_version = 1
        if not self.store.get("settings", "global"):
            self.store.put("settings", "global", {"version": 1, "values": Settings().model_dump()})
        self.refresh_sources()
        for b in self.store.all("batch"):
            if b["state"] in {"QUEUED", "PREPARING", "COPYING", "VERIFYING", "FINALIZING", "RECOVERING"}:
                b["state"] = "INTERRUPTED"
                b["error"] = "服务重启，等待人工核对后继续"
                self.store.put("batch", b["batch_uid"], b)
        self.closed = False

    def refresh_sources(self):
        now = utcnow()
        found = []
        configured_paths = {str(path) for path in self._explicit_paths}
        for path in self._explicit_paths:
            try:
                record = source_record(
                    path,
                    label=self._explicit_labels[str(path)],
                    volume_uuid=self._explicit_uuids[str(path)],
                )
                if self.mode != "demo" and not self._explicit_uuids[str(path)]:
                    record["identity_confidence"] = "low"
                found.append(record)
            except (OSError, ValueError, SafetyError) as exc:
                found.append(
                    disconnected_source_record(
                        path,
                        label=self._explicit_labels[str(path)],
                        volume_uuid=self._explicit_uuids[str(path)],
                        reason=exc.code if isinstance(exc, SafetyError) else "source_unavailable",
                    )
                )
        if self.mode != "demo":
            explicit_identities = {tuple(item["identity"]) for item in found if item["connected"]}
            for item in linux_external_mounts([self.state, self.staging]):
                if tuple(item["identity"]) in explicit_identities:
                    continue
                if item["classification"] != "capture_media":
                    continue
                found.append(item)
        previous = {
            item["source_id"]: item
            for item in self.store.all("source")
            if item.get("source_id")
        }
        # Upgrade existing installations without losing the sources frozen in
        # older batch receipts.
        for batch in self.store.all("batch"):
            frozen = batch.get("source") or {}
            sid = frozen.get("source_id") or batch.get("source_id")
            if not sid or sid in previous:
                continue
            seen_at = batch.get("completed_at") or batch.get("created_at") or now
            previous[sid] = {
                **frozen,
                "source_id": sid,
                "label": frozen.get("label") or batch.get("source_label") or sid,
                "connected": False,
                "ever_connected": True,
                "first_seen_at": seen_at,
                "connected_at": seen_at,
                "last_seen_at": seen_at,
                "disconnected_at": seen_at,
            }
        previous.update(self._sources)
        new = {}
        for s in found:
            p = Path(s["path"])
            if any(p == r or p in r.parents or r in p.parents for r in [self.state, self.staging]):
                raise SafetyError("source_target_overlap")
            if (
                self.mode != "demo"
                and s["path"] not in configured_paths
                and s["identity"][0] == self.target_identity[0]
            ):
                continue
            old = previous.get(s["source_id"])
            if (
                old
                and old["identity"] == s["identity"]
                and old.get("mount_id") == s.get("mount_id")
                and old["connected"]
            ):
                s["session_id"] = old["session_id"]
            if s["connected"]:
                s.update(
                    ever_connected=True,
                    first_seen_at=(old or {}).get("first_seen_at") or now,
                    connected_at=(old or {}).get("connected_at")
                    if old and old.get("connected")
                    else now,
                    last_seen_at=now,
                    disconnected_at=None,
                )
            elif old and old.get("ever_connected"):
                s = {
                    **old,
                    **s,
                    "connected": False,
                    "ever_connected": True,
                    "first_seen_at": old.get("first_seen_at"),
                    "connected_at": old.get("connected_at"),
                    "last_seen_at": old.get("last_seen_at"),
                    "disconnected_at": old.get("disconnected_at") or now,
                }
            else:
                s.update(
                    ever_connected=False,
                    first_seen_at=None,
                    connected_at=None,
                    last_seen_at=None,
                    disconnected_at=None,
                )
            new[s["source_id"]] = s
        for key, old in previous.items():
            if key not in new:
                new[key] = {
                    **old,
                    "connected": False,
                    "disconnected_at": now if old.get("connected") else old.get("disconnected_at"),
                }
        for key, source in new.items():
            self.store.put("source", key, source)
        self._sources = new

    def sources(self):
        with self.lock:
            self.refresh_sources()
            return list(self._sources.values())

    def source(self, sid):
        s = self._sources.get(sid)
        if not s or not s["connected"]:
            raise SafetyError("source_unavailable")
        check_source(s)
        return s

    def settings(self):
        return {**self.store.get("settings", "global"), "schema": settings_schema()}

    def update_settings(self, expected_version, values, confirm_policy_change=False):
        with self.lock:
            old = self.store.get("settings", "global")
            if expected_version != old["version"]:
                raise SafetyError("version_conflict")
            if any(k in UNSUPPORTED_SETTINGS and v != old["values"].get(k) for k, v in values.items()):
                raise SafetyError("setting_not_implemented", "自动调度和永久删除尚未实现")
            new = Settings(**{**old["values"], **values})
            dangerous = old["values"]["require_handoff_confirmation"] and not new.require_handoff_confirmation
            if dangerous and not confirm_policy_change:
                raise SafetyError("policy_confirmation_required", "关闭转存要求可能清理唯一副本，请确认策略")
            current = {"version": old["version"] + 1, "values": new.model_dump()}
            self.store.put("settings", "global", current)
            self.store.event(
                "settings",
                {
                    "time": utcnow(),
                    "type": "settings_updated",
                    "version": current["version"],
                    "changed_keys": list(values),
                },
            )
            self.run_condition.notify_all()
            return {**current, "schema": settings_schema()}

    def scan(self, sid, selected_roots):
        settings = Settings(**self.settings()["values"])
        s = self.source(sid)
        value = scan_source(s, selected_roots, settings.exclude_os_junk)
        self.store.put("scan", value["scan_id"], value)
        return value

    def batch(self, uid):
        value = self.store.get("batch", uid)
        if not value:
            raise SafetyError("batch_not_found")
        return value

    def batches(self):
        return self.store.all("batch")

    def _save(self, b):
        self.store.put("batch", b["batch_uid"], b)

    def start(self, sid, scan_id, scan_revision, idempotency_key, background=True):
        request_digest = blake3(json.dumps([sid, scan_id, scan_revision]).encode()).hexdigest()
        if not idempotency_key or len(idempotency_key) > 200:
            raise SafetyError("invalid_idempotency_key")
        with self.lock:
            previous = self.store.db.execute(
                "SELECT digest,batch FROM requests WHERE key=?", (idempotency_key,)
            ).fetchone()
            if previous:
                if previous["digest"] != request_digest:
                    raise SafetyError("idempotency_conflict")
                return self.batch(previous["batch"])
            source = self.source(sid)
            scan = self.store.get("scan", scan_id)
            if (
                not scan
                or scan["source_id"] != sid
                or scan["revision"] != scan_revision
                or scan["source_session_id"] != source["session_id"]
            ):
                raise SafetyError("stale_scan")
            if not scan["complete"]:
                raise SafetyError("incomplete_scan", "扫描存在错误，不能启动复制")
            current = scan_source(source, scan["selected_roots"], scan["exclude_os_junk"])
            if (
                current["files"] != scan["files"]
                or current["directories"] != scan["directories"]
                or not current["complete"]
            ):
                raise SafetyError("stale_scan", "来源已变化，请重新扫描")
            if any(
                b["source_id"] == sid
                and b["state"] in {"QUEUED", "PREPARING", "COPYING", "VERIFYING", "FINALIZING", "RECOVERING"}
                for b in self.batches()
            ):
                raise SafetyError("source_busy")
            cfg = self.settings()
            with self.store.transaction():
                number = self.store.db.execute("INSERT INTO sequence DEFAULT VALUES").lastrowid
                uid = uuid.uuid4().hex
                batch_timezone = ZoneInfo(os.environ.get("TZ", "UTC"))
                bid = f"BATCH_{datetime.now(batch_timezone).strftime('%Y%m%d')}_{number:06}"
                directory = self.staging / bid
                directory.mkdir(mode=0o700)
                (directory / "SOURCE_DATA").mkdir()
                (directory / ".ingest").mkdir()
                (directory / ".ingest/partials").mkdir()
                (directory / ".ingest/revisions").mkdir()
                atomic_json(directory / ".ingest/owner.json", {"batch_uid": uid, "batch_id": bid})
                sync_dir(self.staging)
                b = {
                    "batch_uid": uid,
                    "batch_id": bid,
                    "source_id": sid,
                    "source_label": source["label"],
                    "source_session_id": source["session_id"],
                    "source": source,
                    "mode": self.mode,
                    "state": "QUEUED",
                    "result": "NOT_VERIFIED",
                    "created_at": utcnow(),
                    "completed_at": None,
                    "scope": {"scan_id": scan_id, "selected_roots": scan["selected_roots"]},
                    "copy_mode": "full" if scan["selected_roots"] == ["."] else "selected_folders",
                    "settings": cfg["values"],
                    "settings_version": cfg["version"],
                    "revision": 0,
                    "summary": {},
                    "progress": {
                        "stage": "QUEUED",
                        "relative_path": None,
                        "bytes": 0,
                        "total_bytes": scan["total_bytes"],
                        "current_file_bytes": 0,
                        "current_file_total_bytes": 0,
                        "speed_bps": 0,
                        "average_speed_bps": 0,
                        "eta_seconds": None,
                        "sampled_at": None,
                        "copy_started_at": None,
                    },
                    "lifecycle": {
                        "storage_state": "ACTIVE",
                        "expires_at": None,
                        "hold": False,
                        "handoff_confirmed": False,
                        "blocked_reasons": [],
                    },
                    "borrowed_batches": [],
                    "error": None,
                    "warnings": [],
                }
                for f in scan["files"]:
                    record = {
                        **f,
                        "file_id": uuid.uuid4().hex,
                        "copy_status": "pending",
                        "attempt_count": 0,
                        "error": None,
                        "existing_copy": None,
                    }
                    self.store.put_file(uid, sid, record)
                b["summary"] = self._summary(uid, scan)
                self._save(b)
                self.store.db.execute(
                    "INSERT INTO requests VALUES(?,?,?)", (idempotency_key, request_digest, uid)
                )
        self.cancels[uid] = threading.Event()
        if background:
            self.futures[uid] = self.pool.submit(self._run, uid)
        else:
            self._run(uid)
        return self.batch(uid)

    def _summary(self, uid, scan):
        files = self.store.files(uid)
        verified = [f for f in files if f["copy_status"] == "verified"]
        size_verified = [f for f in files if f["copy_status"] == "size_verified"]
        staged = [f for f in files if f["copy_status"] == "copied_unverified"]
        skipped = [f for f in files if f["copy_status"] == "skipped_existing"]
        failed = [f for f in files if f["copy_status"] == "failed"]
        copied = verified + size_verified + staged
        return {
            "selected_file_count": len(files),
            "selected_bytes": scan["total_bytes"],
            "source_file_count": len(files) if scan["selected_roots"] == ["."] else None,
            "source_observed_file_count": len(files),
            "source_scan_complete": scan["selected_roots"] == ["."],
            "selection_scan_complete": scan["complete"],
            "copy_required_count": len(files) - len(skipped),
            "copied_file_count": len(copied),
            "verified_file_count": len(verified),
            "size_verified_file_count": len(size_verified),
            "awaiting_hash_file_count": len(staged),
            "previously_ingested_count": len(skipped),
            "failed_file_count": len(failed),
            "pending_file_count": len(files) - len(copied) - len(skipped) - len(failed),
            "copied_bytes": sum(f["size_bytes"] for f in copied),
            "previously_ingested_bytes": sum(f["size_bytes"] for f in skipped),
            "photo_count": sum(f["media_type"] == "photo" for f in files),
            "video_count": sum(f["media_type"] == "video" for f in files),
            "other_count": sum(f["media_type"] == "other" for f in files),
            "selected_directory_count": len(scan["directories"]),
            "excluded_entry_count": len(scan["excluded"]),
        }

    def _event(self, b, kind, **values):
        event = {"time": utcnow(), "type": kind, **values}
        root = self.staging / b["batch_id"]
        for name in [".ingest/journal.jsonl", "ingest.log"]:
            with open(root / name, "a", encoding="utf-8") as out:
                out.write(json.dumps(event, ensure_ascii=False) + "\n")
                out.flush()
                os.fsync(out.fileno())
        self.store.event(b["batch_uid"], event)

    def _check(self, b):
        if self.cancels[b["batch_uid"]].is_set():
            raise SafetyError("operator_stop")
        try:
            expected_uuid = b["source"].get("volume_uuid")
            exact_mount, observed_uuid = mounted_volume_identity(b["source"]["path"])
            if expected_uuid and exact_mount and observed_uuid != expected_uuid:
                raise SafetyError("source_unavailable", "来源设备已断开或被其他卷替换")
            check_source(b["source"])
            with Root(self.staging) as r:
                if r.identity != self.target_identity:
                    raise SafetyError("target_unavailable")
        except OSError as exc:
            raise SafetyError("source_or_target_unavailable", str(exc)) from exc

    @staticmethod
    def _same_volume(first, second):
        first_uuid = first.get("volume_uuid")
        return bool(
            first_uuid
            and first.get("identity_confidence") == "high"
            and second.get("identity_confidence") == "high"
            and first_uuid == second.get("volume_uuid")
        )

    @staticmethod
    def _source_signature_matches(source, expected, observed):
        if expected == observed:
            return True
        # st_dev may change when the same UUID-backed card is mounted again.
        # The volume UUID plus inode, size and timestamps remain frozen.
        return bool(source.get("volume_uuid") and expected[1:] == observed[1:])

    def _progress(self, uid, stage, path=None, size=0, file_total=0):
        with self.lock:
            b = self.batch(uid)
            b["state"] = "VERIFYING" if stage in {"VERIFYING", "FINAL_SOURCE_CHECK"} else "COPYING"
            progress = b["progress"]
            summary = b.get("summary", {})
            copied = int(summary.get("copied_bytes") or 0)
            reused = int(summary.get("previously_ingested_bytes") or 0)
            now = time.time()
            previous_sample = progress.get("sampled_at")
            previous_file_bytes = int(progress.get("current_file_bytes") or 0)
            same_copy = (
                stage == "COPYING"
                and progress.get("stage") == "COPYING"
                and progress.get("relative_path") == path
                and previous_sample is not None
                and size >= previous_file_bytes
            )
            speed = 0.0
            if same_copy and now > previous_sample:
                instant = (size - previous_file_bytes) / (now - previous_sample)
                previous_speed = float(progress.get("speed_bps") or 0)
                speed = instant if previous_speed <= 0 else previous_speed * 0.65 + instant * 0.35
            copy_started = progress.get("copy_started_at")
            if stage == "COPYING" and copy_started is None:
                copy_started = now
            current_file_bytes = size if stage in {"COPYING", "VERIFYING"} else 0
            processed = min(int(progress.get("total_bytes") or 0), copied + reused + current_file_bytes)
            elapsed = now - copy_started if copy_started is not None else 0
            average = (copied + current_file_bytes) / elapsed if elapsed > 0 else 0.0
            remaining = max(0, int(progress.get("total_bytes") or 0) - processed)
            eta = remaining / speed if speed > 0 and stage == "COPYING" else None
            progress.update(
                stage=stage,
                relative_path=path,
                bytes=processed,
                current_file_bytes=current_file_bytes,
                current_file_total_bytes=file_total,
                speed_bps=round(speed, 2),
                average_speed_bps=round(average, 2),
                eta_seconds=round(eta) if eta is not None else None,
                sampled_at=now,
                copy_started_at=copy_started,
            )
            self._save(b)

    def _old_copy(self, b, f, source_hash, cfg, check):
        if not cfg.incremental or b["source"]["identity_confidence"] == "low":
            return None
        for prior_uid, old in self.store.history(b["source_id"], f["relative_path"], source_hash):
            if prior_uid == b["batch_uid"]:
                continue
            with self.lock:
                prior = self.batch(prior_uid)
                if prior["state"] != "COMPLETED" or prior["lifecycle"]["storage_state"] != "ACTIVE":
                    continue
                current = self.batch(b["batch_uid"])
                current["borrowed_batches"] = [prior_uid]
                self._save(current)
            try:
                destination = self.staging / prior["batch_id"] / old["destination_relative_path"]
                if (
                    destination.stat().st_size != f["size_bytes"]
                    or digest_file(destination, cfg.hash_threads, check) != source_hash
                ):
                    continue
                # Receipt is part of the evidence, not just the database status.
                receipt = json.loads((self.staging / prior["batch_id"] / "ingest_complete.json").read_text())
                if (
                    digest_file(self.staging / prior["batch_id"] / "ingest_manifest.md")
                    != receipt["md_blake3"]
                ):
                    continue
                return {
                    "batch_uid": prior_uid,
                    "batch_id": prior["batch_id"],
                    "root_id": "staging_main",
                    "staging_relative_path": prior["batch_id"] + "/" + old["destination_relative_path"],
                    "manifest_id": receipt["manifest_id"],
                    "verified_at": utcnow(),
                }
            except SafetyError as exc:
                if exc.code in {"operator_stop", "source_or_target_unavailable", "target_unavailable"}:
                    raise
                continue
            except (OSError, ValueError, KeyError):
                continue
        return None

    def _one_file(self, b, f, root, cfg):
        uid = b["batch_uid"]
        path = f["relative_path"]

        def check():
            self._check(b)

        with root.file(path) as fd:
            if not self._source_signature_matches(
                b["source"], f["signature"], signature(os.fstat(fd))
            ):
                raise SafetyError("source_changed", path)
            destination = self.staging / b["batch_id"] / "SOURCE_DATA" / path
            h = f.get("source_hash")
            staged_name = f.get("staged_temp")
            if f.get("copy_status") == "copying" and staged_name:
                interrupted = self.staging / b["batch_id"] / ".ingest/partials" / staged_name
                interrupted.unlink(missing_ok=True)
                f.update(copy_status="pending", staged_temp=None, error=None, resumed=True)
            if f.get("copy_status") == "copied_unverified" and h and staged_name:
                staged = self.staging / b["batch_id"] / ".ingest/partials" / staged_name
                if staged.is_file() and staged.stat().st_size == f["size_bytes"]:
                    f.update(error=None, resumed=True)
                    return
                f.update(copy_status="pending", staged_temp=None)
            if f.get("copy_status") == "size_verified" and destination.exists():
                if destination.stat().st_size != f["size_bytes"]:
                    raise SafetyError("target_conflict", path)
                f.update(error=None, resumed=True)
                return
            if f.get("copy_status") == "verified" and h and destination.exists():
                destination_stat = destination.stat()
                if destination_stat.st_size != f["size_bytes"] or (
                    f.get("destination_signature")
                    and signature(destination_stat) != f["destination_signature"]
                ):
                    raise SafetyError("target_conflict", path)
                f.update(error=None, resumed=True)
                return
            needs_pre_hash = destination.exists() or (
                cfg.incremental
                and b["source"]["identity_confidence"] != "low"
                and self.store.has_verified_history(b["source_id"], path)
            )
            if needs_pre_hash:
                self._progress(uid, "SOURCE_HASH", path, file_total=f["size_bytes"])
                h = digest_fd(fd, cfg.hash_threads, check)
                f["source_hash"] = h
            if destination.exists() or destination.is_symlink():
                if (
                    destination.stat().st_size != f["size_bytes"]
                    or digest_file(destination, cfg.hash_threads, check) != h
                ):
                    raise SafetyError("target_conflict", path)
                f.update(
                    copy_status="verified",
                    destination_hash=h,
                    destination_relative_path="SOURCE_DATA/" + path,
                    hash_match=True,
                    verified_at=utcnow(),
                    existing_copy=None,
                    error=None,
                    resumed=True,
                )
                return
            old = self._old_copy(b, f, h, cfg, check) if h else None
            if old:
                f.update(
                    copy_status="skipped_existing",
                    existing_copy=old,
                    existing_hash=h,
                    hash_match=True,
                    destination_relative_path=None,
                    verified_at=utcnow(),
                    error=None,
                )
                return
            temp = self.staging / b["batch_id"] / ".ingest/partials" / uuid.uuid4().hex
            f["attempt_count"] += 1
            f["copy_status"] = "copying"
            f["staged_temp"] = temp.name
            self.store.put_file(uid, b["source_id"], f)
            self._event(
                b,
                "COPY_STARTED",
                path=path,
                attempt=f["attempt_count"],
                temp=temp.name,
                source_hash=h,
                source_hash_during_copy=h is None,
            )
            self._progress(uid, "COPYING", path, file_total=f["size_bytes"])

            def report(n):
                self._progress(uid, "COPYING", path, n, f["size_bytes"])

            if h is None:
                h = self.copier.copy_and_hash(fd, temp, cfg, check, report)
                f["source_hash"] = h
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                self.copier.copy(fd, temp, cfg, check, report)
            if not self._source_signature_matches(
                b["source"], f["signature"], signature(os.fstat(fd))
            ):
                raise SafetyError("source_changed", path)
            with open(temp, "rb") as destfd:
                os.fsync(destfd.fileno())
            if temp.stat().st_size != f["size_bytes"]:
                raise SafetyError("size_mismatch", path)
            if cfg.verification_mode == "exact":
                f.update(
                    copy_status="copied_unverified",
                    staged_temp=temp.name,
                    destination_hash=None,
                    destination_relative_path=None,
                    hash_match=None,
                    verified_at=None,
                    existing_copy=None,
                    error=None,
                )
                self._event(b, "FILE_STAGED", path=path, source_hash=h, temp=temp.name)
            else:
                with Root(self.staging / b["batch_id"] / "SOURCE_DATA") as target:
                    target.publish(temp, path)
                f.update(
                    copy_status="size_verified",
                    staged_temp=None,
                    destination_hash=None,
                    destination_relative_path="SOURCE_DATA/" + path,
                    hash_match=None,
                    verified_at=utcnow(),
                    destination_signature=signature(destination.stat()),
                    existing_copy=None,
                    error=None,
                )
                self._event(b, "FILE_SIZE_VERIFIED", path=path, source_hash=h)

    def _verify_staged_files(self, b, cfg):
        uid = b["batch_uid"]
        for f in self.store.files(uid):
            if f["copy_status"] != "copied_unverified":
                continue
            path = f["relative_path"]
            temp = self.staging / b["batch_id"] / ".ingest/partials" / f["staged_temp"]
            try:
                self._progress(uid, "VERIFYING", path, f["size_bytes"], f["size_bytes"])
                actual = digest_file(temp, cfg.hash_threads, lambda: self._check(b))
                if actual != f["source_hash"] or temp.stat().st_size != f["size_bytes"]:
                    raise SafetyError("hash_mismatch", path)
                self._event(
                    b,
                    "VERIFIED_TEMP",
                    path=path,
                    source_hash=f["source_hash"],
                    destination_hash=actual,
                    temp=temp.name,
                )
                with Root(self.staging / b["batch_id"] / "SOURCE_DATA") as target:
                    target.publish(temp, path)
                destination = self.staging / b["batch_id"] / "SOURCE_DATA" / path
                f.update(
                    copy_status="verified",
                    staged_temp=None,
                    destination_hash=actual,
                    destination_relative_path="SOURCE_DATA/" + path,
                    hash_match=True,
                    verified_at=utcnow(),
                    destination_signature=signature(destination.stat()),
                    existing_copy=None,
                    error=None,
                )
                self._event(b, "FILE_PUBLISHED", path=path, hash=actual)
                f["metadata"] = extract_metadata(
                    destination, f["media_type"], cfg.metadata_level, cfg.metadata_timeout_seconds
                )
            except SafetyError as exc:
                if exc.code in {"operator_stop", "source_or_target_unavailable", "target_unavailable"}:
                    raise
                f.update(
                    copy_status="failed",
                    destination_relative_path=None,
                    error={"type": exc.code, "message": str(exc)},
                )
            except OSError as exc:
                if exc.errno in {28, 30, 122}:
                    raise SafetyError("target_unavailable", str(exc)) from exc
                f.update(
                    copy_status="failed",
                    destination_relative_path=None,
                    error={"type": "file_io_error", "message": str(exc)},
                )
            self.store.put_file(uid, b["source_id"], f)
            with self.lock:
                current = self.batch(uid)
                current["summary"] = self._summary(uid, self.store.get("scan", b["scope"]["scan_id"]))
                self._save(current)

    def _extract_size_only_metadata(self, b, cfg):
        for f in self.store.files(b["batch_uid"]):
            if f["copy_status"] != "size_verified" or f.get("metadata") is not None:
                continue
            destination = self.staging / b["batch_id"] / "SOURCE_DATA" / f["relative_path"]
            f["metadata"] = extract_metadata(
                destination, f["media_type"], cfg.metadata_level, cfg.metadata_timeout_seconds
            )
            self.store.put_file(b["batch_uid"], b["source_id"], f)

    def _effective_parallel_limit(self):
        configured = Settings(**self.settings()["values"]).max_parallel_sources
        try:
            cpu_count = max(1, os.cpu_count() or 1)
            if os.getloadavg()[0] > cpu_count * 1.25:
                return 1
        except OSError:
            pass
        try:
            pressure = Path("/proc/pressure/io").read_text()
            full = next(line for line in pressure.splitlines() if line.startswith("full "))
            avg10 = float(next(part.split("=", 1)[1] for part in full.split() if part.startswith("avg10=")))
            if avg10 >= 20:
                return 1
        except (OSError, StopIteration, ValueError):
            pass
        return configured

    def _acquire_run_slot(self, uid):
        with self.run_condition:
            while len(self.active_runs) >= self._effective_parallel_limit():
                if self.cancels[uid].is_set():
                    b = self.batch(uid)
                    b["state"] = "INTERRUPTED"
                    b["result"] = "NOT_VERIFIED"
                    b["error"] = "operator_stop: operator_stop"
                    b["progress"]["stage"] = "INTERRUPTED"
                    self._save(b)
                    return False
                self.run_condition.wait(timeout=0.5)
            self.active_runs.add(uid)
            b = self.batch(uid)
            b["state"] = "PREPARING"
            b["progress"]["stage"] = "PREPARING"
            self._save(b)
            return True

    def _release_run_slot(self, uid):
        with self.run_condition:
            self.active_runs.discard(uid)
            self.run_condition.notify_all()

    def _run(self, uid):
        if not self._acquire_run_slot(uid):
            return
        try:
            self._run_with_slot(uid)
        finally:
            self._release_run_slot(uid)

    def _run_with_slot(self, uid):
        # Shared for the entire copy/verification/receipt operation. Archive
        # cleanup holds the exclusive counterpart; new work waits safely.
        try:
            with staging_guard(self.staging,wait=True,cancel=self.cancels[uid]):
                return self._run_with_slot_coordinated(uid)
        except Exception as exc:
            b = self.batch(uid)
            b["state"] = "INTERRUPTED" if self.cancels[uid].is_set() else "FAILED"
            b["result"] = "NOT_VERIFIED"
            b["error"] = "staging_coordination: " + str(exc)
            b["completed_at"] = None
            b["progress"]["stage"] = b["state"]
            self._save(b)

    def _run_with_slot_coordinated(self, uid):
        b = self.batch(uid)
        scan = self.store.get("scan", b["scope"]["scan_id"])
        cfg = Settings(**b["settings"])
        # Never rewrite finalized evidence after a crash between receipt and DB commit.
        receipt_path = self.staging / b["batch_id"] / "ingest_complete.json"
        if receipt_path.exists() or receipt_path.is_symlink():
            b["state"] = "FAILED"
            b["result"] = "NOT_VERIFIED"
            b["error"] = "existing_receipt_requires_review: 已存在最终回执，保留证据并停止自动恢复"
            self._save(b)
            return
        try:
            self._check(b)
            reserve = 5 * 1024**2 if self.mode == "demo" else 5 * 1024**3
            if shutil.disk_usage(self.staging).free < scan["total_bytes"] + reserve:
                raise SafetyError("target_unavailable", "可用空间不足")
            with (
                Root(b["source"]["path"]) as root,
                Root(self.staging / b["batch_id"] / "SOURCE_DATA") as target,
            ):
                for directory in scan["directories"]:
                    target.mkdir(directory)
                for f in self.store.files(uid):
                    self._check(b)
                    for attempt in range(cfg.max_retries + 1):
                        try:
                            self._one_file(b, f, root, cfg)
                            break
                        except SafetyError as exc:
                            if exc.code in {
                                "operator_stop",
                                "source_unavailable",
                                "source_or_target_unavailable",
                                "target_unavailable",
                            }:
                                raise
                            f["error"] = {"type": exc.code, "message": str(exc)}
                            if exc.code not in {"hash_mismatch"}:
                                break
                        except OSError as exc:
                            if exc.errno in {28, 30, 122}:
                                raise SafetyError("target_unavailable", str(exc)) from exc
                            if exc.errno in {5, 6, 19, 116}:
                                raise SafetyError("source_unavailable", str(exc)) from exc
                            f["error"] = {"type": "file_io_error", "message": str(exc)}
                        if attempt < cfg.max_retries and self.cancels[uid].wait(
                            cfg.retry_delay_seconds
                        ):
                            raise SafetyError("operator_stop")
                    if f.get("error"):
                        f["copy_status"] = "failed"
                        f["destination_relative_path"] = None
                    self.store.put_file(uid, b["source_id"], f)
                    with self.lock:
                        current = self.batch(uid)
                        current["borrowed_batches"] = []
                        current["summary"] = self._summary(uid, scan)
                        current["progress"].update(
                            bytes=current["summary"]["copied_bytes"]
                            + current["summary"]["previously_ingested_bytes"],
                            current_file_bytes=0,
                            current_file_total_bytes=0,
                            speed_bps=0,
                            eta_seconds=None,
                        )
                        self._save(current)
                    self._event(
                        b,
                        "FILE_RESULT",
                        path=f["relative_path"],
                        status=f["copy_status"],
                        error=f.get("error"),
                    )
                if cfg.verification_mode == "exact":
                    self._verify_staged_files(b, cfg)
                else:
                    self._extract_size_only_metadata(b, cfg)
                # Certify the frozen selection, not an unbounded changing device.
                current_scan = scan_source(b["source"], scan["selected_roots"], cfg.exclude_os_junk)
                if not current_scan["complete"]:
                    raise SafetyError("source_changed", "终检枚举不完整")
                originals = {f["relative_path"]: f for f in scan["files"]}
                observed = {f["relative_path"]: f for f in current_scan["files"]}
                if any(
                    path not in observed
                    or not self._source_signature_matches(
                        b["source"], f["signature"], observed[path]["signature"]
                    )
                    for path, f in originals.items()
                ):
                    raise SafetyError("source_changed", "冻结范围发生变化")
                # The source was hashed while its bytes were copied. The frozen
                # signatures above and below detect any later source mutation
                # without reading the entire card for a second time.
                self._progress(uid, "FINAL_SOURCE_CHECK")
                latest = scan_source(b["source"], scan["selected_roots"], cfg.exclude_os_junk)
                final_observed = {f["relative_path"]: f for f in latest["files"]}
                if not latest["complete"] or any(
                    path not in final_observed
                    or not self._source_signature_matches(
                        b["source"], f["signature"], final_observed[path]["signature"]
                    )
                    for path, f in originals.items()
                ):
                    raise SafetyError("source_changed")
            b = self.batch(uid)
            b["summary"] = self._summary(uid, scan)
            b["state"] = "FAILED" if b["summary"]["failed_file_count"] else "COMPLETED"
            b["result"] = (
                "COPY_VERIFIED"
                if b["state"] == "COMPLETED" and cfg.verification_mode == "exact"
                else "COPY_SIZE_VERIFIED"
                if b["state"] == "COMPLETED"
                else "NOT_VERIFIED"
            )
            b["completed_at"] = utcnow() if b["state"] == "COMPLETED" else None
            exact_full = (
                b["state"] == "COMPLETED"
                and cfg.verification_mode == "exact"
                and b["copy_mode"] == "full"
                and len(originals) > 0
                and set(observed) == set(originals) == set(final_observed)
                and b["source"]["classification"] == "capture_media"
            )
            b["source_coverage"] = {
                "status": (
                    "FULLY_INGESTED"
                    if exact_full
                    else "FULLY_COPIED_SIZE_CHECKED"
                    if b["state"] == "COMPLETED"
                    and cfg.verification_mode == "size_only"
                    and b["copy_mode"] == "full"
                    and len(originals) > 0
                    and set(observed) == set(originals) == set(final_observed)
                    and b["source"]["classification"] == "capture_media"
                    else "NOT_FULL"
                ),
                "scope": "volume",
                "verified_at": utcnow() if exact_full else None,
                "copies_verified": 1 if exact_full else 0,
                "copies_required": 1,
                "format_action_available": False,
                "source_session_id": b["source_session_id"],
            }
            self._finish(b, scan)
        # The worker boundary must convert every failure into a durable batch result.
        except Exception as exc:  # noqa: BLE001
            b = self.batch(uid)
            code = exc.code if isinstance(exc, SafetyError) else "internal_error"
            b["state"] = (
                "INTERRUPTED"
                if code
                in {
                    "operator_stop",
                    "source_unavailable",
                    "source_or_target_unavailable",
                    "target_unavailable",
                }
                else "FAILED"
            )
            b["error"] = f"{code}: {exc}"
            b["result"] = "NOT_VERIFIED"
            b["completed_at"] = None
            b["summary"] = self._summary(uid, scan)
            b["borrowed_batches"] = []
            if receipt_path.exists():
                b["error"] += "; final_receipt_preserved_for_review"
                self._save(b)
                return
            try:
                self._finish(b, scan)
            # Preserve the primary failure even if final report generation also fails.
            except Exception as report_exc:  # noqa: BLE001
                b["error"] += "; report_write_error: " + str(report_exc)
                self._save(b)

    def _finish(self, b, scan):
        intended = b["state"]
        b["revision"] += 1
        b["retention_policy_at_completion"] = retention_snapshot(
            b["settings"], b["completed_at"], b["settings_version"]
        )
        b["lifecycle"]["expires_at"] = b["retention_policy_at_completion"]["expires_at"]
        b["progress"]["stage"] = intended
        b["progress"].update(
            bytes=b["progress"]["total_bytes"] if intended == "COMPLETED" else b["progress"]["bytes"],
            current_file_bytes=0,
            current_file_total_bytes=0,
            speed_bps=0,
            eta_seconds=0 if intended == "COMPLETED" else None,
            sampled_at=time.time(),
        )
        b["borrowed_batches"] = []
        if intended == "COMPLETED":
            intermediate = {**b, "state": "FINALIZING"}
            self._save(intermediate)
        self._event(b, "REPORT_FINALIZING", state=intended, revision=b["revision"])
        write_report(self.staging / b["batch_id"], b, self.store.files(b["batch_uid"]), scan, utcnow())
        self._save(b)

    def interrupt(self, uid):
        b = self.batch(uid)
        if b["state"] in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}:
            return b
        self.cancels[uid].set()
        return {"requested": True, "batch_uid": uid}

    def resume(self, uid, background=True):
        with self.lock:
            b = self.batch(uid)
            if b["state"] not in {"FAILED", "INTERRUPTED"} or b["lifecycle"]["storage_state"] != "ACTIVE":
                raise SafetyError("cannot_resume")
            if uid in self.futures and not self.futures[uid].done():
                raise SafetyError("batch_busy")
            if any(
                x["source_id"] == b["source_id"]
                and x["batch_uid"] != uid
                and x["state"] in {"QUEUED", "PREPARING", "COPYING", "VERIFYING", "FINALIZING", "RECOVERING"}
                for x in self.batches()
            ):
                raise SafetyError("source_busy")
            self.refresh_sources()
            source = self.source(b["source_id"])
            if source["identity"] != b["source"]["identity"] and not self._same_volume(
                b["source"], source
            ):
                raise SafetyError("source_changed")
            b["source"] = source
            b["source_session_id"] = source["session_id"]
            b["state"] = "RECOVERING"
            b["error"] = None
            self._save(b)
            self.cancels[uid] = threading.Event()
        if background:
            self.futures[uid] = self.pool.submit(self._run, uid)
        else:
            self._run(uid)
        return self.batch(uid)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for event in self.cancels.values():
            event.set()
        with self.run_condition:
            self.run_condition.notify_all()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.store.close()
        fcntl.flock(self.lock_file, fcntl.LOCK_UN)
        self.lock_file.close()
