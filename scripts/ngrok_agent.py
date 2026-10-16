"""Local ngrok lifecycle; credentials only in the child environment, never argv."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import zipfile

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def start():
    runtime = ROOT/'runtime'
    tools = runtime/'tools'
    tools.mkdir(exist_ok=True)
    exe = shutil.which('ngrok') or str(tools/'ngrok.exe')
    if not Path(exe).exists():
        url = 'https://bin.ngrok.com/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip'
        response = httpx.get(url,follow_redirects=True,timeout=60)
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            # Extract only this exact filename into the intended directory.
            (tools/'ngrok.exe').write_bytes(archive.read('ngrok.exe'))
    values = {**dotenv_values(ROOT/'.env')}
    env = os.environ.copy()
    if values.get('NGROK_AUTHTOKEN'): env['NGROK_AUTHTOKEN'] = values['NGROK_AUTHTOKEN']
    log_path = runtime/'ngrok.log'
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    log = log_path.open('ab')
    flags = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    port = str(values.get('WEBHOOK_PORT') or 8080)
    args = [exe,'http',port,'--inspect=false','--log','stdout','--log-format','json']
    if values.get('NGROK_PUBLIC_URL'): args += ['--url',values['NGROK_PUBLIC_URL']]
    process = subprocess.Popen(args,cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=flags)
    log.close()
    (runtime/'ngrok.pid').write_text(str(process.pid))
    deadline = time.monotonic()+25
    with httpx.Client(timeout=1) as client:
        while time.monotonic()<deadline:
            if process.poll() is not None: break
            try:
                response = client.get('http://127.0.0.1:4040/api/tunnels')
                tunnels = response.json().get('tunnels',[])
                if any(t.get('proto')=='https' for t in tunnels):
                    print('ngrok HTTPS tunnel ready; bot discovers URL from local agent API.')
                    return 0
            except Exception: pass
            time.sleep(0.5)
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    text = log_path.read_bytes()[log_offset:].decode('utf-8',errors='replace')
    token = env.get('NGROK_AUTHTOKEN','')
    if token:
        # Bad-token errors can echo credentials; keep those out of the saved log.
        complete = log_path.read_text(encoding='utf-8',errors='replace')
        log_path.write_text(complete.replace(token,'[REDACTED]'),encoding='utf-8')
    codes = sorted(set(re.findall(r'ERR_NGROK_\d+',text)))
    print('ngrok unavailable; error codes: '+(', '.join(codes) or 'no HTTPS tunnel'))
    print('NGROK_AUTHTOKEN: '+('present' if env.get('NGROK_AUTHTOKEN') else 'missing'))
    if 'failed to fetch CRL' in text:
        print('Cause: ngrok timed out fetching its certificate revocation list; TLS checks were kept enabled.')
    elif 'ERR_NGROK_4018' in text:
        print('Cause: verified ngrok account authtoken is required.')
    print('Polling remains available.')
    return 1


def stop():
    path = ROOT/'runtime/ngrok.pid'
    if not path.exists(): return
    pid = int(path.read_text())
    if os.name=='nt':
        # Verify the process identity before stopping the process we started.
        result = subprocess.run(['powershell','-NoProfile','-Command',
            f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if ($p -and $p.ProcessName -eq 'ngrok') {{ Stop-Process -Id {pid} }}"],capture_output=True)
    else:
        import signal
        try: os.kill(pid,signal.SIGTERM)
        except ProcessLookupError: pass
    path.unlink(missing_ok=True)
    print('Owned ngrok agent stopped.')


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=('start','stop'))
    args = parser.parse_args()
    try: raise SystemExit(start() if args.action=='start' else stop())
    except (httpx.HTTPError,OSError,zipfile.BadZipFile) as exc:
        print('ngrok setup failed: '+type(exc).__name__+'. Polling remains available.')
        raise SystemExit(1)
