import json
import os
from datetime import datetime
from config import PREDICTIONS_FILE, LAST_SYNC_FILE, CHANNELS_FILE, GAMES_FILE, ADMINS_FILE, ADMIN_ID, ensure_data_dir

ensure_data_dir()

# Toutes les commandes qui peuvent être accordées à un sous-admin
ALL_COMMANDS = [
    'sync', 'fullsync', 'search', 'hsearch', 'report', 'filter', 'stats', 'clear',
    'channels', 'usechannel', 'helpcl',
    'gload', 'gstats', 'gclear', 'ganalyze',
    'gvictoire', 'gparite', 'gstructure', 'gplusmoins', 'gcostume', 'gecartmax',
    'predictsetup', 'gpredictload', 'gpredict',
    'documentation',
]

PREDICT_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data', 'predict_config.json'
)

def load_json(filepath, default=None):
    if not os.path.exists(filepath):
        return default if default is not None else {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def save_json(filepath, data):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

def add_prediction(message_id, numero, couleur, statut, raw_text):
    predictions = load_json(PREDICTIONS_FILE, [])
    
    if any(p['message_id'] == message_id for p in predictions):
        return False
    
    predictions.append({
        'message_id': message_id,
        'numero': numero,
        'couleur': couleur,
        'statut': statut,
        'raw_text': raw_text,
        'date': datetime.now().isoformat()
    })
    
    save_json(PREDICTIONS_FILE, predictions)
    return True

def get_predictions(filters=None):
    predictions = load_json(PREDICTIONS_FILE, [])
    
    if not filters:
        return predictions
    
    result = []
    for p in predictions:
        match = True
        if filters.get('couleur'):
            if filters['couleur'].lower() not in p['couleur'].lower():
                match = False
        if filters.get('statut'):
            if filters['statut'].lower() not in p['statut'].lower():
                match = False
        if match:
            result.append(p)
    return result

def get_stats():
    predictions = load_json(PREDICTIONS_FILE, [])
    return {'total': len(predictions)}

def get_last_sync():
    return load_json(LAST_SYNC_FILE, {'last_message_id': 0})

def update_last_sync(message_id):
    save_json(LAST_SYNC_FILE, {
        'last_message_id': message_id,
        'sync_date': datetime.now().isoformat()
    })

def search_predictions(keywords):
    """Cherche des mots-clés dans les prédictions stockées (raw_text, couleur, statut)"""
    predictions = load_json(PREDICTIONS_FILE, [])
    keywords_lower = [k.strip().lower() for k in keywords if k.strip()]
    if not keywords_lower:
        return []
    
    results = []
    for p in predictions:
        text = (p.get('raw_text', '') + ' ' + p.get('couleur', '') + ' ' + p.get('statut', '')).lower()
        if all(kw in text for kw in keywords_lower):
            results.append(p)
    return results

def clear_all():
    save_json(PREDICTIONS_FILE, [])
    save_json(LAST_SYNC_FILE, {'last_message_id': 0})

# ── Gestion des canaux de recherche ──────────────────────────────────────────

def get_channels():
    return load_json(CHANNELS_FILE, [])

def add_channel(channel_id: str, name: str = '') -> bool:
    channels = get_channels()
    if any(ch['id'] == str(channel_id) for ch in channels):
        return False
    channels.append({
        'id': str(channel_id),
        'name': name,
        'active': len(channels) == 0,
        'added_date': datetime.now().strftime('%Y-%m-%d %H:%M'),
    })
    save_json(CHANNELS_FILE, channels)
    return True

def remove_channel(channel_id: str):
    channels = [ch for ch in get_channels() if ch['id'] != str(channel_id)]
    save_json(CHANNELS_FILE, channels)

def get_active_channel():
    channels = get_channels()
    for ch in channels:
        if ch.get('active'):
            return ch
    return channels[0] if channels else None

def set_active_channel(channel_id: str):
    channels = get_channels()
    for ch in channels:
        ch['active'] = (ch['id'] == str(channel_id))
    save_json(CHANNELS_FILE, channels)

# ── Jeux analysés ─────────────────────────────────────────────────────────────

def get_analyzed_games():
    return load_json(GAMES_FILE, [])

def save_analyzed_games(games: list):
    save_json(GAMES_FILE, games)

def clear_analyzed_games():
    save_json(GAMES_FILE, [])

# ── Administrateurs dynamiques avec permissions ────────────────────────────────

def _load_admins_raw() -> dict:
    """Charge le fichier admins et migre l'ancien format liste → dict si nécessaire."""
    raw = load_json(ADMINS_FILE, {})
    if isinstance(raw, list):
        # Migration : ancien format [id1, id2] → nouveau {id1: ALL_COMMANDS}
        migrated = {}
        for uid in raw:
            migrated[str(uid)] = list(ALL_COMMANDS)
        save_json(ADMINS_FILE, migrated)
        return migrated
    return raw

def _save_admins_raw(data: dict):
    save_json(ADMINS_FILE, data)

def get_admins() -> list:
    """Retourne la liste des IDs admin (toujours inclus : ADMIN_ID principal)."""
    raw = _load_admins_raw()
    ids = [int(k) for k in raw.keys()]
    if ADMIN_ID not in ids:
        ids.insert(0, ADMIN_ID)
    return ids

def get_admins_with_permissions() -> dict:
    """Retourne le dict complet {user_id: [commands]}."""
    raw = _load_admins_raw()
    result = {}
    # Main admin toujours présent avec toutes les permissions
    result[ADMIN_ID] = list(ALL_COMMANDS)
    for k, v in raw.items():
        uid = int(k)
        if uid != ADMIN_ID:
            result[uid] = v if isinstance(v, list) else list(ALL_COMMANDS)
    return result

def get_admin_permissions(user_id: int) -> list:
    """Retourne les commandes autorisées pour un admin donné."""
    if user_id == ADMIN_ID:
        return list(ALL_COMMANDS)
    raw = _load_admins_raw()
    entry = raw.get(str(user_id))
    if entry is None:
        return []
    return entry if isinstance(entry, list) else list(ALL_COMMANDS)

def has_permission(user_id: int, command: str) -> bool:
    """Vérifie si un admin peut utiliser une commande."""
    if user_id == ADMIN_ID:
        return True
    perms = get_admin_permissions(user_id)
    return command in perms

def add_admin(user_id: int, commands: list = None) -> bool:
    """Ajoute un admin avec sa liste de commandes autorisées."""
    if user_id == ADMIN_ID:
        return False
    raw = _load_admins_raw()
    if str(user_id) in raw:
        return False
    raw[str(user_id)] = commands if commands is not None else list(ALL_COMMANDS)
    _save_admins_raw(raw)
    return True

def update_admin_permissions(user_id: int, commands: list) -> bool:
    """Met à jour les permissions d'un admin existant."""
    if user_id == ADMIN_ID:
        return False
    raw = _load_admins_raw()
    if str(user_id) not in raw:
        return False
    raw[str(user_id)] = commands
    _save_admins_raw(raw)
    return True

def remove_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False
    raw = _load_admins_raw()
    if str(user_id) not in raw:
        return False
    del raw[str(user_id)]
    _save_admins_raw(raw)
    return True


# ── Configuration de prédiction multi-canal ───────────────────────────────────

def get_predict_config() -> dict:
    """
    Retourne la config de prédiction.
    Format : {
      'channels': {channel_id: 'stats'|'predictor'},
      'configured': bool,
    }
    """
    return load_json(PREDICT_CONFIG_FILE, {'channels': {}, 'configured': False})

def save_predict_config(config: dict):
    save_json(PREDICT_CONFIG_FILE, config)

def set_channel_role(channel_id: str, role: str):
    """role = 'stats' | 'predictor'."""
    cfg = get_predict_config()
    cfg['channels'][str(channel_id)] = role
    cfg['configured'] = True
    save_predict_config(cfg)

def get_stats_channels() -> list:
    """Retourne les IDs des canaux marqués 'stats'."""
    cfg = get_predict_config()
    return [cid for cid, role in cfg.get('channels', {}).items() if role == 'stats']

def get_predictor_channels() -> list:
    """Retourne les IDs des canaux marqués 'predictor'."""
    cfg = get_predict_config()
    return [cid for cid, role in cfg.get('channels', {}).items() if role == 'predictor']

def reset_predict_config():
    save_predict_config({'channels': {}, 'configured': False})
