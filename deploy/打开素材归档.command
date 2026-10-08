#!/bin/zsh
/usr/bin/python3 <<'APP'
import json,subprocess
from urllib.request import build_opener,ProxyHandler
url='http://192.168.31.244:18767/'
try:
    with build_opener(ProxyHandler({})).open(url+'health',timeout=5) as response:
        state=json.load(response)
    assert state.get('service')=='muli-sorter-console' and state.get('worker_running') and state.get('submission_enabled') and not state.get('example_data')
    subprocess.run(['/usr/bin/open',url],check=True)
except Exception:
    print('暂时无法连接 NAS 归档工作台，请确认设备连接工作室局域网、NAS 已开机。')
    input('按回车关闭。')
    raise SystemExit(1)
APP
