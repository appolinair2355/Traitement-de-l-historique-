import asyncio
import logging
import os
import shutil
import signal
import time
from aiohttp import web
from datetime import datetime
from config import PORT, DATA_DIR, SESSION_PATH, ensure_data_dir

ensure_data_dir()

_LOCK_FILE = '/tmp/vip_kouame_bot.pid'

def _kill_previous_instance():
    """Tue l'ancienne instance si un fichier PID existe."""
    if not os.path.exists(_LOCK_FILE):
        return
    try:
        old_pid = int(open(_LOCK_FILE).read().strip())
        if old_pid == os.getpid():
            return
        logging.getLogger(__name__).info(f"Arrêt ancienne instance PID={old_pid}...")
        os.kill(old_pid, signal.SIGTERM)
        # Attendre que le processus se termine (max 8 secondes)
        for _ in range(8):
            time.sleep(1)
            try:
                os.kill(old_pid, 0)  # Vérifier si le processus existe encore
            except ProcessLookupError:
                break  # Processus mort
        logging.getLogger(__name__).info("Ancienne instance terminée.")
    except (ValueError, PermissionError):
        pass
    finally:
        try:
            os.remove(_LOCK_FILE)
        except OSError:
            pass

def _write_pid():
    with open(_LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))

def _remove_pid():
    try:
        if os.path.exists(_LOCK_FILE):
            pid = int(open(_LOCK_FILE).read().strip())
            if pid == os.getpid():
                os.remove(_LOCK_FILE)
    except OSError:
        pass


def _bootstrap_session():
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
    if os.path.abspath(src_dir) == os.path.abspath(DATA_DIR):
        return
    files_to_copy = [
        'telethon_session.session',
        'auth_state.json',
        'last_sync.json',
        'predictions.json',
    ]
    for fname in files_to_copy:
        src = os.path.join(src_dir, fname)
        dst = os.path.join(DATA_DIR, fname)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
            os.chmod(dst, 0o664)
            logging.getLogger(__name__).info(f"Bootstrap: copié {fname} → {DATA_DIR}")

# Tuer l'ancienne instance AVANT d'importer bot_handler
_kill_previous_instance()
_write_pid()
_bootstrap_session()

from bot_handler import setup_bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def health(request):
    from storage import get_stats
    s = get_stats()
    return web.json_response({
        'status': 'ok',
        'bot': 'VIP_KOUAME_PREDICTIONS',
        'predictions': s['total'],
        'time': str(datetime.now())
    })

async def web_server():
    app = web.Application()
    app.router.add_get('/', health)
    app.router.add_get('/health', health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

async def main():
    await web_server()

    application = setup_bot()
    await application.initialize()
    await application.start()

    await application.updater.start_polling(
        allowed_updates=['message'],
        drop_pending_updates=True,
    )

    logger.info("Bot VIP KOUAMÉ démarré!")

    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("Signal reçu — arrêt immédiat du polling...")
        # Stopper le polling IMMÉDIATEMENT (sans attendre stop_event)
        asyncio.ensure_future(application.updater.stop())
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

    await stop_event.wait()

    logger.info("Arrêt du bot...")
    _remove_pid()
    # updater.stop() est déjà en cours via ensure_future
    try:
        await application.updater.stop()
    except Exception:
        pass
    await application.stop()
    await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
