import re
import os
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel, PeerChannel
from config import API_ID, API_HASH, SESSION_PATH, CHANNEL_USERNAME, CHANNEL_ID, TELETHON_SESSION_STRING
from storage import add_prediction, get_last_sync, update_last_sync

PATTERN = re.compile(
    r'PRÉDICTION\s*#(\d+).*?'
    r'Couleur:\s*([^\n]+).*?'
    r'Statut:\s*([^\n]+)',
    re.IGNORECASE | re.DOTALL
)

def _load_session_string():
    """Charge la StringSession depuis le fichier (si re-auth récente) ou la config."""
    session_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'data', 'session_string.txt')
    # Aussi chercher dans DATA_DIR
    from config import DATA_DIR
    data_session_file = os.path.join(DATA_DIR, 'session_string.txt')
    for path in [data_session_file, session_file]:
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    s = f.read().strip()
                    if s:
                        return s
            except Exception:
                pass
    return TELETHON_SESSION_STRING


class Scraper:
    def __init__(self):
        self._make_client()

    def _make_client(self):
        self.client = TelegramClient(StringSession(_load_session_string()), API_ID, API_HASH)

    async def _get_channel(self):
        """Trouve le canal par ID numérique, puis par nom si besoin."""
        # 1. Essai par ID numérique direct (le plus fiable)
        try:
            entity = await self.client.get_entity(PeerChannel(abs(CHANNEL_ID) % 10**10))
            if isinstance(entity, Channel):
                return entity
        except Exception:
            pass

        # 2. Essai avec l'ID complet tel quel
        try:
            entity = await self.client.get_entity(CHANNEL_ID)
            if isinstance(entity, Channel):
                return entity
        except Exception:
            pass

        # 3. Chercher dans les dialogues (conversations actives)
        try:
            async for dialog in self.client.iter_dialogs():
                if dialog.is_channel:
                    name = dialog.name or ""
                    if (CHANNEL_USERNAME.lower() in name.lower() or
                            name.lower() in CHANNEL_USERNAME.lower()):
                        return dialog.entity
        except Exception:
            pass

        raise Exception(
            f"Canal introuvable. Assurez-vous que le compte `{'+22995501564'}` "
            f"est bien membre du canal '{CHANNEL_USERNAME}'."
        )

    async def sync(self, full=False, progress_callback=None):
        """Synchronise le canal VIP DE KOUAMÉ & JOKER"""
        self._make_client()
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise Exception("Non authentifié. Tapez /connect puis /code d'abord.")

        try:
            entity = await self._get_channel()

            total = 0
            last_id = 0
            min_id = 0 if full else get_last_sync().get('last_message_id', 0)

            async for message in self.client.iter_messages(entity, limit=50000, min_id=min_id):
                if not message.text:
                    continue

                match = PATTERN.search(message.text)
                if match:
                    added = add_prediction(
                        message_id=message.id,
                        numero=match.group(1),
                        couleur=match.group(2).strip(),
                        statut=match.group(3).strip(),
                        raw_text=message.text[:500]
                    )
                    if added:
                        total += 1

                if message.id > last_id:
                    last_id = message.id

                if total % 100 == 0 and progress_callback:
                    await progress_callback(total)

            if last_id > 0:
                update_last_sync(last_id)

            return {'new': total, 'last_id': last_id}

        finally:
            await self.client.disconnect()

    async def search_in_channel(self, keywords, limit=5000, progress_callback=None):
        """Recherche des messages contenant les mots-clés dans le canal"""
        self._make_client()
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise Exception("Non authentifié. Tapez /connect puis /code d'abord.")

        try:
            entity = await self._get_channel()

            keywords_lower = [k.strip().lower() for k in keywords if k.strip()]
            found = []
            checked = 0

            async for message in self.client.iter_messages(entity, limit=limit):
                if not message.text:
                    continue
                checked += 1
                text_lower = message.text.lower()
                if all(kw in text_lower for kw in keywords_lower):
                    found.append({
                        'id': message.id,
                        'text': message.text[:800],
                        'date': str(message.date)
                    })

                if progress_callback and checked % 500 == 0:
                    await progress_callback(checked, len(found))

            return found

        finally:
            await self.client.disconnect()

    async def search_in_any_channel(self, channel_id: str, keywords, limit=None,
                                     from_date=None, progress_callback=None,
                                     cancel_check=None):
        """Recherche dans n'importe quel canal par son ID.

        Args:
            limit: Nombre max de messages à analyser (None = tout l'historique).
            from_date: datetime (UTC) — arrête dès qu'on dépasse cette date vers le passé.
            cancel_check: callable() → bool — si True, interrompt la recherche.
        """
        self._make_client()
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise Exception("Non authentifié. Tapez /connect puis /code d'abord.")

        try:
            try:
                cid = int(channel_id)
            except ValueError:
                cid = channel_id

            try:
                entity = await self.client.get_entity(cid)
            except Exception:
                from telethon.tl.types import PeerChannel
                entity = await self.client.get_entity(PeerChannel(abs(cid) % 10**10))

            keywords_lower = [k.strip().lower() for k in keywords if k.strip()]
            found = []
            checked = 0
            iter_limit = limit  # None = pas de limite Telethon

            cancelled = False
            async for message in self.client.iter_messages(entity, limit=iter_limit):
                if cancel_check and cancel_check():
                    cancelled = True
                    break

                if not message.text:
                    continue

                # Filtre par date : Telethon itère du plus récent au plus ancien
                if from_date and message.date and message.date < from_date:
                    break

                checked += 1
                text_lower = message.text.lower()
                if all(kw in text_lower for kw in keywords_lower):
                    found.append({
                        'id': message.id,
                        'text': message.text[:800],
                        'date': str(message.date)
                    })

                if progress_callback and checked % 200 == 0:
                    await progress_callback(checked, len(found))

            return found, entity.title if hasattr(entity, 'title') else str(channel_id), cancelled

        finally:
            await self.client.disconnect()

    async def get_game_records(self, channel_id: str, limit=None, from_date=None,
                               progress_callback=None, cancel_check=None):
        """Récupère tous les messages au format #N... du canal donné.

        Args:
            limit: Nombre max de messages à parcourir (None = tout l'historique).
            from_date: datetime (UTC) — arrête dès qu'on dépasse cette date.
            cancel_check: callable() → bool — si True, interrompt la récupération.
        """
        from game_analyzer import GAME_PATTERN
        self._make_client()
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise Exception("Non authentifié. Tapez /connect puis /code d'abord.")

        try:
            try:
                cid = int(channel_id)
            except ValueError:
                cid = channel_id
            try:
                entity = await self.client.get_entity(cid)
            except Exception:
                from telethon.tl.types import PeerChannel
                entity = await self.client.get_entity(PeerChannel(abs(cid) % 10**10))

            records = []
            checked = 0
            cancelled = False

            async for message in self.client.iter_messages(entity, limit=limit):
                if cancel_check and cancel_check():
                    cancelled = True
                    break

                if not message.text:
                    continue

                if from_date and message.date and message.date < from_date:
                    break

                checked += 1
                txt = message.text
                # Inclure si le pattern principal correspond OU si c'est un match nul
                # reconnaissable (#N ... 🔰 ... #T) avec un format légèrement différent
                if GAME_PATTERN.search(txt) or (
                    '🔰' in txt and '#N' in txt and '#T' in txt
                ):
                    records.append(txt)
                if progress_callback and checked % 200 == 0:
                    await progress_callback(checked, len(records))

            title = entity.title if hasattr(entity, 'title') else str(channel_id)
            return records, title, cancelled

        finally:
            await self.client.disconnect()


scraper = Scraper()
