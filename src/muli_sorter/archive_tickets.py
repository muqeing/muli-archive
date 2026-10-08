"""Durable, bounded cache for confirmed archive preview tickets.

Ticket files are addressed directly by their token.  The directory is never
inventoried: memory eviction only drops the in-process copy and leaves the
durable ticket available for a later lookup or process restart.
"""

from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
import json
import re
from pathlib import Path
import threading

from .archive_io import ArchiveError
from .order_feed_io import atomic_json, read_json


SCHEMA = "archive-preview-ticket/1"
MAX_BYTES = 48 * 1024 * 1024
TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_MISSING = object()


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return sha256(encoded).hexdigest()


class _MissingTicket(Exception):
    """Internal marker separating a missing file from a rejected file."""


class PersistentTickets:
    """A small mapping with durable per-token records and an LRU memory cache.

    ``root`` is a service-owned directory.  ``identity`` is copied at
    construction and is embedded in every record, so a ticket cannot be
    reused by another service instance.  Mutating an object returned from the
    memory cache changes only that cache entry; the durable binding remains
    the record written by ``__setitem__``.
    """

    def __init__(self, root, identity, memory_items=2):
        if type(memory_items) is not int or memory_items < 0:
            raise ArchiveError("预览票据内存容量无效")
        try:
            copied_identity = deepcopy(identity)
            # Validate the identity using the same canonical JSON rules as the
            # durable record before accepting it as a service binding.
            _digest(copied_identity)
        except Exception as exc:
            raise ArchiveError("服务身份不可序列化") from exc
        self.root = Path(root).absolute()
        self._identity = copied_identity
        self.memory_items = memory_items
        self._cache = OrderedDict()
        self._lock = threading.RLock()

    @property
    def identity(self):
        """Return a copy so callers cannot alter future record bindings."""
        with self._lock:
            return deepcopy(self._identity)

    @staticmethod
    def _validate_token(token):
        if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
            raise ArchiveError("预览票据编号无效")
        return token

    def _path(self, token):
        return self.root / (self._validate_token(token) + ".json")

    def _record(self, token, payload):
        body = {
            "schema": SCHEMA,
            "token": token,
            "identity": deepcopy(self._identity),
            "payload": payload,
        }
        return {**body, "digest": _digest(body)}

    @staticmethod
    def _reject(message, exc=None):
        error = ArchiveError(message)
        if exc is not None:
            raise error from exc
        raise error

    def _decode(self, token, value):
        if (not isinstance(value, dict) or
                set(value) != {"schema", "token", "identity", "payload", "digest"}):
            self._reject("预览票据记录格式异常")
        body = {key: value[key] for key in ("schema", "token", "identity", "payload")}
        if (value["schema"] != SCHEMA or value["token"] != token or
                value["identity"] != self._identity or
                not isinstance(value["digest"], str) or
                value["digest"] != _digest(body)):
            self._reject("预览票据记录绑定不匹配")
        return value["payload"]

    def _read_disk(self, token):
        path = self._path(token)
        try:
            value = read_json(path)
        except FileNotFoundError as exc:
            raise _MissingTicket from exc
        except Exception as exc:
            # read_json already performs the bounded, O_NOFOLLOW read.  Turn
            # malformed JSON, oversize files, and filesystem safety failures
            # into the archive service's fail-closed error type.
            self._reject("预览票据读取失败", exc)
        return self._decode(token, value)

    def _remember(self, token, payload):
        if self.memory_items == 0:
            return payload
        self._cache[token] = payload
        self._cache.move_to_end(token)
        while len(self._cache) > self.memory_items:
            self._cache.popitem(last=False)
        return payload

    def __setitem__(self, token, payload):
        with self._lock:
            token = self._validate_token(token)
            try:
                stored = deepcopy(payload)
                # This also rejects NaN and other values that cannot be represented
                # by the canonical JSON record.
                _digest(stored)
            except Exception as exc:
                self._reject("预览票据内容不可序列化", exc)

            path = self._path(token)
            # Do not let a service with a different binding replace an existing
            # ticket in the same state directory.
            try:
                self._read_disk(token)
            except _MissingTicket:
                pass
            try:
                record = self._record(token, stored)
                atomic_json(path, record)
            except ArchiveError:
                raise
            except Exception as exc:
                self._reject("预览票据写入失败", exc)
            self._remember(token, stored)

    def get(self, token, default=None):
        with self._lock:
            token = self._validate_token(token)
            if token in self._cache:
                value = self._cache[token]
                self._cache.move_to_end(token)
                return value
            try:
                value = self._read_disk(token)
            except _MissingTicket:
                return default
            return self._remember(token, value)

    def __getitem__(self, token):
        value = self.get(token, _MISSING)
        if value is _MISSING:
            raise KeyError(token)
        return value

    def __len__(self):
        with self._lock:
            return len(self._cache)

    def __iter__(self):
        # Iteration is intentionally limited to the bounded memory cache.
        with self._lock:
            return iter(tuple(self._cache))

    def items(self):
        with self._lock:
            return tuple(self._cache.items())

    def pop(self, token, default=_MISSING):
        """Evict a cached value without deleting its durable ticket."""
        with self._lock:
            token = self._validate_token(token)
            if token in self._cache:
                return self._cache.pop(token)
            if default is not _MISSING:
                return default
            raise KeyError(token)
