import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager
import os

DB_PATH = os.getenv('DATABASE_PATH', '/data/predictions.db')

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER UNIQUE,
                numero TEXT,
                couleur TEXT,
                statut TEXT,
                raw_text TEXT,
                date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS last_sync (
                id INTEGER PRIMARY KEY,
                last_message_id INTEGER DEFAULT 0,
                sync_date TIMESTAMP
            )
        ''')
        conn.execute('''
            INSERT OR IGNORE INTO last_sync (id, last_message_id, sync_date) 
            VALUES (1, 0, NULL)
        ''')

def save_prediction(message_id, numero, couleur, statut, raw_text):
    with get_db() as conn:
        conn.execute('''
            INSERT OR REPLACE INTO predictions 
            (message_id, numero, couleur, statut, raw_text, date)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (message_id, numero, couleur, statut, raw_text, datetime.now()))

def get_predictions(filters=None):
    with get_db() as conn:
        query = "SELECT * FROM predictions WHERE 1=1"
        params = []
        
        if filters:
            if filters.get('couleur'):
                query += " AND couleur LIKE ?"
                params.append(f"%{filters['couleur']}%")
            if filters.get('statut'):
                query += " AND statut LIKE ?"
                params.append(f"%{filters['statut']}%")
            if filters.get('numero'):
                query += " AND numero = ?"
                params.append(filters['numero'])
        
        query += " ORDER BY message_id DESC"
        
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def get_last_sync():
    with get_db() as conn:
        row = conn.execute('SELECT * FROM last_sync WHERE id = 1').fetchone()
        return dict(row) if row else {'last_message_id': 0}

def update_last_sync(message_id):
    with get_db() as conn:
        conn.execute('''
            UPDATE last_sync 
            SET last_message_id = ?, sync_date = ?
            WHERE id = 1
        ''', (message_id, datetime.now()))

def get_stats():
    with get_db() as conn:
        total = conn.execute('SELECT COUNT(*) FROM predictions').fetchone()[0]
        return {'total': total}
