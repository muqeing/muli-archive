"""Durable, identity-bound index for archive target reservations.

``reservations.json`` remains the old service record and is used only once,
when an index is first opened.  The SQLite file is the authority after its
transactional migration is complete; no operation falls back to the JSON
record when the index cannot be trusted.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import threading
import unicodedata

from .archive_io import ArchiveError
from .intake import decode, read_bytes


SCHEMA = "archive-reservations/1"
DB_NAME = "reservations.sqlite3"
LEGACY_NAME = "reservations.json"
MAX_LEGACY_BYTES = 64 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _fold(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _path_identity(path: Path, *, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ArchiveError("归档预留索引文件身份异常")
    return (info.st_dev, info.st_ino)


def _legacy_signature(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ArchiveError("旧预留记录文件身份异常")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _validate_path(value, label):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ArchiveError(f"{label}无效")
    return value


def _validate_reservation(source_path, value):
    _validate_path(source_path, "来源路径")
    if not isinstance(value, Mapping) or set(value) != {"target_path", "blake3"}:
        raise ArchiveError("预留记录格式异常")
    target = _validate_path(value["target_path"], "目标路径")
    digest = value["blake3"]
    if not isinstance(digest, str) or HEX.fullmatch(digest) is None:
        raise ArchiveError("预留记录摘要无效")
    parent = str(PurePosixPath(target).parent)
    return {
        "target_path": target,
        "blake3": digest,
        "target_fold": _fold(target),
        "target_parent": parent,
        "target_parent_fold": _fold(parent),
    }


class ReservationIndex:
    """A small SQLite authority for source-to-target reservations.

    The database is owned by the service directory and bound to ``identity``.
    Migration is deliberately one transaction: an interrupted migration leaves
    the database incomplete and therefore unusable until the immutable legacy
    input can be migrated successfully.
    """

    def __init__(self, root, identity):
        self.root = Path(root).absolute()
        try:
            root_info = self.root.lstat()
        except OSError as exc:
            raise ArchiveError("归档预留索引目录无效") from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise ArchiveError("归档预留索引目录无效")
        try:
            copied_identity = deepcopy(identity)
            identity_json = _canonical(copied_identity).decode("utf-8")
        except Exception as exc:
            raise ArchiveError("服务身份不可序列化") from exc
        self._identity = copied_identity
        self._identity_json = identity_json
        self._identity_digest = _digest(copied_identity)
        self.path = self.root / DB_NAME
        self.legacy_path = self.root / LEGACY_NAME
        self._lock = threading.RLock()
        self._closed = False
        self._db = None
        self._db_identity = None
        existed = _path_identity(self.path, missing_ok=True) is not None
        try:
            self._db = sqlite3.connect(str(self.path), timeout=5.0,
                                       isolation_level=None,
                                       check_same_thread=False)
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db_identity = _path_identity(self.path)
            if not existed:
                self._create_schema()
            else:
                self._check_schema()
            self._migrate_if_needed()
            self._check_legacy_binding()
            self._validate_rows()
        except ArchiveError:
            self.close()
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            self.close()
            raise ArchiveError("归档预留索引打开失败") from exc

    @property
    def identity(self):
        with self._lock:
            self._ensure_open()
            return deepcopy(self._identity)

    def _ensure_open(self):
        if self._closed:
            raise ArchiveError("归档预留索引已关闭")

    def _guard_db(self):
        self._ensure_open()
        if _path_identity(self.path) != self._db_identity:
            raise ArchiveError("归档预留索引文件已被替换")

    def _create_schema(self):
        self._guard_db()
        with self._db:
            self._db.execute("CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.execute("""CREATE TABLE reservations (
                source_path TEXT PRIMARY KEY,
                target_path TEXT NOT NULL,
                target_fold TEXT NOT NULL,
                target_parent TEXT NOT NULL,
                target_parent_fold TEXT NOT NULL,
                blake3 TEXT NOT NULL
            )""")
            self._db.execute("CREATE INDEX reservations_target_fold ON reservations(target_fold)")
            self._db.execute("CREATE INDEX reservations_target_parent_fold ON reservations(target_parent_fold, source_path)")
            self._db.executemany("INSERT INTO meta(name, value) VALUES (?, ?)", [
                ("schema", SCHEMA),
                ("identity", self._identity_json),
                ("identity_digest", self._identity_digest),
                ("migration_complete", "0"),
            ])

    def _check_schema(self):
        self._guard_db()
        if self._db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ArchiveError("归档预留索引数据库损坏")
        tables = {row[0] for row in self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != {"meta", "reservations"}:
            raise ArchiveError("归档预留索引结构异常")
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(reservations)")]
        expected = ["source_path", "target_path", "target_fold", "target_parent",
                    "target_parent_fold", "blake3"]
        if columns != expected:
            raise ArchiveError("归档预留索引字段异常")
        indexes = {row[1] for row in self._db.execute("PRAGMA index_list(reservations)")}
        if not {"reservations_target_fold", "reservations_target_parent_fold"} <= indexes:
            raise ArchiveError("归档预留索引缺少目标目录索引")
        meta = dict(self._db.execute("SELECT name, value FROM meta"))
        if set(meta) != {"schema", "identity", "identity_digest", "migration_complete", "legacy_signature"}:
            # A database created by this class before migration has no legacy
            # signature yet; the migration path below fills it transactionally.
            if set(meta) != {"schema", "identity", "identity_digest", "migration_complete"}:
                raise ArchiveError("归档预留索引元数据异常")
        if (meta.get("schema") != SCHEMA or meta.get("identity") != self._identity_json
                or meta.get("identity_digest") != self._identity_digest):
            raise ArchiveError("归档预留索引服务身份不匹配")
        if meta.get("migration_complete") not in {"0", "1"}:
            raise ArchiveError("归档预留索引迁移状态异常")
        if meta.get("migration_complete") == "0" and self._db.execute(
                "SELECT 1 FROM reservations LIMIT 1").fetchone() is not None:
            raise ArchiveError("归档预留索引迁移未完成但已有数据")

    def _read_legacy(self):
        signature = _legacy_signature(self.legacy_path)
        if signature == "absent":
            return {}, signature
        try:
            raw = read_bytes(self.root, LEGACY_NAME)
            if len(raw) > MAX_LEGACY_BYTES:
                raise ArchiveError("旧预留记录过大")
            value = decode(raw)
        except ArchiveError:
            raise
        except Exception as exc:
            raise ArchiveError("旧预留记录读取失败") from exc
        if _legacy_signature(self.legacy_path) != signature:
            raise ArchiveError("旧预留记录读取期间发生变化")
        if not isinstance(value, dict):
            raise ArchiveError("旧预留记录顶层格式异常")
        rows = {}
        for source_path in sorted(value):
            rows[source_path] = _validate_reservation(source_path, value[source_path])
        self._validate_target_conflicts(rows.values())
        return rows, signature

    def _migrate_if_needed(self):
        self._guard_db()
        migration = self._db.execute(
            "SELECT value FROM meta WHERE name='migration_complete'").fetchone()
        if migration != ("0",):
            if migration != ("1",):
                raise ArchiveError("归档预留索引迁移状态异常")
            return
        rows, signature = self._read_legacy()
        if _legacy_signature(self.legacy_path) != signature:
            raise ArchiveError("旧预留记录已被替换")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            for source_path in sorted(rows):
                row = rows[source_path]
                self._db.execute(
                    "INSERT INTO reservations(source_path,target_path,target_fold,target_parent,target_parent_fold,blake3) "
                    "VALUES (?,?,?,?,?,?)",
                    (source_path, row["target_path"], row["target_fold"], row["target_parent"],
                     row["target_parent_fold"], row["blake3"]))
            self._db.execute("INSERT INTO meta(name,value) VALUES ('legacy_signature',?)", (
                json.dumps(signature, separators=(",", ":"), ensure_ascii=False),))
            self._db.execute("UPDATE meta SET value='1' WHERE name='migration_complete'")
            if _legacy_signature(self.legacy_path) != signature:
                raise ArchiveError("旧预留记录已被替换")
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _check_legacy_binding(self):
        self._guard_db()
        row = self._db.execute("SELECT value FROM meta WHERE name='legacy_signature'").fetchone()
        if row is None:
            raise ArchiveError("归档预留索引缺少迁移绑定")
        try:
            stored = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise ArchiveError("归档预留索引迁移绑定异常") from exc
        if stored != _legacy_signature(self.legacy_path):
            raise ArchiveError("旧预留记录已被替换")

    @staticmethod
    def _validate_target_conflicts(rows):
        owners = {}
        for row in rows:
            owners.setdefault(row["target_fold"], []).append(row)
        for values in owners.values():
            first = values[0]
            if any(row["target_path"] != first["target_path"] or row["blake3"] != first["blake3"]
                   for row in values[1:]):
                raise ArchiveError("目标路径存在大小写或 Unicode 冲突")

    def _validate_rows(self):
        self._guard_db()
        current_fold = None
        representative = None
        for source_path, target_path, target_fold, target_parent, target_parent_fold, digest in self._db.execute(
                "SELECT source_path,target_path,target_fold,target_parent,target_parent_fold,blake3 "
                "FROM reservations ORDER BY target_fold, source_path"):
            validated = _validate_reservation(source_path, {"target_path": target_path, "blake3": digest})
            if (validated["target_fold"], validated["target_parent"], validated["target_parent_fold"]) != \
                    (target_fold, target_parent, target_parent_fold):
                raise ArchiveError("归档预留索引记录派生字段异常")
            if current_fold != validated["target_fold"]:
                current_fold = validated["target_fold"]
                representative = (validated["target_path"], validated["blake3"])
            elif (validated["target_path"], validated["blake3"]) != representative:
                raise ArchiveError("目标路径存在大小写或 Unicode 冲突")
        self._guard_db()

    @staticmethod
    def _query_chunk_size():
        return 400

    @staticmethod
    def _source_list(source_paths):
        if isinstance(source_paths, (str, bytes)):
            raise ArchiveError("来源路径范围无效")
        try:
            paths = list(source_paths)
        except TypeError as exc:
            raise ArchiveError("来源路径范围无效") from exc
        for path in paths:
            _validate_path(path, "来源路径")
        return sorted(set(paths))

    def get(self, source_path):
        with self._lock:
            self._guard_db()
            self._check_legacy_binding()
            _validate_path(source_path, "来源路径")
            row = self._db.execute(
                "SELECT target_path,blake3 FROM reservations WHERE source_path=?", (source_path,)
            ).fetchone()
            self._guard_db()
            return None if row is None else {"target_path": row[0], "blake3": row[1]}

    def subset(self, source_paths):
        with self._lock:
            self._guard_db()
            self._check_legacy_binding()
            paths = self._source_list(source_paths)
            if not paths:
                return {}
            result = {}
            for offset in range(0, len(paths), self._query_chunk_size()):
                chunk = paths[offset:offset + self._query_chunk_size()]
                marks = ",".join("?" for _ in chunk)
                rows = self._db.execute(
                    f"SELECT source_path,target_path,blake3 FROM reservations WHERE source_path IN ({marks}) "
                    "ORDER BY source_path", chunk).fetchall()
                result.update({source: {"target_path": target, "blake3": digest}
                               for source, target, digest in rows})
            self._guard_db()
            return result

    def owners(self, target_path):
        """Return all source owners for one normalized target path."""
        with self._lock:
            self._guard_db()
            self._check_legacy_binding()
            _validate_path(target_path, "目标路径")
            rows = self._db.execute(
                "SELECT source_path,target_path,blake3 FROM reservations "
                "WHERE target_fold=? ORDER BY source_path", (_fold(target_path),)
            ).fetchall()
            self._guard_db()
            return {source: {"target_path": target, "blake3": digest}
                    for source, target, digest in rows}

    def destinations(self, parent_paths):
        with self._lock:
            self._guard_db()
            self._check_legacy_binding()
            if isinstance(parent_paths, (str, bytes)):
                raise ArchiveError("目标目录范围无效")
            try:
                parents = list(parent_paths)
            except TypeError as exc:
                raise ArchiveError("目标目录范围无效") from exc
            for parent in parents:
                _validate_path(parent, "目标目录")
            folded = sorted({_fold(parent) for parent in parents})
            if not folded:
                return {}
            marks = ",".join("?" for _ in folded)
            rows = self._db.execute(
                f"SELECT source_path,target_path,blake3 FROM reservations "
                f"WHERE target_parent_fold IN ({marks}) ORDER BY source_path", folded).fetchall()
            self._guard_db()
            return {source: {"target_path": target, "blake3": digest}
                    for source, target, digest in rows}

    def reserve(self, rows):
        if not isinstance(rows, Mapping):
            raise ArchiveError("预留记录范围无效")
        prepared = {source: _validate_reservation(source, value) for source, value in rows.items()}
        self._validate_target_conflicts(prepared.values())
        with self._lock:
            self._guard_db()
            self._check_legacy_binding()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                for source_path in sorted(prepared):
                    row = prepared[source_path]
                    prior = self._db.execute(
                        "SELECT target_path,blake3 FROM reservations WHERE source_path=?", (source_path,)
                    ).fetchone()
                    expected = (row["target_path"], row["blake3"])
                    if prior is not None:
                        if tuple(prior) != expected:
                            raise ArchiveError("素材已经提交过其他归档归属")
                        continue
                    owners = self._db.execute(
                        "SELECT target_path,blake3 FROM reservations WHERE target_fold=?",
                        (row["target_fold"],),
                    ).fetchall()
                    if any(tuple(owner) != expected for owner in owners):
                        raise ArchiveError("目标路径已被其他归档任务占用")
                    self._db.execute(
                        "INSERT INTO reservations(source_path,target_path,target_fold,target_parent,target_parent_fold,blake3) "
                        "VALUES (?,?,?,?,?,?)",
                        (source_path, row["target_path"], row["target_fold"], row["target_parent"],
                         row["target_parent_fold"], row["blake3"]),
                    )
                self._guard_db()
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
        return self.subset(prepared)

    def export_rollback(self, filename):
        """Write a new, bounded-memory rollback snapshot; never replace authority.

        Caller must hold the archive service's exclusive directory lock and
        stop all business writes before using this output for an old image.
        """
        if not isinstance(filename,str) or not re.fullmatch(r'reservations-rollback-[a-zA-Z0-9_-]+\.json',filename):
            raise ArchiveError('回退导出文件名无效')
        with self._lock:
            self._guard_db();self._check_legacy_binding()
            from .archive_io import directory
            with directory(self.root) as root_fd:
                fd=os.open(filename,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root_fd)
                hasher=hashlib.sha256();count=0
                try:
                    with os.fdopen(fd,'wb') as stream:
                        def emit(raw):
                            stream.write(raw);hasher.update(raw)
                        emit(b'{')
                        for source,target,content in self._db.execute(
                                'SELECT source_path,target_path,blake3 FROM reservations ORDER BY source_path'):
                            if count:emit(b',')
                            emit(_canonical(source)+b':'+_canonical({'target_path':target,'blake3':content}))
                            count+=1
                        emit(b'}\n');stream.flush();os.fsync(stream.fileno())
                    self._guard_db();self._check_legacy_binding();os.fsync(root_fd)
                except BaseException:
                    # A partial export remains visibly incomplete; it is never
                    # installed as reservations.json and never auto-replayed.
                    raise
            digest=hasher.hexdigest()
            with (self.root/filename).open('rb') as stream:
                verified=hashlib.file_digest(stream,'sha256').hexdigest()
            if verified!=digest:
                raise ArchiveError('回退导出回读摘要不一致')
            return {'file':filename,'count':count,'sha256':digest,'legacy_replaced':False}

    def close(self):
        with getattr(self, "_lock", threading.RLock()):
            if not getattr(self, "_closed", True):
                if self._db is not None:
                    self._db.close()
                self._closed = True
