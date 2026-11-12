"""Capture launch settings privately on each host, output only inventory metadata."""
import json, os, pathlib, pwd, re, subprocess
BASE = pathlib.Path('/var/lib/ktrader-delta-rollout')
BASE.mkdir(mode=0o700, exist_ok=True)
markers = ['bitget/start.py', 'gigamoney.cli', 'fangde.cli', 'zhuorui_api.py',
           '-m starwealth.ktrader', 'trading_server/tiger.py', 'trading_ui.webserver:app',
           'trading_ui.http_redirect', 'order_ui.webserver:app', '-m bosco run']
records=[]
for p in pathlib.Path('/proc').iterdir():
    if not p.name.isdigit(): continue
    try:
        argv=p.joinpath('cmdline').read_bytes().decode().strip('\0').split('\0')
        if not argv or 'python' not in pathlib.Path(argv[0]).name: continue
        command=' '.join(argv)
        if not any(m in command for m in markers): continue
        uid=p.stat().st_uid
        env=dict(x.split('=',1) for x in p.joinpath('environ').read_bytes().decode().split('\0') if '=' in x)
        cwd=str(p.joinpath('cwd').resolve())
        group=p.joinpath('cgroup').read_text().strip()
        services=re.findall(r'/([^/]+\.service)',group)
        service=services[-1] if services else None
        scope='user' if '/user.slice/' in group else 'system' if service else 'unmanaged'
        if service and service.startswith(('user@','app-')): service=None; scope='unmanaged'
        record=dict(pid=int(p.name),argv=argv,env=env,cwd=cwd,uid=uid,user=pwd.getpwuid(uid).pw_name,service=service,scope=scope)
        records.append(record)
    except (FileNotFoundError, ProcessLookupError, PermissionError): pass
target=BASE/'runtime.json'
saved=json.loads(target.read_text()) if target.exists() else []
saved += [r for r in records if r['pid'] not in {s['pid'] for s in saved}]
target.write_text(json.dumps(saved)); target.chmod(0o600)
print(json.dumps([dict(pid=r['pid'],cwd=r['cwd'],user=r['user'],service=r['service'],scope=r['scope'],
                      entry=[a for a in r['argv'] if any(m.split(' ')[-1] in a for m in markers)]) for r in records]))
