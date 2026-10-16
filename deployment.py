"""Polling/webhook transition checks and a local single-process lock."""
import asyncio
from contextlib import contextmanager
import logging
import os
from urllib.parse import urlparse

import httpx
from config import ROOT, ConfigError

logger = logging.getLogger(__name__)


@contextmanager
def single_instance(path=ROOT / "runtime/bot.lock"):
    path.parent.mkdir(exist_ok=True)
    stream = path.open("a+b")
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ConfigError("Уже работает другой экземпляр бота. Остановите его перед сменой режима.") from None
    try:
        yield
    finally:
        stream.close()


async def polling_ready(bot):
    await bot.delete_webhook(drop_pending_updates=False)
    info = await bot.get_webhook_info()
    if info.url:
        raise ConfigError("Webhook остался активным. Polling не запущен.")
    logger.info("event=polling_webhook_removed pending=%d",info.pending_update_count)


async def tunnel_url(config,transport=None):
    if config.webhook_host != "127.0.0.1":
        raise ConfigError("Webhook слушает только 127.0.0.1.")
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}",config.webhook_secret) or not re.fullmatch(r"[A-Za-z0-9_-]{20,100}",config.webhook_path):
        raise ConfigError("Webhook требует отдельные криптографические секреты пути и заголовка.")
    if config.webhook_secret == config.bot_token:
        raise ConfigError("Webhook secret не может совпадать с BOT_TOKEN.")
    try:
        async with httpx.AsyncClient(timeout=8,transport=transport) as client:
            data = (await client.get("http://127.0.0.1:4040/api/tunnels")).json()
            tunnels = [t for t in data.get('tunnels',[]) if t.get('proto')=='https']
            for tunnel in tunnels:
                url = tunnel['public_url'].rstrip('/')
                parsed = urlparse(url)
                address = str(tunnel.get('config',{}).get('addr',''))
                if address not in (str(config.webhook_port),f"http://localhost:{config.webhook_port}",f"http://127.0.0.1:{config.webhook_port}",f"localhost:{config.webhook_port}",f"127.0.0.1:{config.webhook_port}"):
                    continue
                if config.ngrok_url and config.ngrok_url != url: continue
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.query: continue
                response = await client.get(url,headers={"ngrok-skip-browser-warning":"true"})
                # 502 before the local PTB server starts is expected; the tunnel itself responded.
                if response.status_code in (200,403,404,502):
                    logger.info("event=ngrok_preflight_ok status=%d",response.status_code)
                    return url
    except (httpx.HTTPError,ValueError,KeyError):
        pass
    raise ConfigError("ngrok HTTPS-туннель к порту webhook не подтверждён. Выполняется возврат к polling.")


async def watch_tunnel(application,config):
    failures = 0
    while True:
        await asyncio.sleep(15)
        try:
            await tunnel_url(config)
            info = await application.bot.get_webhook_info()
            if info.last_error_date and info.last_error_date.timestamp() > application.bot_data.get('started_at',0):
                failures += 1
            else: failures = 0
        except Exception:
            failures += 1
        if failures >= 2:
            logger.error("event=webhook_failed fallback=polling")
            application.bot_data['webhook_failed'] = True
            application.stop_running()
            return
