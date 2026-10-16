"""Create stable ignored service secrets without changing the user's .env."""
from pathlib import Path
import secrets
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import dotenv_values


def prepare():
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    values = dict(dotenv_values(runtime / "services.env"))
    # Existing .env has priority. Recovered input is private and never displayed.
    values.update({k:v for k,v in dotenv_values(runtime / "recovered-template.env").items() if v})
    values.update({k:v for k,v in dotenv_values(ROOT / ".env").items() if v})
    for name,prefix in (("LITELLM_MASTER_KEY","sk-"),("LITELLM_SALT_KEY",""),("POSTGRES_PASSWORD",""),
                        ("WEBHOOK_PATH","telegram-"),("WEBHOOK_SECRET_TOKEN","")):
        if not values.get(name): values[name] = prefix+secrets.token_urlsafe(32)
    values.setdefault("POSTGRES_USER","litellm")
    values.setdefault("POSTGRES_DB","litellm")
    def write(path,names):
        # JSON quoting is compatible with dotenv and Compose, including $ and #.
        # Compose env files use format: raw below only if necessary; generated secrets are URL safe.
        import json
        text = "".join(k+"='"+str(values.get(k,"")).replace("'","\\'")+"'\n" for k in names)
        path.write_text(text,encoding="utf-8")
    names = ("LITELLM_MASTER_KEY","LITELLM_SALT_KEY","POSTGRES_USER","POSTGRES_PASSWORD","POSTGRES_DB","WEBHOOK_PATH","WEBHOOK_SECRET_TOKEN")
    write(runtime / "services.env",names)
    write(runtime / "postgres.env",("POSTGRES_USER","POSTGRES_PASSWORD","POSTGRES_DB"))
    values["DATABASE_URL"] = "postgresql://"+quote(values["POSTGRES_USER"],safe="")+":"+quote(values["POSTGRES_PASSWORD"],safe="")+"@postgres:5432/"+quote(values["POSTGRES_DB"],safe="")
    write(runtime / "litellm.env",("LITELLM_MASTER_KEY","LITELLM_SALT_KEY","DATABASE_URL","OPENAI_API_KEY"))
    print("Private service configuration prepared in runtime/; .env unchanged.")
    print("OpenAI credential: "+("present" if values.get("OPENAI_API_KEY") else "missing; paid requests unavailable"))


if __name__ == "__main__": prepare()
