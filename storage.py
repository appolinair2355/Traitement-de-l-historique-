import json
import os
from datetime import datetime
from config import PREDICTIONS_FILE, LAST_SYNC_FILE, ensure_data_dir

# Créer le dossier au chargement
ensure_data_dir()

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
    
