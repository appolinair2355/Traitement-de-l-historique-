import asyncio
import logging
import os
import shutil
from aiohttp import web
from datetime import datetime
from config import PORT, DATA_DIR, SESSION_PATH, ensure_data_dir

# Créer le dossier data avant tout (critique sur Render)
ensure_data_dir()

def _bootstrap_session():
    """
    Copie les fichiers bundlés (data/) vers DATA_DIR si absents.
    Utile sur Render : le disque /data est vide au premier démarrage,
    mais les fichiers de session sont inclus dans le zip.
    """
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
    if os.path.abspath(src_dir) == os.path.abspath(DATA_DIR):
        return  # même dossier, rien à faire (Replit)

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
    await application.updater.start_polling(allowed_updates=['message'])
    
    logger.info("Bot VIP KOUAMÉ démarré!")
    
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
    
