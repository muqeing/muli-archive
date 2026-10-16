"""Small, derived SQLite catalog for completed archive history projections."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from itertools import zip_longest
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import threading


SCHEMA = "archive-history-catalog/1"
HEX = re.compile(r"[0-9a-f]{64}\Z")
_CHUNK = 400
_FINGERPRINT_CHUNK = 200


class HistoryCatalogError(ValueError):
    pass


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_identity(path: Path, *, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise HistoryCatalogError("历史目录数据库文件身份异常")
    return (info.st_dev, info.st_ino)


def _name(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HistoryCatalogError("元数据名称无效")
    return value


def _job_id(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HistoryCatalogError("任务编号无效")
    return value


def _unit_id(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HistoryCatalogError("素材单元编号无效")
    return value


def _fingerprint(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise HistoryCatalogError("内容指纹格式无效")
    digest, size = value
    if not isinstance(digest, str) or HEX.fullmatch(digest) is None:
        raise HistoryCatalogError("内容摘要无效")
    if type(size) is not int or size < 0:
        raise HistoryCatalogError("内容大小无效")
    return digest, size


class HistoryCatalog:
    """Identity-bound derived history index; receipts remain authoritative."""

    def __init__(self, path, identity):
        self.path = Path(path).absolute()
        try:
            parent = self.path.parent.lstat()
        except OSError as exc:
            raise HistoryCatalogError("历史目录数据库目录无效") from exc
        if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
            raise HistoryCatalogError("历史目录数据库目录无效")
        try:
            self._identity_json = _canonical(deepcopy(identity))
        except Exception as exc:
            raise HistoryCatalogError("历史目录服务身份不可序列化") from exc
        self._identity_digest = hashlib.sha256(self._identity_json.encode()).hexdigest()
        self._lock = threading.RLock()
        self._closed = False
        self._db = None
        self._db_identity = None
        existed = _file_identity(self.path, missing_ok=True) is not None
        try:
            self._db = sqlite3.connect(str(self.path), timeout=5.0,
                                       isolation_level=None, check_same_thread=False)
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db_identity = _file_identity(self.path)
            if existed:
                self._check_schema()
            else:
                self._create_schema()
            self._validate_rows()
        except HistoryCatalogError:
            self.close()
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            self.close()
            raise HistoryCatalogError("历史目录数据库打开失败") from exc

    def _ensure_open(self):
        if self._closed:
            raise HistoryCatalogError("历史目录数据库已关闭")

    def _guard(self):
        self._ensure_open()
        if _file_identity(self.path) != self._db_identity:
            raise HistoryCatalogError("历史目录数据库文件已被替换")

    def _create_schema(self):
        self._guard()
        with self._db:
            self._db.execute("CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.execute("CREATE TABLE user_meta (name TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
            self._db.execute("""CREATE TABLE items (
                job_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                scope_digest TEXT NOT NULL,
                item_json TEXT NOT NULL,
                PRIMARY KEY (job_id, unit_id)
            )""")
            self._db.execute("""CREATE TABLE latest (
                unit_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                scope_digest TEXT NOT NULL,
                item_json TEXT NOT NULL,
                FOREIGN KEY (job_id, unit_id) REFERENCES items(job_id, unit_id) ON DELETE CASCADE
            )""")
            self._db.execute("""CREATE TABLE fingerprints (
                job_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                blake3 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                PRIMARY KEY (job_id, unit_id, blake3, size_bytes),
                FOREIGN KEY (job_id, unit_id) REFERENCES items(job_id, unit_id) ON DELETE CASCADE
            )""")
            self._db.execute("""CREATE TABLE asset_latest (
                blake3 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                job_id TEXT NOT NULL, unit_id TEXT NOT NULL,
                PRIMARY KEY(blake3,size_bytes),
                FOREIGN KEY(job_id,unit_id) REFERENCES items(job_id,unit_id) ON DELETE CASCADE
            )""")
            self._db.execute("CREATE INDEX items_unit_latest ON items(unit_id, completed_at, job_id)")
            self._db.execute("CREATE INDEX items_job ON items(job_id)")
            self._db.execute("CREATE INDEX latest_page ON latest(completed_at DESC, unit_id DESC)")
            self._db.execute("CREATE INDEX fingerprints_content ON fingerprints(blake3, size_bytes, job_id)")
            self._db.executemany("INSERT INTO meta(name,value) VALUES (?,?)", [
                ("schema", SCHEMA),
                ("identity", self._identity_json),
                ("identity_digest", self._identity_digest),
                ("generation", "0"),
                ("history_total", "0"),
            ])

    def _check_schema(self):
        self._guard()
        if self._db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise HistoryCatalogError("历史目录数据库损坏")
        tables = {row[0] for row in self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != {"meta", "user_meta", "items", "latest", "fingerprints", "asset_latest"}:
            raise HistoryCatalogError("历史目录数据库结构异常")
        item_columns = [row[1] for row in self._db.execute("PRAGMA table_info(items)")]
        if item_columns != ["job_id", "unit_id", "completed_at", "scope_digest", "item_json"]:
            raise HistoryCatalogError("历史目录项目字段异常")
        latest_columns = [row[1] for row in self._db.execute("PRAGMA table_info(latest)")]
        if latest_columns != ["unit_id", "job_id", "completed_at", "scope_digest", "item_json"]:
            raise HistoryCatalogError("历史目录最新项目字段异常")
        fingerprint_columns = [row[1] for row in self._db.execute("PRAGMA table_info(fingerprints)")]
        if fingerprint_columns != ["job_id", "unit_id", "blake3", "size_bytes"]:
            raise HistoryCatalogError("历史目录指纹字段异常")
        if [r[1] for r in self._db.execute('PRAGMA table_info(asset_latest)')] != ['blake3','size_bytes','job_id','unit_id']:
            raise HistoryCatalogError('历史目录资产字段异常')
        indexes = {row[1] for row in self._db.execute("PRAGMA index_list(items)")} | \
                  {row[1] for row in self._db.execute("PRAGMA index_list(latest)")} | \
                  {row[1] for row in self._db.execute("PRAGMA index_list(fingerprints)")}
        if not {"items_unit_latest", "items_job", "latest_page", "fingerprints_content"} <= indexes:
            raise HistoryCatalogError("历史目录数据库索引异常")
        meta = dict(self._db.execute("SELECT name,value FROM meta"))
        if set(meta) != {"schema", "identity", "identity_digest", "generation", "history_total"}:
            raise HistoryCatalogError("历史目录数据库元数据异常")
        if (meta["schema"] != SCHEMA or meta["identity"] != self._identity_json
                or meta["identity_digest"] != self._identity_digest):
            raise HistoryCatalogError("历史目录服务身份不匹配")
        try:
            generation = int(meta["generation"])
        except (TypeError, ValueError) as exc:
            raise HistoryCatalogError("历史目录代际异常") from exc
        if generation < 0:
            raise HistoryCatalogError("历史目录代际异常")
        try:
            history_total = int(meta["history_total"])
        except (TypeError, ValueError) as exc:
            raise HistoryCatalogError("历史目录历史总数异常") from exc
        if history_total < 0:
            raise HistoryCatalogError("历史目录历史总数异常")

    @staticmethod
    def _prepare_item(job_id, pair):
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise HistoryCatalogError("历史项目必须包含 item 与 scope_digest")
        item, scope_digest = pair
        if not isinstance(item, Mapping):
            raise HistoryCatalogError("历史项目格式无效")
        item = deepcopy(dict(item))
        unit_id = _unit_id(item.get("unit_id"))
        if item.get("job_id") != job_id or not isinstance(item.get("completed_at"), str) \
                or not item["completed_at"]:
            raise HistoryCatalogError("历史项目任务或完成时间不匹配")
        if not isinstance(scope_digest, str) or HEX.fullmatch(scope_digest) is None:
            raise HistoryCatalogError("范围摘要无效")
        files = item.get("files", [])
        if not isinstance(files, list):
            raise HistoryCatalogError("历史项目文件列表无效")
        fingerprints = set()
        for file in files:
            if not isinstance(file, Mapping):
                raise HistoryCatalogError("历史项目文件记录无效")
            if file.get("blake3") is None:
                continue
            fingerprints.add(_fingerprint((file.get("blake3"), file.get("size_bytes"))))
        try:
            item_json = _canonical(item)
        except Exception as exc:
            raise HistoryCatalogError("历史项目不可序列化") from exc
        return (job_id, unit_id, item["completed_at"], scope_digest, item_json, fingerprints)

    def _validate_rows(self):
        self._guard()
        for job_id, unit_id, completed_at, scope_digest, item_json in self._db.execute(
                "SELECT job_id,unit_id,completed_at,scope_digest,item_json FROM items ORDER BY job_id,unit_id"):
            prepared = self._prepare_item(job_id, (json.loads(item_json), scope_digest))
            if prepared[1] != unit_id or prepared[2] != completed_at:
                raise HistoryCatalogError("历史目录项目派生字段异常")
        for job_id, unit_id, digest, size in self._db.execute(
                "SELECT job_id,unit_id,blake3,size_bytes FROM fingerprints"):
            _job_id(job_id)
            _unit_id(unit_id)
            _fingerprint((digest, size))
        expected = self._db.execute(
            "WITH ranked AS (SELECT unit_id,job_id,completed_at,scope_digest,item_json, "
            "ROW_NUMBER() OVER (PARTITION BY unit_id ORDER BY completed_at DESC,job_id DESC) AS rank "
            "FROM items) SELECT unit_id,job_id,completed_at,scope_digest,item_json "
            "FROM ranked WHERE rank=1 ORDER BY unit_id"
        )
        actual = self._db.execute(
            "SELECT unit_id,job_id,completed_at,scope_digest,item_json FROM latest ORDER BY unit_id"
        )
        expected_count = 0
        for expected_row, actual_row in zip_longest(expected, actual):
            expected_count += 1
            if expected_row is None or actual_row is None or tuple(expected_row) != tuple(actual_row):
                raise HistoryCatalogError("历史目录最新项目与历史记录不一致")
        if expected_count != self._db.execute("SELECT COUNT(*) FROM latest").fetchone()[0]:
            raise HistoryCatalogError("历史目录最新项目数量异常")
        total = self._generation_meta("history_total")
        if total != expected_count:
            raise HistoryCatalogError("历史目录历史总数与最新项目不一致")
        expected_assets=self._db.execute("""WITH ranked AS (
            SELECT f.blake3,f.size_bytes,f.job_id,f.unit_id,
            ROW_NUMBER() OVER(PARTITION BY f.blake3,f.size_bytes
            ORDER BY i.completed_at DESC,i.job_id DESC,i.unit_id DESC) AS rank
            FROM fingerprints f JOIN items i ON i.job_id=f.job_id AND i.unit_id=f.unit_id)
            SELECT blake3,size_bytes,job_id,unit_id FROM ranked WHERE rank=1 ORDER BY blake3,size_bytes""")
        actual_assets=self._db.execute('SELECT blake3,size_bytes,job_id,unit_id FROM asset_latest ORDER BY blake3,size_bytes')
        for expected_row,actual_row in zip_longest(expected_assets,actual_assets):
            if expected_row != actual_row:
                raise HistoryCatalogError('历史目录资产来源不一致')
        self._guard()

    @staticmethod
    def _strings(values, label):
        if isinstance(values, (str, bytes)):
            raise HistoryCatalogError(f"{label}范围无效")
        try:
            result = list(values)
        except TypeError as exc:
            raise HistoryCatalogError(f"{label}范围无效") from exc
        for value in result:
            if not isinstance(value, str) or not value or "\x00" in value:
                raise HistoryCatalogError(f"{label}无效")
        return sorted(set(result))

    def _generation(self):
        return self._generation_meta("generation")

    def _generation_meta(self, name):
        row = self._db.execute("SELECT value FROM meta WHERE name=?", (name,)).fetchone()
        if row is None:
            raise HistoryCatalogError("历史目录元数据缺失")
        try:
            value = int(row[0])
        except (TypeError, ValueError) as exc:
            raise HistoryCatalogError("历史目录元数据数值异常") from exc
        if value < 0:
            raise HistoryCatalogError("历史目录元数据数值异常")
        return value

    def _bump_generation(self):
        current = self._generation()
        self._db.execute("UPDATE meta SET value=? WHERE name='generation'", (str(current + 1),))
        return current + 1

    def _latest_count(self, unit_ids):
        total = 0
        units = sorted(set(unit_ids))
        for offset in range(0, len(units), _CHUNK):
            chunk = units[offset:offset + _CHUNK]
            marks = ",".join("?" for _ in chunk)
            total += self._db.execute(
                f"SELECT COUNT(*) FROM latest WHERE unit_id IN ({marks})", chunk
            ).fetchone()[0]
        return total

    def _refresh_latest(self, unit_ids, before=None):
        units = sorted(set(unit_ids))
        if before is None:
            before = self._latest_count(units)
        for unit_id in units:
            winner = self._db.execute(
                "SELECT unit_id,job_id,completed_at,scope_digest,item_json FROM items "
                "WHERE unit_id=? ORDER BY completed_at DESC,job_id DESC LIMIT 1", (unit_id,)
            ).fetchone()
            if winner is None:
                self._db.execute("DELETE FROM latest WHERE unit_id=?", (unit_id,))
            else:
                self._db.execute(
                    "INSERT INTO latest(unit_id,job_id,completed_at,scope_digest,item_json) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(unit_id) DO UPDATE SET job_id=excluded.job_id, "
                    "completed_at=excluded.completed_at, scope_digest=excluded.scope_digest, "
                    "item_json=excluded.item_json", winner
                )
        after = self._latest_count(units)
        total = self._generation_meta("history_total")
        if total + after - before < 0:
            raise HistoryCatalogError("历史目录历史总数异常")
        self._db.execute("UPDATE meta SET value=? WHERE name='history_total'",
                         (str(total + after - before),))

    def _refresh_assets(self, fingerprints):
        for hash_value,size in fingerprints:
            winner=self._db.execute("""SELECT f.job_id,f.unit_id FROM fingerprints f JOIN items i
                ON i.job_id=f.job_id AND i.unit_id=f.unit_id
                WHERE f.blake3=? AND f.size_bytes=?
                ORDER BY i.completed_at DESC,i.job_id DESC,i.unit_id DESC LIMIT 1""",(hash_value,size)).fetchone()
            if winner is None:
                self._db.execute('DELETE FROM asset_latest WHERE blake3=? AND size_bytes=?',(hash_value,size))
            else:
                self._db.execute('INSERT OR REPLACE INTO asset_latest VALUES(?,?,?,?)',(hash_value,size,*winner))

    @property
    def total(self):
        with self._lock:
            self._guard()
            return self._generation_meta('history_total')

    @property
    def generation(self):
        with self._lock:
            self._guard()
            value = self._generation()
            self._guard()
            return value

    def replace_job(self, job_id, items):
        job_id = _job_id(job_id)
        try:
            prepared = [self._prepare_item(job_id, pair) for pair in items]
        except TypeError as exc:
            raise HistoryCatalogError("历史项目列表无效") from exc
        if len({row[1] for row in prepared}) != len(prepared):
            raise HistoryCatalogError("同一任务包含重复素材单元")
        with self._lock:
            self._guard()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                affected_assets = set(self._db.execute('SELECT blake3,size_bytes FROM fingerprints WHERE job_id=?',(job_id,)))
                affected = {row[0] for row in self._db.execute("SELECT unit_id FROM items WHERE job_id=?", (job_id,))}
                affected_assets.update(fp for row in prepared for fp in row[5])
                affected |= {row[1] for row in prepared}
                before_count = self._latest_count(affected)
                self._db.execute("DELETE FROM fingerprints WHERE job_id=?", (job_id,))
                self._db.execute("DELETE FROM items WHERE job_id=?", (job_id,))
                for current_job, unit_id, completed_at, scope_digest, item_json, fingerprints in prepared:
                    self._db.execute(
                        "INSERT INTO items(job_id,unit_id,completed_at,scope_digest,item_json) VALUES (?,?,?,?,?)",
                        (current_job, unit_id, completed_at, scope_digest, item_json))
                    self._db.executemany(
                        "INSERT INTO fingerprints(job_id,unit_id,blake3,size_bytes) VALUES (?,?,?,?)",
                        ((current_job, unit_id, digest, size) for digest, size in fingerprints))
                self._refresh_latest(affected,before_count)
                self._refresh_assets(affected_assets)
                generation = self._bump_generation()
                self._guard()
                self._db.execute("COMMIT")
                return generation
            except HistoryCatalogError:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise HistoryCatalogError("历史目录任务替换失败") from exc

    def remove_job(self, job_id):
        job_id = _job_id(job_id)
        with self._lock:
            self._guard()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                present = self._db.execute("SELECT 1 FROM items WHERE job_id=? LIMIT 1", (job_id,)).fetchone()
                if present is None:
                    self._db.execute("COMMIT")
                    return self._generation()
                affected_assets = set(self._db.execute('SELECT blake3,size_bytes FROM fingerprints WHERE job_id=?',(job_id,)))
                affected = {row[0] for row in self._db.execute("SELECT unit_id FROM items WHERE job_id=?", (job_id,))}
                before_count = self._latest_count(affected)
                self._db.execute("DELETE FROM fingerprints WHERE job_id=?", (job_id,))
                self._db.execute("DELETE FROM items WHERE job_id=?", (job_id,))
                self._refresh_latest(affected,before_count)
                self._refresh_assets(affected_assets)
                generation = self._bump_generation()
                self._guard()
                self._db.execute("COMMIT")
                return generation
            except HistoryCatalogError:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise HistoryCatalogError("历史目录任务删除失败") from exc

    def job_ids(self, unit_ids=(), fingerprints=()):
        units = self._strings(unit_ids, "素材单元")
        try:
            fp_values = sorted({_fingerprint(value) for value in fingerprints})
        except TypeError as exc:
            raise HistoryCatalogError("内容指纹范围无效") from exc
        with self._lock:
            self._guard()
            result = set()
            for offset in range(0, len(units), _CHUNK):
                chunk = units[offset:offset + _CHUNK]
                marks = ",".join("?" for _ in chunk)
                result.update(row[0] for row in self._db.execute(
                    f"SELECT DISTINCT job_id FROM items WHERE unit_id IN ({marks})", chunk))
            for offset in range(0, len(fp_values), _FINGERPRINT_CHUNK):
                chunk = fp_values[offset:offset + _FINGERPRINT_CHUNK]
                where = " OR ".join("(blake3=? AND size_bytes=?)" for _ in chunk)
                args = [part for value in chunk for part in value]
                result.update(row[0] for row in self._db.execute(
                    f"SELECT DISTINCT job_id FROM fingerprints WHERE {where}", args))
            self._guard()
            return result

    def unit_ids(self, job_ids=()):
        jobs = self._strings(job_ids, "任务")
        with self._lock:
            self._guard()
            result = set()
            for offset in range(0, len(jobs), _CHUNK):
                chunk = jobs[offset:offset + _CHUNK]
                marks = ",".join("?" for _ in chunk)
                result.update(row[0] for row in self._db.execute(
                    f"SELECT DISTINCT unit_id FROM items WHERE job_id IN ({marks})", chunk))
            self._guard()
            return result

    def _matching_rows(self, unit_ids, fp_values):
        """Select one latest row per explicit unit and per input fingerprint."""
        selected = {}

        def remember(rows):
            for unit_id, scope_digest, completed_at, job_id in rows:
                previous = selected.get(unit_id)
                version = (completed_at, job_id, unit_id)
                if previous is None or version > previous[0]:
                    selected[unit_id] = (version, scope_digest, job_id)

        for offset in range(0, len(unit_ids), _CHUNK):
            chunk = unit_ids[offset:offset + _CHUNK]
            marks = ",".join("?" for _ in chunk)
            remember(self._db.execute(
                f"SELECT unit_id,scope_digest,completed_at,job_id FROM latest WHERE unit_id IN ({marks})", chunk))
        for offset in range(0, len(fp_values), _FINGERPRINT_CHUNK):
            chunk = fp_values[offset:offset + _FINGERPRINT_CHUNK]
            where = " OR ".join("(f.blake3=? AND f.size_bytes=?)" for _ in chunk)
            args = [part for value in chunk for part in value]
            remember(self._db.execute(
                "SELECT i.unit_id,i.scope_digest,i.completed_at,i.job_id FROM asset_latest f JOIN items i "
                "ON i.job_id=f.job_id AND i.unit_id=f.unit_id WHERE " + where, args))
        return selected

    def matching_scopes(self, unit_ids=(), fingerprints=()):
        """Return scope digests for latest rows selected by units or fingerprints."""
        units = self._strings(unit_ids, "素材单元")
        try:
            fp_values = sorted({_fingerprint(value) for value in fingerprints})
        except TypeError as exc:
            raise HistoryCatalogError("内容指纹范围无效") from exc
        with self._lock:
            self._guard()
            selected = self._matching_rows(units, fp_values)
            self._guard()
            return {unit_id: value[1] for unit_id, value in selected.items()}

    def matching_jobs(self, unit_ids=(), fingerprints=()):
        """Return jobs owning the latest rows selected by units or fingerprints."""
        units = self._strings(unit_ids, "素材单元")
        try:
            fp_values = sorted({_fingerprint(value) for value in fingerprints})
        except TypeError as exc:
            raise HistoryCatalogError("内容指纹范围无效") from exc
        with self._lock:
            self._guard()
            selected = self._matching_rows(units, fp_values)
            self._guard()
            return {value[2] for value in selected.values()}

    def page(self, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise HistoryCatalogError("历史分页参数无效")
        with self._lock:
            self._guard()
            total = self._generation_meta('history_total')
            rows = self._db.execute(
                "SELECT item_json FROM latest ORDER BY completed_at DESC, unit_id DESC LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()
            items = [json.loads(row[0]) for row in rows]
            self._guard()
            next_offset = offset + len(items) if offset + len(items) < total else None
            return {"items": items, "total": total, "next_offset": next_offset}

    def get_meta(self, name, default=None):
        _name(name)
        with self._lock:
            self._guard()
            row = self._db.execute("SELECT value_json FROM user_meta WHERE name=?", (name,)).fetchone()
            if row is None:
                return default
            try:
                value = json.loads(row[0])
            except (TypeError, ValueError) as exc:
                raise HistoryCatalogError("用户元数据损坏") from exc
            self._guard()
            return value

    def set_meta(self, name, value):
        _name(name)
        try:
            value_json = _canonical(value)
        except Exception as exc:
            raise HistoryCatalogError("用户元数据不可序列化") from exc
        with self._lock:
            self._guard()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute("INSERT INTO user_meta(name,value_json) VALUES (?,?) "
                                 "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json",
                                 (name, value_json))
                self._guard()
                self._db.execute("COMMIT")
            except HistoryCatalogError:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise HistoryCatalogError("用户元数据写入失败") from exc

    def close(self):
        with getattr(self, "_lock", threading.RLock()):
            if not getattr(self, "_closed", True):
                if self._db is not None:
                    self._db.close()
                self._closed = True
