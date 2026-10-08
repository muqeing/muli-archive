from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from muli_sorter.material_triage import build_material_state, companion_links, check_companion_metadata, MISSING


def unit(uid, name, kind='auxiliary', manifest='card-a', **extra):
    return {'unit_id': uid, 'kind': kind, 'warnings': [], 'provenance': [{'manifest_id': manifest}],
            'files': [{'name': name, 'source_path': 'batch/SOURCE_DATA/' + name, 'size_bytes': 8}], **extra}


def model(*units):
    return {'report_id': 'same-report', 'units': list(units)}


class MaterialTriageTests(unittest.TestCase):
    def test_dji_cross_directory_and_sony_names_without_changing_identity(self):
        m = model(unit('v', 'DCIM/DJI_001/DJI_20260920141811_0045_D.MP4', 'video'),
                  unit('t', 'MISC/THM/DJI_001/DJI_20260920141811_0045_D.THM'),
                  unit('s', 'M4ROOT/CLIP/C4863.MP4', 'video'),
                  unit('x', 'M4ROOT/CLIP/C4863M01.XML'))
        before = deepcopy(m)
        state = build_material_state(m)
        self.assertEqual(state['units']['t']['parent_unit_id'], 'v')
        self.assertEqual(state['units']['x']['parent_unit_id'], 's')
        self.assertEqual(m, before)

    def test_never_pairs_globally_by_basename_and_rejects_ambiguity(self):
        v = unit('v', 'DCIM/DJI_001/DJI_0001.MP4', 'video')
        t = unit('t', 'MISC/THM/DJI_001/DJI_0001.THM', manifest='card-b')
        self.assertEqual(companion_links(model(v, t))[0], {})
        v['provenance'].append({'manifest_id': 'card-b'})
        self.assertEqual(companion_links(model(v, t))[0], {'t': 'v'})
        other = deepcopy(v); other['unit_id'] = 'different-physical-file'
        self.assertEqual(companion_links(model(v, t, other)), ({}, {'t'}))

    def test_known_support_not_entire_directory_or_unknown_extensions(self):
        m = model(unit('a', 'AVF_INFO/AVIN0001.BNP'), unit('b', 'PRIVATE/DATABASE/DATABASE.BIN'),
                  unit('c', 'SONY/SETTING/7RM5/CAMSET/PORTRA40.DAT'),
                  unit('d', 'AVF_INFO/important.MP4', 'video'), unit('e', 'OTHER.BNP'),
                  unit('f', 'actual.ARW', 'photo'))
        states = build_material_state(m)['units']
        self.assertEqual([states[k]['category'] for k in 'abcdef'], ['support', 'support', 'support', 'exception', 'exception', 'shoot'])

    def test_appledouble_header_and_live_missing_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            photo = unit('p', 'actual.ARW', 'photo', warnings=[MISSING])
            metadata = unit('m', '._actual.ORF', 'photo')
            unknown = unit('u', '._unknown.ORF', 'photo')
            for item, data in [(photo, b'RAW BYTS'), (metadata, bytes.fromhex('0005160700020000')), (unknown, b'NOT META')]:
                p = root / item['files'][0]['source_path']; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
            missing = unit('gone', 'missing.ARW', 'photo')
            states = build_material_state(model(photo, metadata, unknown, missing), root)['units']
            self.assertEqual(states['p']['category'], 'shoot')
            self.assertEqual(states['m']['category'], 'support')
            self.assertEqual(states['u']['category'], 'exception')
            self.assertEqual(states['gone']['reason_code'], 'source_missing')
            self.assertEqual(build_material_state(model(metadata))['units']['m']['category'], 'exception')

    def test_missing_companion_is_not_silently_attached(self):
        m = model(unit('v', 'M4ROOT/CLIP/C4863.MP4', 'video'),
                  unit('x', 'M4ROOT/CLIP/C4863M01.XML', warnings=[MISSING]))
        self.assertEqual(build_material_state(m)['units']['x']['reason_code'], 'source_missing')

    def test_sony_xml_content_and_trusted_time_conflict_is_not_auto_attached(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            v = unit('v', 'M4ROOT/CLIP/C4863.MP4', 'video', timezone_trusted=True, capture_time='2026-09-27T10:00:00+08:00')
            x = unit('x', 'M4ROOT/CLIP/C4863M01.XML')
            p = root / x['files'][0]['source_path']; p.parent.mkdir(parents=True)
            for raw in [b'<NotCameraMetadata/>', b'<NonRealTimeMeta><CreationDate value="2026-09-28T10:00:00+08:00"/></NonRealTimeMeta>']:
                p.write_bytes(raw); x['files'][0]['size_bytes'] = len(raw)
                with self.assertRaises(ValueError): check_companion_metadata(root, x, v)
            raw = b'<NonRealTimeMeta><CreationDate value="2026-09-27T02:00:00+00:00"/></NonRealTimeMeta>'
            p.write_bytes(raw); x['files'][0]['size_bytes'] = len(raw)
            check_companion_metadata(root, x, v)


if __name__ == '__main__':
    unittest.main()
