"""Prewarm the six actual display samples per pending segment, off request path."""
import threading
import sqlite3

class PhotoWarmup:
    def __init__(self, index, previews):
        self.index=index;self.previews=previews;self.lock=threading.Lock()
        self.scope=None;self.offset=0;self.finished=False;self.states={};self.stop=threading.Event();self.wake=threading.Event()
        self.thread=threading.Thread(target=self._run,name='photo-preview-warmup',daemon=True)
        self.thread.start()

    def start_scope(self, descriptor):
        with self.lock:
            if self.scope==descriptor:return
            self.scope=dict(descriptor);self.offset=0;self.finished=False;self.states={}
            # Queued work for an obsolete pending scope is skipped before decode.
            with self.previews.lock:
                self.previews.scope_token=descriptor['file']
        self.wake.set()

    def close(self):
        self.stop.set();self.wake.set();self.thread.join(timeout=2)

    def snapshot(self, descriptor):
        with self.lock:
            return dict(self.states) if self.scope==descriptor else {}

    def _run(self):
        while not self.stop.is_set():
            self.wake.wait(.25);self.wake.clear()
            with self.lock:scope=self.scope;offset=self.offset;finished=self.finished
            if scope is None or finished or self.previews.queue.qsize()>96:continue
            try:
                ids=self.index.priority_ids(scope,offset)
                if not ids:
                    with self.lock:
                        if self.scope==scope:self.finished=True
                    continue
                units=self.index.lookup(scope,scope['report_id'],ids)
                result=self.previews.request_units(units,scope_token=scope['file'])
                with self.lock:
                    if self.scope!=scope:continue
                    self.states.update(result['previews'])
                    # Retry the same bounded slice until every item is cached
                    # or queued. A full queue must never lose prewarm work.
                    if not result['deferred'] and all(row['state']!='pending' for row in result['previews'].values()):
                        self.offset+=len(ids)
                        self.wake.set()
            except (OSError,ValueError,KeyError,sqlite3.DatabaseError):
                # A damaged/stale display index never changes archive authority.
                continue
