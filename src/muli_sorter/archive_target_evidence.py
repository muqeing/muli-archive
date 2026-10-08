"""Selected-only verified receipt evidence; unverified copy summaries are never proof."""
from .archive import record
from .archive_io import signature,ArchiveError,directory
from .review import digest

class SelectedTargetEvidence:
    def __init__(self, service, selected, fallback):
        self.fallback=fallback;self.values={}
        with directory(service.state/'units') as fd:
            for item in selected:
                identity={'unit_id':item['unit']['unit_id'],'project_id':item['project']['project_id'],
                          'project_path':item['project']['path'],'files':item['rows']}
                jid=digest(identity)
                try:
                    job=record(fd,'job-'+jid+'.json');receipt=record(fd,'receipt-'+jid+'.json')
                    if not job or not receipt:continue
                    if job.get('identity')!=identity or job.get('state')!='completed':continue
                    if (receipt.get('status')!='completed' or receipt.get('job_id')!=jid or
                        receipt.get('unit_id')!=identity['unit_id'] or
                        receipt.get('example_data') is not (not service.production) or
                        receipt.get('storage') not in ('independent_copy','same_volume_move')):continue
                    rows=receipt['files']
                    if len(rows)!=len(identity['files']) or rows!=job.get('files'):continue
                    verified=[]
                    for expected,row in zip(identity['files'],rows):
                        if any(row.get(k)!=v for k,v in expected.items()):raise ValueError('plan mismatch')
                        sig=row.get('target_signature')
                        if (row.get('published') is not True or not isinstance(sig,list) or len(sig)!=5 or
                            any(type(v) is not int or v<0 for v in sig) or sig[2]!=row['size_bytes']):raise ValueError('missing proof')
                        verified.append((tuple(sig),row['blake3']))
                    self.values.update(verified)
                except (OSError,ValueError,KeyError,TypeError):continue
    def digest(self, fd, progress=None):
        before=tuple(signature(fd));value=self.values.get(before)
        if value is None:return self.fallback.digest(fd,progress)
        if progress:progress(0)
        if tuple(signature(fd))!=before:raise ArchiveError('复用归档回执时目标身份发生变化')
        return value
