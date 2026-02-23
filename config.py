import os

# =====================================================
# CONFIGURATION PRÉ-DÉFINIE
# =====================================================

BOT_TOKEN = "7830176220:AAGPSbyhxLazb1G6IVCzen5oUbGPDwx7wY0"
ID_ADMIN = 1190237801
ID_API = 29177661
API_HASH = "a8639172fa8d35dbfd8ea46286d349ab"

# Canal cible
ID_CANAL = - 1003329818758
CHANNEL_USERNAME = "VIP DE KOUAMÉ & JOKER"

# VOTRE NUMÉRO PRÉ-CONFIGURÉ
TÉLÉPHONE_UTILISATEUR = "+22995501564"

# Port
PORT = int ( os. getenv ( 'PORT' , 5000 ) )

# Dossier de données : /data si défini (disque persistant Render), sinon ./data/ local
_BASE_DIR = os.path.dirname ( os.path.abspath ( __ file__ ) )​​​
DATA_DIR = os.getenv ( ' DATA_DIR' , os.path.join ( _BASE_DIR , ' data' ) )

FICHIER_DE_PRÉDICTIONS = os.path.join(DATA_DIR, 'predictions.json')path.join(DATA_DIR, 'predictions.json')
DERNIER_FICHIER_SYNCHRONISATION = os.path.join(DATA_DIR, 'last_sync.json')path.join(DATA_DIR, 'last_sync.json')
SESSION_PATH = os.path.join(DATA_DIR, 'telethon_session')path.join(DATA_DIR, 'telethon_session')
FICHIER_D'ÉTAT_AUTH = os.path.join(DATA_DIR, 'auth_state.json')path.join(DATA_DIR, 'auth_state.json')

def ensure_data_dir(): ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)makedirs(DATA_DIR, exist_ok=True)
    os.chmod(DATA_DIR, 0o775)chmod(DATA_DIR, 0o775)
