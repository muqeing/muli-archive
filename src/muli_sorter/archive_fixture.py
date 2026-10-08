"""Create a new, isolated fixture from bundled generated JPEG/MP4 assets."""
from datetime import datetime, timezone
import json
from pathlib import Path
from blake3 import blake3
from .archive_io import ArchiveError
from .intake import resolve_files, validate_record
from .matching import group_files, project_index
from .review import build_review_model

MARKER = {'kind': 'muli-synthetic-archive-v1', 'production_authorized': False}


def save_manifest(root, manifest):
    batch = root / 'staging' / manifest['batch']['batch_id']
    raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    markdown = b'# GENERATED SYNTHETIC INGEST FIXTURE\n' + raw
    (batch / 'ingest_manifest.json').write_bytes(raw)
    (batch / 'ingest_manifest.md').write_bytes(markdown)
    receipt = {'algorithm': 'blake3', 'batch_uid': manifest['batch']['batch_uid'], 'manifest_id': manifest['manifest_id'],
               'revision': manifest['revision'], 'example_data': True, 'json_blake3': blake3(raw).hexdigest(), 'md_blake3': blake3(markdown).hexdigest()}
    (batch / 'ingest_complete.json').write_text(json.dumps(receipt))


def prepare(root):
    root = Path(root).absolute()
    if root.exists() or root.is_symlink() or root.parent.resolve() != root.parent or str(root).startswith('/Volumes/'):
        raise ArchiveError('仅在不存在的本地独立目录创建合成实验')
    root.mkdir(mode=0o700)
    for name in ('staging', 'projects', 'state'):
        (root / name).mkdir(mode=0o700)
    (root / 'SYNTHETIC_ONLY.json').write_text(json.dumps(MARKER))
    assets = Path(__file__).with_name('demo_assets')
    image = (assets / 'synthetic.jpg').read_bytes()
    video = (assets / 'synthetic.mp4').read_bytes()
    files = [('DCIM/A.JPG', image, '2026-07-09'), ('DCIM/A.XMP', b'<x:xmpmeta xmlns:x="adobe:ns:meta/">SYNTHETIC</x:xmpmeta>', '2026-07-09'),
             ('DCIM/B.MP4', video, '2026-07-10'), ('DCIM/B.LRF', video, '2026-07-10'), ('DCIM/C.JPG', image+b'\nSYNTHETIC PENDING\n', '2026-07-11')]
    bid = 'BATCH_20260928_000001'
    batch = root / 'staging' / bid
    (batch / 'SOURCE_DATA/DCIM').mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for n, (name, data, day) in enumerate(files):
        (batch / 'SOURCE_DATA' / name).write_bytes(data)
        h = blake3(data).hexdigest()
        records.append({'file_id': 'synthetic-'+str(n), 'relative_path': name, 'filename': Path(name).name,
                        'size_bytes': len(data), 'copy_status': 'verified', 'hash_match': True, 'existing_copy': None,
                        'destination_relative_path': 'SOURCE_DATA/'+name, 'hash': {'algorithm':'blake3','source':h,'destination':h,'readback_verified_at':now},
                        'metadata': {'capture_time': {'normalized':day+'T10:00:00+08:00','confidence':'timezone_aware'},'camera':{'make':'DJI' if name.endswith(('.MP4','.LRF')) else 'SONY','model':'Synthetic Demo'}}})
    m = {'schema_version':'1.1','example_data':True,'manifest_id':'synthetic-fixture-r1','revision':1,
         'batch':{'batch_id':bid,'batch_uid':'synthetic-fixture','state':'COMPLETED','result':'COPY_VERIFIED','completed_at':now,'source_id':'synthetic-camera'},
         'summary':{'pending_file_count':0,'failed_file_count':0,'selected_file_count':len(records),'selected_bytes':sum(r['size_bytes'] for r in records),'verified_file_count':len(records),'previously_ingested_count':0},
         'scan_errors':[],'files':records}
    save_manifest(root,m)
    snapshot = {'generated_at':now,'batches':[{**m['batch'],'revision':1}]}
    (root/'runtime-state.json').write_text(json.dumps(snapshot))
    for day, name in [('20260709','合成照片项目'),('20260710','合成视频项目'),('20260711','未确认项目')]:
        (root/'projects/2026/7月'/f'{day}_09999_{name}').mkdir(parents=True)
    index = project_index(root/'projects')
    validated = validate_record(root/'staging',bid,allow_examples=True)
    groups = group_files(resolve_files(root/'staging',validated,{bid:validated}),index,[],m['manifest_id'])
    model = build_review_model({'mode':'read_only_preview','example_data':True,'generated_at':now,'projects':index,
                               'batches':[{'batch_id':bid,'status':'verified','manifest_id':m['manifest_id'],'groups':groups}]})
    decisions = {'schema_version':'0.2','mode':'classification_confirmation_only','media_write_authorized':False,'report_id':model['report_id'],'created_at':now,'segments':[]}
    lookup = {u['unit_id']:u for u in model['units']}
    for segment in model['initial_segments']:
        unit = lookup[segment['unit_ids'][0]]
        confirmed = unit['capture_date'] != '2026-07-11'
        decisions['segments'].append({**segment,'decision':'confirmed' if confirmed else 'pending','project_id':unit['candidate_project_ids'][0] if confirmed else None})
    (root/'model.json').write_text(json.dumps(model,ensure_ascii=False,indent=2))
    (root/'decisions.json').write_text(json.dumps(decisions,ensure_ascii=False,indent=2))
    return model, decisions
