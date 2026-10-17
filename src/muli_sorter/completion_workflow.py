"""Durable pending-only handoff index owned by the archive service.

No history import or directory sweep. Registration precedes the job control
write, so a crash can leave harmless pending work but cannot lose an event.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from . import handoff_protocol as wire


class CompletionWorkflow:
    def __init__(self, state, identity):
        self.path = Path(state) / 'completion-workflow.sqlite3'
        self.lock = threading.RLock()
        for suffix in ('', '-wal', '-shm'):
            p = Path(str(self.path) + suffix)
            if p.exists() or p.is_symlink():
                s = p.lstat()
                if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                    raise ValueError('unsafe_completion_index')
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            PRAGMA busy_timeout=3000;
            CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL, activation REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS work (
                job_id TEXT PRIMARY KEY, binding TEXT NOT NULL, ready INTEGER NOT NULL CHECK(ready IN (0,1)),
                state TEXT NOT NULL CHECK(state IN ('pending','publishing','published','recorded','needs_review')), next_try REAL NOT NULL DEFAULT 0,
                fact TEXT, acknowledgement TEXT, error TEXT);
            CREATE INDEX IF NOT EXISTS pending ON work(state,ready,next_try,job_id);''')
        bound = wire.canonical(identity).decode()
        row = self.db.execute('SELECT value,activation FROM identity WHERE id=1').fetchone()
        if row is None:
            if self.db.execute('SELECT 1 FROM work LIMIT 1').fetchone():
                self.db.close();raise ValueError('completion_index_missing_identity')
            self.activation = time.time()
            self.db.execute('INSERT INTO identity VALUES(1,?,?)', (bound, self.activation))
        elif row[0] != bound:
            self.db.close()
            raise ValueError('completion_index_identity_changed')
        if row is not None:
            self.activation = row['activation']
        self.db.commit()
        os.chmod(self.path, 0o600)
        info = self.path.lstat()
        self.file_identity = (info.st_dev, info.st_ino)

    @contextmanager
    def guard(self, *, write=True):
        with self.lock:
            info = self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != self.file_identity):
                raise ValueError('completion_index_replaced')
            if write:
                with self.db:
                    yield
            else:
                yield

    @staticmethod
    def binding(job):
        return wire.digest({k: job.get(k) for k in
                            ('job_id', 'request_digest', 'status', 'completed_at',
                             'archive_options', 'example_data', 'summary')})

    def register(self, job):
        jid = job['job_id']
        if not wire.HEX.fullmatch(jid):
            raise ValueError('invalid_completion_job')
        binding = self.binding(job)
        ready = job.get('status') == 'completed'
        state = 'pending' if job.get('status') in ('queued', 'running', 'completed') else 'needs_review'
        with self.guard():
            old = self.db.execute('SELECT binding,state,fact FROM work WHERE job_id=?', (jid,)).fetchone()
            if old is None:
                self.db.execute('INSERT INTO work(job_id,binding,ready,state) VALUES(?,?,?,?)',
                                (jid, binding, ready, state))
            elif old['state'] == 'needs_review':
                return  # Only explicit review tooling may reopen failed feedback.
            elif old['fact'] is not None:
                if old['binding'] != binding:
                    raise ValueError('completed_job_changed')
            elif old['binding'] != binding or old['state'] != state:
                self.db.execute('UPDATE work SET binding=?,ready=?,state=?,next_try=0 WHERE job_id=?',
                                (binding, ready, state, jid))

    def next(self, clock):
        with self.guard(write=False):
            row = self.db.execute("SELECT * FROM work WHERE ready=1 AND state IN ('pending','publishing','published') AND next_try<=? ORDER BY next_try,job_id LIMIT 1", (clock,)).fetchone()
            return dict(row) if row else None

    def defer(self, jid, clock):
        with self.guard():
            self.db.execute('UPDATE work SET next_try=? WHERE job_id=?', (clock + 10, jid))

    def prepare(self, jid, binding, fact):
        raw = wire.canonical(fact).decode()
        if len(raw.encode()) > wire.MAX_BYTES:
            raise ValueError('completion_fact_too_large')
        with self.guard():
            row = self.db.execute('SELECT binding,fact FROM work WHERE job_id=?', (jid,)).fetchone()
            if row is None or row['binding'] != binding or row['fact'] not in (None, raw):
                raise ValueError('completion_publication_conflict')
            self.db.execute("UPDATE work SET state='publishing',fact=? WHERE job_id=?", (raw, jid))

    def published(self, jid):
        with self.guard():
            self.db.execute("UPDATE work SET state='published' WHERE job_id=? AND state='publishing'", (jid,))

    def record(self, jid, fact_id, ack):
        with self.guard():
            row = self.db.execute('SELECT fact FROM work WHERE job_id=?', (jid,)).fetchone()
            if row is None or not row[0] or json.loads(row[0])['fact_id'] != fact_id:
                raise ValueError('completion_ack_conflict')
            self.db.execute("UPDATE work SET state='recorded',acknowledgement=?,error=NULL WHERE job_id=?",
                            (wire.canonical(ack).decode(), jid))

    def fail(self, jid, reason):
        with self.guard():
            self.db.execute("UPDATE work SET state='needs_review',error=? WHERE job_id=?", (str(reason)[:200], jid))

    def status(self, jid):
        with self.guard(write=False):
            row = self.db.execute('SELECT state,acknowledgement,error FROM work WHERE job_id=?', (jid,)).fetchone()
            if row is None:
                return {'status': 'not_included', 'batches': []}
            if row['state'] == 'recorded':
                ack = json.loads(row['acknowledgement'])
                return {'status': 'recorded', 'historical_only': True,
                        'checked_at': ack['checked_at'], 'batches': ack['batches']}
            return {'status': 'needs_review' if row['state'] == 'needs_review' else 'pending',
                    'batches': [], 'reason': row['error']}

    def close(self):
        with self.lock:
            self.db.close()
