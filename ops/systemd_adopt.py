"""Adopt a captured Python worker into a system unit without changing its settings.

Run as root. Runtime capture and environment files contain secrets and stay 0600.
The old process is stopped gracefully; a second worker is never started alongside it.
"""
import argparse, json, os, pathlib, re, shutil, signal, subprocess, time

BASE=pathlib.Path('/var/lib/ktrader-delta-rollout')
def call(args, **kwargs):
    return subprocess.run(args,check=True,capture_output=True,text=True,**kwargs)
def quote(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('\n','\\n')+'"'
def same_process(record):
    try:
        raw=pathlib.Path('/proc')/str(record['pid'])/'cmdline'
        return raw.read_bytes().decode().strip('\0').split('\0')==record['argv']
    except FileNotFoundError:return False
def adopt(record, service):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.service',service):raise ValueError('Invalid unit name')
    backup=BASE/'systemd';backup.mkdir(mode=0o700,exist_ok=True)
    existing=record['scope']=='system' and record['service']==service
    if existing:
        fragment=call(['systemctl','show','--property=FragmentPath','--value',service]).stdout.strip()
        if fragment:shutil.copy2(fragment,backup/service)
        drop=pathlib.Path('/etc/systemd/system')/(service+'.d')
        drop.mkdir(exist_ok=True)
        (drop/'90-delta-worker.conf').write_text('[Service]\nRestart=on-failure\nRestartSec=5\nTimeoutStopSec=360\nSendSIGKILL=no\n')
    else:
        target=pathlib.Path('/etc/systemd/system')/service
        if target.exists():shutil.copy2(target,backup/service)
        envdir=pathlib.Path('/etc/ktrader-services');envdir.mkdir(mode=0o700,exist_ok=True)
        envpath=envdir/(service+'.env')
        env=record['env'].copy()
        excluded=('SSH_','SUDO_','INVOCATION_','JOURNAL_','NOTIFY_','LISTEN_')
        for key in list(env):
            if key.startswith(excluded) or key in {'DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR','SHLVL','_','TERM','OLDPWD'}:env.pop(key)
        env['PYTHONUNBUFFERED']='1'
        def envquote(v):return '"'+v.replace('\\','\\\\').replace('"','\\"').replace('`','\\`').replace('$','\\$')+'"'
        envpath.write_text(''.join(k+'='+envquote(v)+'\n' for k,v in env.items() if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',k)))
        envpath.chmod(0o600)
        args=list(record['argv'])
        if not args[0].startswith('/'):
            args[0]=shutil.which(args[0],path=env.get('PATH'))
        if not args[0] or not pathlib.Path(args[0]).exists():raise ValueError('Missing interpreter')
        content='\n'.join(['[Unit]','Description=KTrader managed '+service.removesuffix('.service'),
            'After=network-online.target','Wants=network-online.target','','[Service]','Type=simple',
            'User='+record['user'],'WorkingDirectory='+record['cwd'].replace('%','%%'),
            'EnvironmentFile='+str(envpath),'ExecStart=:'+ ' '.join(quote(a) for a in args),
            'UMask=0077','Restart=on-failure','RestartSec=5','TimeoutStopSec=360','SendSIGKILL=no',
            *(['RestartPreventExitStatus='+str(record['auth_exit_code'])] if record.get('auth_exit_code') else []),
            'KillSignal=SIGTERM',*(['AmbientCapabilities=CAP_NET_BIND_SERVICE'] if record.get('bind_low_ports') else []),
            '','[Install]','WantedBy=multi-user.target',''])
        target.write_text(content)
        call(['systemd-analyze','verify',str(target)])
    call(['systemctl','daemon-reload'])
    if record['service'] and record['scope']=='user':
        call(['runuser','-u',record['user'],'--','env',f'XDG_RUNTIME_DIR=/run/user/{record["uid"]}',
              'systemctl','--user','disable','--now',record['service']],timeout=365)
    elif existing:
        call(['systemctl','stop',service],timeout=365)
    elif same_process(record):
        os.kill(record['pid'],signal.SIGTERM)
    deadline=time.monotonic()+365
    while same_process(record):
        if time.monotonic()>deadline:raise TimeoutError('Original worker did not stop; replacement not started')
        time.sleep(.5)
    call(['systemctl','enable',service])
    call(['systemctl','start',service])
    print(json.dumps({'service':service,'active':call(['systemctl','is-active',service]).stdout.strip(),
                      'enabled':call(['systemctl','is-enabled',service]).stdout.strip()}),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--service')
    args=parser.parse_args()
    records=[r for r in json.loads((BASE/'runtime.json').read_text()) if r['cwd']==args.root]
    if not records:raise ValueError('No captured process for repository')
    for r in records:adopt(r,args.service or r['service'])
