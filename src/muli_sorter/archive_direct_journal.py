"""Bounded per-file rename intents; legacy whole-request journals still recover."""
from copy import deepcopy
import re

from .archive import record
from .archive_io import ArchiveError, atomic_json
from .review import digest


ENTRY_STORAGE = 'per-file/v1'


def valid_signature(value, length):
    return isinstance(value, list) and len(value) == length and all(type(v) is int and v >= 0 for v in value)


class DirectJournal:
    def __init__(self, fd, name, journal, *, recovering):
        self.fd, self.name, self.journal = fd, name, journal
        storage = journal.get('entry_storage')
        if storage not in (None, ENTRY_STORAGE):
            raise ArchiveError('直接移动逐文件日志版本不受支持')
        self.per_file = storage == ENTRY_STORAGE
        self.owner = digest(journal['identity'])
        self.fixed = {}
        for entry in journal['files']:
            path = entry['source_path']
            if path in self.fixed:
                raise ArchiveError('直接移动逐文件日志路径重复')
            method = entry.get('method', 'rename')
            required = ('source_path', 'source_signature', 'source_parent', 'target_parent')
            self.fixed[path] = {k:deepcopy(entry[k]) for k in required}
            # Mixed direct journals bind the decision made for this row.  A
            # pre-existing target's identity is part of that decision; a
            # target created by an earlier rename is recorded later and is
            # therefore deliberately not fixed here.
            # Legacy journals had only rename entries. Bind that default so a
            # recovery side-entry cannot silently turn an old rename into a
            # duplicate-source unlink.
            self.fixed[path]['method'] = method
            if 'target_preexisting' in entry:
                self.fixed[path]['target_preexisting'] = entry['target_preexisting']
            if 'target_digest' in entry:
                self.fixed[path]['target_digest'] = entry['target_digest']
            if entry.get('target_preexisting'):
                if 'target_signature' not in entry:
                    raise ArchiveError('直接移动日志缺少预先存在目标身份')
                self.fixed[path]['target_signature'] = deepcopy(entry['target_signature'])
            if (not valid_signature(entry['source_signature'], 5) or
                    not valid_signature(entry['source_parent'], 2) or
                    not valid_signature(entry['target_parent'], 2)):
                raise ArchiveError('直接移动日志原始文件身份无效')
            if (method not in ('rename', 'skip_identical') or
                    entry.get('target_preexisting') is not None and
                    type(entry['target_preexisting']) is not bool or
                    'target_signature' in self.fixed[path] and
                    not valid_signature(self.fixed[path]['target_signature'], 5) or
                    'target_digest' in entry and
                    (not isinstance(entry['target_digest'], str) or
                     not re.fullmatch(r'[0-9a-f]{64}', entry['target_digest']))):
                raise ArchiveError('直接移动日志混合条目无效')
            if not self.per_file or not recovering:
                continue
            saved = record(fd, self.entry_name(path))
            if saved is None:
                if entry.get('state') != 'pending':
                    raise ArchiveError('直接移动逐文件回执缺失')
                continue
            actual = saved.get('entry')
            if (set(saved) != {'owner', 'entry'} or saved['owner'] != self.owner or
                    not isinstance(actual, dict) or
                    set(actual) - set(self.fixed[path]) - {'state', 'method', 'target_signature', 'target_preexisting', 'target_digest'} or
                    any(actual.get(k, 'rename' if k == 'method' else object()) != v
                        for k,v in self.fixed[path].items()) or
                    actual.get('state') not in ('renaming', 'removed') or
                    actual.get('method', 'rename') not in ('rename', 'skip_identical') or
                    (entry.get('state') == 'removed' and actual['state'] != 'removed') or
                    ('target_signature' in actual and not valid_signature(actual['target_signature'], 5)) or
                    ('target_preexisting' in actual and type(actual['target_preexisting']) is not bool) or
                    ('target_digest' in actual and (not isinstance(actual['target_digest'], str) or
                                                    not re.fullmatch(r'[0-9a-f]{64}', actual['target_digest']))) or
                    (actual['state'] == 'removed' and 'target_signature' not in actual)):
                raise ArchiveError('直接移动逐文件回执与原确认范围不符')
            entry.clear()
            entry.update(actual)
            if 'target_preexisting' in self.fixed[path]:
                entry.setdefault('target_preexisting', self.fixed[path]['target_preexisting'])
            if 'target_digest' in self.fixed[path]:
                entry.setdefault('target_digest', self.fixed[path]['target_digest'])

    def entry_name(self, path):
        return 'direct-entry-' + self.journal['identity']['job_id'] + '-' + digest(path) + '.json'

    def save_entry(self, entry):
        if not self.per_file:
            return self.save()
        if any(entry.get(k) != v for k,v in self.fixed[entry['source_path']].items()):
            raise ArchiveError('直接移动逐文件原始身份改变')
        atomic_json(self.fd, self.entry_name(entry['source_path']),
                    {'owner':self.owner, 'entry':entry})

    def save(self):
        atomic_json(self.fd, self.name, self.journal)
