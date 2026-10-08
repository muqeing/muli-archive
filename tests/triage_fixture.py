"""Local synthetic media only, shared by integration and browser acceptance."""
from copy import deepcopy
import json
from pathlib import Path
from blake3 import blake3

from muli_sorter.archive_fixture import prepare, save_manifest
from muli_sorter.intake import resolve_files, validate_record
from muli_sorter.matching import group_files, project_index
from muli_sorter.review import build_review_model

DJI = 'DJI_20260710100000_0001_D'


def prepare_triage(root, *, multi=False):
    prepare(root)
    batch = root / 'staging/BATCH_20260928_000001'
    manifest = json.loads((batch / 'ingest_manifest.json').read_text())
    assets = Path(__file__).parents[1] / 'src/muli_sorter/demo_assets'
    additions = [
        ('DCIM/DJI_001/' + DJI + '.MP4', (assets / 'synthetic.mp4').read_bytes(), 'DJI'),
        ('MISC/THM/DJI_001/' + DJI + '.THM', b'THUMBNAIL SYNTHETIC', None),
        ('MISC/THM/DJI_001/' + DJI + '.SCR', b'SCREEN SYNTHETIC', None),
        ('M4ROOT/CLIP/C0001.MP4', (assets / 'synthetic.mp4').read_bytes(), 'SONY'),
        ('M4ROOT/CLIP/C0001M01.XML', b'<NonRealTimeMeta><CreationDate value="2026-07-10T10:00:00+08:00"/></NonRealTimeMeta>', None),
        ('AVF_INFO/AVIN0001.BNP', b'DEVICE DATABASE', None),
        ('System Volume Information/WPSettings.dat', b'VOLUME SETTINGS', None),
        ('DCIM/._not-a-photo.ORF', bytes.fromhex('0005160700020000') + b'Mac OS X', None),
        ('DCIM/DJI_001/DJI_20260711100000_0002_D.LRF', b'ORPHAN PROXY', 'DJI'),
        ('DCIM/missing.JPG', (assets / 'synthetic.jpg').read_bytes(), 'SONY'),
    ]
    if multi:
        other = 'DJI_20260711100000_0003_D'
        additions += [('DCIM/DJI_001/' + other + '.MP4', (assets/'synthetic.mp4').read_bytes(), 'DJI'),
                      ('MISC/THM/DJI_001/' + other + '.THM', b'OTHER THUMB', None),
                      ('MISC/THM/DJI_001/' + other + '.SCR', b'OTHER SCREEN', None),
                      ('DCIM/present.JPG', (assets/'synthetic.jpg').read_bytes(), 'SONY'),
                      ('DCIM/unknown-date.JPG', (assets/'synthetic.jpg').read_bytes(), None)]
    for n, (name, data, camera) in enumerate(additions):
        f = deepcopy(manifest['files'][0])
        f.update(file_id='triage-' + str(n), relative_path=name, filename=Path(name).name,
                 destination_relative_path='SOURCE_DATA/' + name, size_bytes=len(data))
        f['hash']['source'] = f['hash']['destination'] = blake3(data).hexdigest()
        f['metadata'] = {} if camera is None else {'capture_time': {'normalized': '2026-07-10T10:00:00+08:00', 'confidence': 'timezone_aware'}, 'camera': {'make': camera, 'model': 'Synthetic Demo'}}
        if multi and camera and 'DJI_20260711100000_0003_D' in name:
            f['metadata']['capture_time']['normalized'] = '2026-07-11T10:00:00+08:00'
        manifest['files'].append(f)
        path = batch / 'SOURCE_DATA' / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    manifest['summary'].update(selected_file_count=len(manifest['files']), verified_file_count=len(manifest['files']), selected_bytes=sum(f['size_bytes'] for f in manifest['files']))
    save_manifest(root, manifest)
    valid = validate_record(root / 'staging', manifest['batch']['batch_id'], allow_examples=True)
    groups = group_files(resolve_files(root / 'staging', valid, {valid['batch']['batch_id']: valid}), project_index(root / 'projects'), [], valid['manifest_id'])
    model = build_review_model({'mode': 'read_only_preview', 'example_data': True, 'generated_at': manifest['batch']['completed_at'],
                                'projects': project_index(root / 'projects'), 'batches': [{'batch_id': valid['batch']['batch_id'], 'manifest_id': valid['manifest_id'], 'status': 'verified', 'groups': groups}]})
    decisions = {'schema_version': '0.3', 'mode': 'classification_confirmation_only', 'media_write_authorized': False,
                 'report_id': model['report_id'], 'created_at': model['snapshot_at'], 'manual_projects': [],
                 'segments': [{**s, 'decision': 'deferred'} for s in model['initial_segments']]}
    return model, decisions
