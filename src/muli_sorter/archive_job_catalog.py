"""Small task summaries; unchanged completed task bodies leave the hot path."""
from copy import deepcopy
import heapq
import threading
from .order_feed_io import read_json

def summary(job):
    fields = ('job_id','status','summary','archive_options','projects','phase',
              'current_file_bytes','execution_strategy','direct_move_recovery_required',
              'completed_at','finished_at','completedAt','verification')
    result = {k:job[k] for k in fields if k in job}
    result['errors'] = list(job.get('errors') or []) + [r['error'] for r in job.get('outcomes',[]) if r.get('error')]
    return result

class JobCatalog:
    def __init__(self, root):
        self.root=root;self.lock=threading.RLock();self.rows={}

    def snapshot(self, limit=20):
        # Only recent summaries remain in memory. Old task files are cold; the
        # inexpensive directory metadata walk never parses their request bodies.
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('任务列表范围无效')
        with self.lock:
            def metadata():
                for path in self.root.glob('job-*.json'):
                    s=path.lstat()
                    yield ((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns),path)
            newest=heapq.nlargest(limit,metadata(),key=lambda row:(row[0][3],row[1].name))
            live={path for _,path in newest}
            for path in set(self.rows)-live:
                del self.rows[path]
            for token,path in newest:
                if path in self.rows and self.rows[path][0]==token:
                    continue
                body=read_json(path)
                after=path.lstat()
                if token!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    self.rows.pop(path,None)
                    continue
                self.rows[path]=(token,summary(body))
            return deepcopy([self.rows[path][1] for _,path in newest if path in self.rows])
