import re
from telethon import TelegramClient
from telethon.tl.types import Channel, PeerChannel
from config import API_ID, API_HASH, SESSION_PATH, CHANNEL_USERNAME, CHANNEL_ID
from storage import add_prediction, get_last_sync, update_last_sync

PATTERN = re.compile(
    r'PRÉDICTION\s*#(\d+).*?'
    r'Couleur:\s*([^\n]+).*?'
    r'Statut:\s*([^\n]+)',
    re.IGNORECASE | re.DOTALL
)

class Scraper:
    def __init__(self):
        self.client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

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


scraper = Scraper()
