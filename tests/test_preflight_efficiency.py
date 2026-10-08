import os
import time
import unittest
from unittest.mock import patch
from test_archive_jobs import Fixture

class PreparationTests(Fixture, unittest.TestCase):
    def test_ticket_lasts_twenty_minutes_and_changed_source_stops_at_execution(self):
        start=time.time();result=self.service.preflight(self.decisions)
        self.assertEqual(result['status'],'ready')
        self.assertGreaterEqual(result['expires_at']-start,1200)
        self.assertLess(result['expires_at']-start,1205)
        target=self.root/'staging'/self.model['units'][0]['files'][0]['source_path']
        target.write_bytes(b'changed')
        job=self.service.submit(result['preview_id'],self.decisions,True)
        self.assertEqual(job['status'],'queued')
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'partial',done)
        self.assertEqual(done['summary']['removed_sources'],0)
        affected=self.model['units'][0]['unit_id']
        self.assertNotEqual(next(x for x in done['outcomes'] if x['unit_id']==affected)['status'],'completed')
        self.assertEqual(target.read_bytes(),b'changed')
    def test_directory_listing_is_bounded_per_prepare(self):
        # Populate actual fixture target directories, then compare listdir counts
        # with the number of target directories, not the number of source files.
        self.manual();first=self.submit();self.service.run_job(first['job_id'])
        targets={ (p.parent.stat().st_dev,p.parent.stat().st_ino) for p in (self.root/'projects').rglob('*') if p.is_file() }
        calls=[];original=os.listdir
        def listing(path):
            result=original(path)
            if type(path) is int:
                st=os.fstat(path)
                if (st.st_dev,st.st_ino) in targets:calls.append((st.st_dev,st.st_ino))
            return result
        with patch('muli_sorter.archive_jobs.os.listdir',side_effect=listing):
            result=self.service.preflight(self.decisions)
        self.assertEqual(result['status'],'ready')
        from collections import Counter
        # Includes plan_rows' first pass and _prepare's validation pass.
        self.assertLessEqual(max(Counter(calls).values(),default=0),2)

if __name__=='__main__':unittest.main()
