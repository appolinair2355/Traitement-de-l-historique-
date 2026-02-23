import os

# =====================================================
# CONFIGURATION PRÉ-DÉFINIE
# =====================================================

BOT_TOKEN = "8359623168:AAHno00lno02QOw5OvGukP0TIgn4sDFB158"
ADMIN_ID = 1190237801
API_ID = 29177661
API_HASH = "a8639172fa8d35dbfd8ea46286d349ab"

# Canal cible
CHANNEL_ID = -1003329818758
CHANNEL_USERNAME = "VIP DE KOUAMÉ & JOKER"

# VOTRE NUMÉRO PRÉ-CONFIGURÉ
USER_PHONE = "+22995501564"

# Session Telethon exportée (StringSession — portable, aucun fichier requis)
TELETHON_SESSION_STRING = os.getenv('TELETHON_SESSION_STRING',
    "1BJWap1wBu4lzaLg-cXjrhxCjrUgNy-DIKrVvmd9A4RKEzSJG9yX3akzUc_yzlw1O_Ub-deixdJYpjA0KsJNVJyN7aTDqFsvMRADc4i5pQkNLhkoqmrtFXZLnmTe2bIqWwb_I8hnDxu4QTGAPu2cLZvQqXdpirEc9wGHvQY2OU9l3MFrZYE3V9OoCXNZsxG-wq3PmWE8hxentIZ-rZ8bgpHzokLMV3VYmlIvfdlVwztAIXkiK21BVXG2VoggANAxnZaEWepYE8sNhlt0JpGiFtFaCD4xUCI4kxs2OFKlHupc3jU905yGCF2svTIQgjyxaCYY4VmBnjovlWHx1tIcRabQiblQ0T64="
)

# Port
PORT = int(os.getenv('PORT', 5000))

# Dossier de données : /data si défini (disque persistant Render), sinon ./data/ local
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv('DATA_DIR', os.path.join(_BASE_DIR, 'data'))

PREDICTIONS_FILE = os.path.join(DATA_DIR, 'predictions.json')
LAST_SYNC_FILE = os.path.join(DATA_DIR, 'last_sync.json')
SESSION_PATH = os.path.join(DATA_DIR, 'telethon_session')
AUTH_STATE_FILE = os.path.join(DATA_DIR, 'auth_state.json')

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.chmod(DATA_DIR, 0o775)
    # Corriger les permissions des fichiers existants dans le dossier data
    for fname in os.listdir(DATA_DIR):
        fpath = os.path.join(DATA_DIR, fname)
        try:
            os.chmod(fpath, 0o664)
        except Exception:
            pass
