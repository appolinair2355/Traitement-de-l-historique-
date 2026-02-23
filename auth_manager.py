import json
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from config import API_ID, API_HASH, SESSION_PATH, AUTH_STATE_FILE, USER_PHONE, DATA_DIR, TELETHON_SESSION_STRING


def _save_session_string(session_str: str):
    """Sauvegarde la nouvelle StringSession dans DATA_DIR pour les prochaines connexions."""
    path = os.path.join(DATA_DIR, 'session_string.txt')
    try:
        with open(path, 'w') as f:
            f.write(session_str)
        os.chmod(path, 0o664)
    except Exception as e:
        pass


class AuthManager:
    def __init__(self):
        self.client = None
        self._load_state()

    def _load_state(self):
        if os.path.exists(AUTH_STATE_FILE):
            with open(AUTH_STATE_FILE, 'r') as f:
                self.state = json.load(f)
        else:
            self.state = {'step': 'idle'}

    def _save_state(self):
        with open(AUTH_STATE_FILE, 'w') as f:
            json.dump(self.state, f)

    def is_connected(self):
        # Vérifie via StringSession (pas de fichier requis)
        return self.state.get('step') == 'connected' or os.path.exists(SESSION_PATH + ".session")

    async def send_code(self):
        """Envoie le code SMS avec une StringSession vide."""
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

        # Nouvelle session vide pour l'authentification
        self.client = TelegramClient(StringSession(), API_ID, API_HASH)
        await self.client.connect()

        try:
            result = await self.client.send_code_request(USER_PHONE)

            self.state = {
                'step': 'waiting_code',
                'phone_code_hash': result.phone_code_hash
            }
            self._save_state()

            return True, (
                f"📲 Code envoyé à `{USER_PHONE}`\n\n"
                f"Tapez: `/code aa` suivi du code reçu\n"
                f"Exemple: `/code aa43481`\n\n"
                f"⚠️ Entrez le code rapidement (il expire en 2 minutes)"
            )

        except Exception as e:
            await self.client.disconnect()
            self.client = None
            return False, f"❌ Erreur envoi code: {str(e)}"

    async def verify_code(self, code: str):
        """Vérifie le code reçu par SMS et sauvegarde la nouvelle session."""
        if self.state.get('step') != 'waiting_code':
            return False, "❌ Pas de code en attente. Tapez /connect d'abord."

        phone_code_hash = self.state.get('phone_code_hash')
        if not phone_code_hash:
            return False, "❌ Session invalide. Retapez /connect."

        real_code = code[2:] if code.startswith('aa') else code
        real_code = real_code.strip()

        if self.client is None or not self.client.is_connected():
            self.state = {'step': 'idle'}
            self._save_state()
            return False, (
                "❌ La connexion a été interrompue (redémarrage du bot).\n"
                "Tapez /connect pour recevoir un nouveau code."
            )

        try:
            await self.client.sign_in(
                phone=USER_PHONE,
                code=real_code,
                phone_code_hash=phone_code_hash
            )

            # Sauvegarder la nouvelle StringSession
            new_session_str = self.client.session.save()
            if new_session_str:
                _save_session_string(new_session_str)

            self.state = {'step': 'connected'}
            self._save_state()

            await self.client.disconnect()
            self.client = None
            return True, "✅ Connecté avec succès ! Utilisez /sync ou /fullsync"

        except SessionPasswordNeededError:
            await self.client.disconnect()
            self.client = None
            return False, "❌ Ce compte a la validation en 2 étapes. Désactivez-la dans Telegram d'abord."

        except Exception as e:
            err = str(e)
            if 'PHONE_CODE_INVALID' in err:
                return False, "❌ Code incorrect. Vérifiez et réessayez avec `/code aaXXXXXX`"
            if 'PHONE_CODE_EXPIRED' in err:
                self.state = {'step': 'idle'}
                self._save_state()
                await self.client.disconnect()
                self.client = None
                return False, "❌ Code expiré. Tapez /connect pour recevoir un nouveau code."
            return False, f"❌ Erreur: {err}"

    async def reset(self):
        """Déconnexion et nettoyage."""
        try:
            if self.client and self.client.is_connected():
                await self.client.disconnect()
        except Exception:
            pass
        self.client = None
        self.state = {'step': 'idle'}
        self._save_state()
        # Supprimer la session sauvegardée
        for path in [
            SESSION_PATH + ".session",
            os.path.join(DATA_DIR, 'session_string.txt')
        ]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


auth_manager = AuthManager()
