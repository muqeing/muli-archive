import os
from pathlib import Path
import tempfile
import unittest
from blake3 import blake3
from muli_sorter.archive_io import directory
from muli_sorter.archive_copy import stage_file


class Interrupted(Exception): pass


class CopyTests(unittest.TestCase):
    def test_resume_checks_prefix_and_writes_only_remainder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            data=b'synthetic-independent-copy'*100
            (root/'source').write_bytes(data)
            row={'size_bytes':len(data),'blake3':blake3(data).hexdigest(),'temp':'.copy.partial'}
            def stop(phase,row):
                if phase=='copy_chunk': raise Interrupted()
            with directory(root) as fd:
                with self.assertRaises(Interrupted): stage_file(fd,'source',fd,row,lambda:None,stop,64)
                self.assertEqual((root/'.copy.partial').stat().st_size,64)
                written,resumed=stage_file(fd,'source',fd,row,lambda:None,lambda *args:None,64)
            self.assertEqual((written,resumed),(len(data)-64,64))
            self.assertEqual((root/'.copy.partial').read_bytes(),data)
            self.assertNotEqual((root/'source').stat().st_ino,(root/'.copy.partial').stat().st_ino)
