from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from muli_sorter.queue_files import publish
from muli_sorter.queue_view import _publish_model
from muli_sorter.review_projection import page_signature
from muli_sorter.order_feed_io import atomic_json


class PublicationStabilityTests(unittest.TestCase):
    def test_unchanged_generated_report_over_intake_limit_keeps_identity(self):
        # Production's combined report is larger than the 64 MiB intake limit.
        # Republishing identical output must not invalidate an open page.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            content = b'x' * (64 * 1024 * 1024 + 1)
            publish(root, '', {'确认模型.json': content})
            before = (root/'确认模型.json').stat()
            publish(root, '', {'确认模型.json': content})
            after = (root/'确认模型.json').stat()
            self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_preview_change_does_not_serialize_or_rewrite_model(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            q = SimpleNamespace(root=root, bundle_cache={})
            model = {'report_id': 'sha256:'+'a'*64}
            path = 'combined/'+model['report_id']
            atomic_json(root/'队列状态.json', {'base_model_path': path+'/确认模型.json'})
            with patch('muli_sorter.queue_view.preview_source_key', return_value='frame1'), patch('muli_sorter.queue_view.render_with_previews', return_value=b'frame1'):
                _publish_model(q, path, model)
            before = page_signature(root)
            with patch('muli_sorter.queue_view.preview_source_key', return_value='frame2'), patch('muli_sorter.queue_view.render_with_previews', return_value=b'frame2'), patch('muli_sorter.queue_view.canonical', side_effect=AssertionError('model serialized for preview')):
                _publish_model(q, path, model)
            self.assertEqual((root/path/'拍摄段确认.html').read_bytes(), b'frame2')
            self.assertEqual(page_signature(root), before)

    def test_separate_caches_still_repair_changed_model_and_missing_html(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            q = SimpleNamespace(root=root, bundle_cache={})
            model = {'report_id': 'sha256:'+'a'*64}
            path = 'combined/'+model['report_id']
            with patch('muli_sorter.queue_view.preview_source_key', return_value='frame'), patch('muli_sorter.queue_view.render_with_previews', return_value=b'frame'):
                _publish_model(q, path, model)
                expected = (root/path/'确认模型.json').read_bytes()
                (root/path/'确认模型.json').write_bytes(b'tampered')
                (root/path/'拍摄段确认.html').unlink()
                _publish_model(q, path, model)
            self.assertEqual((root/path/'确认模型.json').read_bytes(), expected)
            self.assertEqual((root/path/'拍摄段确认.html').read_bytes(), b'frame')
