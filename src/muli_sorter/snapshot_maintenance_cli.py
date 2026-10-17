"""Versioned local JSON maintenance CLI. Default run only previews."""
import argparse,json,sys,sqlite3,subprocess
from dataclasses import asdict
from pathlib import Path
from . import __version__
from .order_feed_io import read_json
from .snapshot_references import References,RetentionBlocked
from .snapshot_cleanup import Policy,Cleanup
from .snapshot_runtime import DockerRuntime


def configuration(path):
    path=Path(path).absolute()
    if path.stat().st_size>65536:raise ValueError('maintenance config exceeds limit')
    v=read_json(path)
    expected={'schema','queue','archive_state','ingest_database','classifier_container','archive_container','protected_containers','archive_image_id','storage','policy'}
    if not isinstance(v,dict) or set(v)!=expected or v['schema']!='snapshot-maintenance/1':raise ValueError('unknown/incomplete maintenance config')
    for k in ('queue','archive_state','ingest_database'):
        if not isinstance(v[k],str) or not Path(v[k]).is_absolute():raise ValueError('maintenance paths must be absolute')
    return v,Policy.parse(v['policy'])


def main(argv=None):
    parser=argparse.ArgumentParser(description='Generated page maintenance; JSON, default dry-run, never media cleanup')
    parser.add_argument('--config');parser.add_argument('--version',action='store_true')
    parser.add_argument('command',nargs='?',choices=['policy','status','init','bootstrap','preview','shadow','run','recover-freezer'])
    parser.add_argument('--execute',action='store_true');parser.add_argument('--confirmed-policy');parser.add_argument('--expected-plan')
    args=parser.parse_args(argv);command='version' if args.version else args.command or 'policy'
    def result(data,ok=True):print(json.dumps({'schema_version':1,'ok':ok,'service':'muli-archive-maintenance','cli_version':__version__,'command':command,'data':data},ensure_ascii=False))
    try:
        if args.version:result({'version':__version__});return 0
        if command=='policy' and not args.config:result({'policy':asdict(Policy()),'sha256':Policy().digest()});return 0
        if not args.config:raise ValueError('--config required')
        config,policy=configuration(args.config)
        if command=='policy':result({'policy':asdict(policy),'sha256':policy.digest()});return 0
        if command in ('init','bootstrap','shadow','recover-freezer') and (not args.execute or args.confirmed_policy!=policy.digest()):
            result({'status':'dry_run','requested':command,'policy_sha256':policy.digest()});return 0
        refs=References(config['archive_state'],config['queue'],create=command=='init')
        runtime=DockerRuntime(config);cleanup=Cleanup(refs,policy,runtime)
        if command=='init':result({'status':'initialized_not_bootstrapped','automatic_deletion_enabled':False});return 0
        if command=='status':result(cleanup.status());return 0
        if command=='bootstrap':
            state=runtime.check(refs,policy)
            if state.get('busy'):result({'status':'busy','reason':state['busy']});return 0
            with refs.gate(exclusive=True,wait=False):
                result({'status':'indexed','index':refs.refresh(maximum_records=100000,maximum_bytes=4*1024**3,seconds=None)})
            return 0
        if command=='shadow':result(cleanup.shadow());return 0
        if command=='recover-freezer':result(cleanup.recover_freezer(confirmed_policy=args.confirmed_policy));return 0
        if command=='preview':result({'status':'dry_run','plan':cleanup.preview()});return 0
        result(cleanup.run(execute=args.execute,confirmed_policy=args.confirmed_policy,expected_plan=args.expected_plan));return 0
    except BlockingIOError:result({'status':'busy','reason':'maintenance_gate_in_use'});return 0
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError) as exc:
        result({'status':'blocked','error':str(exc)[:300]},ok=False);return 2

if __name__=='__main__':raise SystemExit(main())
