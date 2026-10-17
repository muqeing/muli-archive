"""Build the dependency-free host maintenance zipapp from this exact checkout."""
from pathlib import Path
import argparse,json,hashlib,tempfile,zipapp,shutil

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args()
 root=Path(__file__).resolve().parents[1];out=Path(args.output).absolute();out.parent.mkdir(parents=True,exist_ok=True)
 names=['__init__.py','order_feed_io.py','handoff_request_binding.py','snapshot_references.py','snapshot_cleanup.py','snapshot_runtime.py','snapshot_maintenance_cli.py']
 with tempfile.TemporaryDirectory() as tmp:
  staging=Path(tmp);(staging/'muli_sorter').mkdir()
  for name in names:shutil.copyfile(root/'src/muli_sorter'/name,staging/'muli_sorter'/name)
  (staging/'__main__.py').write_text('from muli_sorter.snapshot_maintenance_cli import main\nraise SystemExit(main())\n')
  zipapp.create_archive(staging,out,interpreter='/usr/bin/env python3',compressed=True)
 hashes={n:hashlib.sha256((root/'src/muli_sorter'/n).read_bytes()).hexdigest() for n in names}
 out.with_suffix(out.suffix+'.json').write_text(json.dumps({'stdlib_only':True,'source_sha256':hashes,'zipapp_sha256':hashlib.sha256(out.read_bytes()).hexdigest()},indent=2)+'\n')
 print(out)
if __name__=='__main__':main()
