"""Sanitized Docker diagnostics, including failures before logging is configured."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import dotenv_values


def redact(value):
    secrets = []
    for path in [ROOT/'.env',*list((ROOT/'runtime').glob('*.env'))]:
        secrets.extend(v for k,v in dotenv_values(path).items() if v and any(w in k.upper() for w in ('TOKEN','KEY','PASSWORD','SECRET','DATABASE_URL','WEBHOOK_PATH')))
    for secret in sorted(secrets,key=len,reverse=True): value = value.replace(secret,'[REDACTED]')
    value = re.sub(r'\bsk-[\w-]+','[REDACTED]',value)
    value = re.sub(r'\b\d{5,}:[A-Za-z0-9_-]{20,}','[REDACTED]',value)
    return value


if __name__ == '__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    commands = [['docker','compose','ps'],['docker','compose','logs','--no-color','--tail','45','litellm','postgres']]
    for command in commands:
        result = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace')
        print(redact(result.stdout+result.stderr))
