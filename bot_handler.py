import os
import asyncio
import logging
import html
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                           ContextTypes, MessageHandler, filters)
from config import BOT_TOKEN, ADMIN_ID, CHANNEL_USERNAME, USER_PHONE

logger = logging.getLogger(__name__)

# Ensemble de tâches de suppression pour éviter le garbage collection
_pending_deletions: set = set()

def _max_ecart(nums):
    """Calcule l'écart maximum entre numéros consécutifs triés."""
    if len(nums) < 2:
        return 0
    s = sorted(int(n) for n in nums)
    return max(s[i+1] - s[i] for i in range(len(s)-1))

async def _delete_after_delay(msg, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
        logger.info(f"Message {msg.message_id} supprimé après {delay}s")
    except Exception as e:
        logger.warning(f"Impossible de supprimer message {msg.message_id}: {e}")

def _schedule_delete(msg, delay: int = 10):
    task = asyncio.create_task(_delete_after_delay(msg, delay))
    _pending_deletions.add(task)
    task.add_done_callback(_pending_deletions.discard)


# ── Détection et formatage des erreurs Telethon critiques ─────────────────────
_AUTH_KEY_MARKERS = (
    "authorization key",
    "authkeyduplicatederror",
    "auth_key_duplicated",
    "used under two different ip",
    "two different ip addresses",
)

def _is_auth_key_dup(e: Exception) -> bool:
    """Détecte AuthKeyDuplicatedError de Telethon par le message d'erreur."""
    msg = str(e).lower()
    return any(m in msg for m in _AUTH_KEY_MARKERS)


_AUTH_KEY_DUP_MSG = (
    "🔑 <b>Conflit de session Telethon</b>\n\n"
    "La même clé de session est utilisée <b>simultanément depuis deux serveurs</b> "
    "(ex. Replit + Render en même temps).\n\n"
    "<b>Pour corriger :</b>\n"
    "  1. Arrêtez l'une des instances (gardez <b>seulement Replit OU Render</b>)\n"
    "  2. Tapez <b>/disconnect</b> pour effacer la session corrompue\n"
    "  3. Tapez <b>/connect</b> puis <b>/code</b> pour vous reconnecter\n\n"
    "⚠️ <i>N'utilisez jamais la même session depuis deux serveurs simultanément.</i>"
)


from storage import (get_predictions, get_stats, clear_all, search_predictions,
                     get_channels, add_channel, remove_channel,
                     get_active_channel, set_active_channel,
                     get_analyzed_games, save_analyzed_games, clear_analyzed_games,
                     get_admins, get_admins_with_permissions, get_admin_permissions,
                     has_permission, add_admin, remove_admin, update_admin_permissions,
                     get_predict_config, save_predict_config, set_channel_role,
                     get_stats_channels, get_predictor_channels, reset_predict_config,
                     ALL_COMMANDS)
from game_analyzer import (parse_game, format_analysis, build_category_stats,
                           format_ecarts, normalize_suit, SUIT_EMOJI)
from predictor import (generate_category_list, format_category_list,
                       build_predict_data, format_global_summary,
                       generate_top_predictions)
from scraper import scraper
from auth_manager import auth_manager
from pdf_generator import generate_pdf, generate_search_pdf, generate_channel_search_pdf, generate_costume_pdf
from pdf_analyzer import analyze_pdf

def is_admin(user_id: int) -> bool:
    """Vrai si l'utilisateur est dans la liste des admins (incluant le main admin)."""
    return user_id in get_admins()

def is_main_admin(user_id: int) -> bool:
    """Vrai uniquement pour l'admin principal (commandes sensibles)."""
    return user_id == ADMIN_ID

def parse_date(s: str):
    """Parse une date/heure en datetime UTC. Retourne None si invalide."""
    formats = ['%Y-%m-%d', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M',
               '%d/%m/%Y', '%d/%m/%Y %H:%M', '%d-%m-%Y', '%d-%m-%Y %H:%M']
    for fmt in formats:
        try:
            dt = datetime.strptime(s.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def parse_search_options(args: list):
    """Sépare les mots-clés des options limit:, from:/depuis: et to:/fin:/jusqu'au:.

    Retourne (keywords, limit, from_date, to_date).
    Options reconnues :
      limit:500              → analyser 500 derniers messages
      from:2024-01-15        → depuis cette date (début)
      from:2024-01-15 10:30  → date + heure (espace accepté)
      from:2024-01-15T10:30  → date + heure (T accepté)
      depuis:2024-01-15      → alias de from:
      to:2024-01-20          → jusqu'à cette date (fin)
      to:2024-01-20 23:59    → date de fin + heure
      fin:2024-01-20         → alias de to:
      jusqu'au:2024-01-20    → alias de to:
    """
    import re as _re
    keywords = []
    limit = None
    from_date = None
    to_date = None
    i = 0
    while i < len(args):
        arg = args[i]
        lo = arg.lower()
        if lo.startswith('limit:'):
            try:
                limit = int(arg[6:])
            except ValueError:
                pass
        elif lo.startswith('from:') or lo.startswith('depuis:'):
            date_val = arg.split(':', 1)[1]
            if i + 1 < len(args) and _re.match(r'^\d{1,2}:\d{2}$', args[i + 1]):
                date_val += ' ' + args[i + 1]
                i += 1
            from_date = parse_date(date_val)
        elif (lo.startswith('to:') or lo.startswith('fin:')
              or lo.startswith("jusqu'au:") or lo.startswith('jusquau:')):
            date_val = arg.split(':', 1)[1]
            if i + 1 < len(args) and _re.match(r'^\d{1,2}:\d{2}$', args[i + 1]):
                date_val += ' ' + args[i + 1]
                i += 1
            to_date = parse_date(date_val)
        else:
            keywords.append(arg)
        i += 1
    return keywords, limit, from_date, to_date


def _filter_games_by_date(games: list, from_date=None, to_date=None) -> list:
    """Filtre une liste de jeux par plage de dates (champ 'date' du jeu)."""
    if not from_date and not to_date:
        return games
    result = []
    for g in games:
        date_str = g.get('date', '')
        if not date_str:
            result.append(g)
            continue
        try:
            dt = datetime.fromisoformat(str(date_str).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if from_date and dt < from_date:
                continue
            if to_date and dt > to_date:
                continue
            result.append(g)
        except Exception:
            result.append(g)
    return result

# État de la conversation : attend un ID de canal de l'admin
_waiting_for_channel = {}
# État : attend un enregistrement de jeu pour analyse
_waiting_for_game = {}
# Flags d'annulation par utilisateur pour les recherches en cours
_search_cancel: dict[int, bool] = {}
# État : attend la sélection de commandes pour un nouvel admin
# {main_admin_uid: {'target_uid': int, 'action': 'add'|'update'}}
_waiting_for_perm: dict[int, dict] = {}
# État : attend le choix du canal dans /helpcl
_waiting_for_helpcl: dict[int, bool] = {}
# État : attend la saisie des rôles dans /predictsetup
# {uid: {'step': str, 'channels': list}}
_waiting_for_predict: dict[int, dict] = {}
# Mapping costumes → emojis pour l'export public
_DS_SUIT_EMOJI = {'♠': '♠️', '♥': '❤️', '♦': '♦️', '♣': '♣️'}

# État persistant de la recherche publique (fichier partagé entre instances)
_DS_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data', 'dsearch_state.json'
)

def _ds_load(uid: int) -> dict:
    """Charge l'état de recherche d'un utilisateur depuis le fichier."""
    try:
        import json as _json
        if not os.path.exists(_DS_STATE_FILE):
            return {}
        with open(_DS_STATE_FILE, 'r', encoding='utf-8') as f:
            data = _json.load(f)
        return data.get(str(uid), {})
    except Exception:
        return {}

def _ds_save(uid: int, state: dict):
    """Sauvegarde l'état de recherche d'un utilisateur dans le fichier."""
    try:
        import json as _json
        os.makedirs(os.path.dirname(_DS_STATE_FILE), exist_ok=True)
        try:
            with open(_DS_STATE_FILE, 'r', encoding='utf-8') as f:
                data = _json.load(f)
        except Exception:
            data = {}
        data[str(uid)] = state
        with open(_DS_STATE_FILE, 'w', encoding='utf-8') as f:
            _json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass

def _ds_clear(uid: int):
    """Efface l'état de recherche d'un utilisateur."""
    try:
        import json as _json
        if not os.path.exists(_DS_STATE_FILE):
            return
        with open(_DS_STATE_FILE, 'r', encoding='utf-8') as f:
            data = _json.load(f)
        data.pop(str(uid), None)
        with open(_DS_STATE_FILE, 'w', encoding='utf-8') as f:
            _json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _clear_waits(uid: int):
    """Efface tous les états d'attente d'un utilisateur.
    Appelé automatiquement dès qu'une nouvelle commande est reçue,
    pour éviter qu'un ancien état bloque le nouveau flux."""
    _waiting_for_channel.pop(uid, None)
    _waiting_for_game.pop(uid, None)
    _waiting_for_perm.pop(uid, None)
    _waiting_for_helpcl.pop(uid, None)
    _waiting_for_predict.pop(uid, None)
    _ds_clear(uid)

def _build_channel_menu(channels: list) -> str:
    """Construit le menu numéroté des canaux pour /helpcl."""
    lines = ["📡 <b>CANAUX CONFIGURÉS</b>\n"]
    for i, ch in enumerate(channels, 1):
        name = ch.get('name') or ch['id']
        cid = ch['id']
        date = ch.get('added_date', 'N/A')
        mark = " ▶️" if ch.get('active') else ""
        lines.append(f"<b>{i}.</b> {name}{mark}\n   ID : <code>{cid}</code>\n   Ajouté : {date}")
    lines.append("\n✏️ Tapez le <b>numéro</b> du canal à utiliser pour les analyses")
    lines.append("Tapez <b>sortir</b> pour quitter sans changer")
    return '\n'.join(lines)

def _build_cmd_menu(target_uid: int, action: str) -> str:
    """Construit le menu numéroté des commandes disponibles."""
    verb = "Ajouter" if action == 'add' else "Modifier les permissions de"
    lines = [f"📋 <b>{verb} l'admin <code>{target_uid}</code></b>\n"]
    lines.append("Choisissez les commandes autorisées :\n")
    for i, cmd in enumerate(ALL_COMMANDS, 1):
        lines.append(f"  <b>{i}.</b> {cmd}")
    lines.append("\n✏️ Tapez les numéros séparés par des virgules")
    lines.append("Ex : <code>1,3,4</code>  ou  <code>1-5,8,13</code>")
    lines.append("\n/cancel pour annuler")
    return '\n'.join(lines)

def _main_menu_keyboard(is_main: bool = True) -> InlineKeyboardMarkup:
    """Clavier principal du bot organisé par section."""
    rows = [
        # ── Commande publique — bien séparée des outils admin ──
        [InlineKeyboardButton("🔎 Recherche SpécialeB", callback_data="menu:rechercheB")],
        # ── Outils admin ──
        [InlineKeyboardButton("🔍 Recherche",      callback_data="menu:recherche"),
         InlineKeyboardButton("🔮 Prédiction",     callback_data="menu:prediction")],
        [InlineKeyboardButton("📊 Analyse",         callback_data="menu:analyse"),
         InlineKeyboardButton("🔄 Cycles",          callback_data="menu:cycles")],
        [InlineKeyboardButton("📡 Canaux",          callback_data="menu:canaux"),
         InlineKeyboardButton("📚 Documentation",  callback_data="menu:doc")],
    ]
    if is_main:
        rows.append([InlineKeyboardButton("👥 Administration", callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Menu principal", callback_data="menu:accueil")]
    ])


# Textes de chaque section du menu
_MENU_SECTIONS = {
    "recherche": (
        "🔍 <b>RECHERCHE</b>\n\n"
        "<b>/hsearch</b> — Recherche dans l'historique du canal actif\n"
        "  <code>/hsearch GAGNÉ Cœur</code>\n"
        "  <code>/hsearch GAGNÉ from:2026-02-20 to:2026-02-23</code>\n"
        "  <code>/hsearch GAGNÉ limit:500</code>\n\n"
        "<b>/searchcard</b> — Recherche par valeur de carte (A, K, Q, J)\n"
        "  <code>/searchcard K joueur</code>\n"
        "  <code>/searchcard A banquier from:2026-02-20 to:2026-02-23</code>\n\n"
        "<b>/search</b> — Recherche dans les données locales (export PDF)\n"
        "  <code>/search rouge gagné</code>\n\n"
        "💡 <i>Options disponibles partout : from:DATE  to:DATE  limit:N</i>"
    ),
    "prediction": (
        "🔮 <b>PRÉDICTION</b>\n\n"
        "<b>Étape 1 — Charger les jeux :</b>\n"
        "  <code>/gload from:2026-02-20 to:2026-02-23</code>\n"
        "  <code>/gload limit:500</code>\n\n"
        "<b>Étape 2 — Lancer les prédictions :</b>\n"
        "  <code>/gpredict 30</code> — Les 30 prochains jeux\n"
        "  <code>/gpredict 900 950</code> — Du jeu #900 au #950\n"
        "  <code>/gpredict 30 from:2026-02-20 to:2026-02-23</code>\n\n"
        "<b>🔝 TOP prédictions (classées par confiance) :</b>\n"
        "  <code>/gtop</code> — Top prédictions sur les 30 prochains jeux\n"
        "  <code>/gtop 50</code> — Sur les 50 prochains jeux\n\n"
        "<b>Autres :</b>\n"
        "  <code>/gpredictload</code> — Charger depuis canaux de stats\n"
        "  <code>/ganalyze</code> — Analyser un enregistrement (copier-coller)\n"
        "  <code>/predictsetup</code> — Configurer les canaux de prédiction\n\n"
        "💡 <i>Chaque prédiction montre : fréquence, écart moyen, retard actuel,\n"
        "ratio de retard et barre de confiance visuelle.</i>"
    ),
    "analyse": (
        "📊 <b>ANALYSE</b>\n\n"
        "<b>/gstats</b> — Résumé complet des jeux chargés\n\n"
        "<b>Catégories d'analyse :</b>\n"
        "  <b>/gvictoire</b> — Victoires par résultat\n"
        "    <code>/gvictoire joueur</code>  <code>/gvictoire banquier</code>  <code>/gvictoire nul</code>\n\n"
        "  <b>/gparite</b> — Parité du total\n"
        "    <code>/gparite pair</code>  <code>/gparite impair</code>\n\n"
        "  <b>/gstructure</b> — Structure des cartes\n"
        "    <code>/gstructure 2/2</code>  <code>/gstructure 2/3</code>  <code>/gstructure 3/2</code>  <code>/gstructure 3/3</code>\n\n"
        "  <b>/gplusmoins</b> — Plus/Moins de 6,5 ou 4,5\n"
        "    <code>/gplusmoins j plus</code>  <code>/gplusmoins b moins</code>\n\n"
        "  <b>/gcostume</b> — Costumes manquants par main\n"
        "    <code>/gcostume ♠ j</code>  <code>/gcostume ♥ b</code>\n\n"
        "  <b>/gvaleur</b> — Valeurs spéciales par costume (A♠, K♦…)\n"
        "    <code>/gvaleur A</code>  <code>/gvaleur K joueur</code>\n\n"
        "<b>Écarts :</b>\n"
        "  <b>/gecartmax</b> — Écart maximum dans toutes les catégories\n\n"
        "<b>/gclear</b> — Effacer les jeux chargés"
    ),
    "cycles": (
        "🔄 <b>CORRECTION DE CYCLES DE COSTUMES</b>\n\n"
        "<b>/gcycle</b> — Vérifier un cycle prédéfini\n"
        "  <code>/gcycle pair</code> — Tester cycle pairs (sauf ×10)\n"
        "  <code>/gcycle impair</code> — Tester cycle impairs + ×10\n"
        "  <code>/gcycle pair j</code> — Côté joueur seulement\n"
        "  <code>/gcycle impair b 6-1436</code> — Plage spécifique\n\n"
        "<b>/gcycleauto</b> — Trouver le meilleur cycle automatiquement\n"
        "  <code>/gcycleauto</code> — Recherche complète\n"
        "  <code>/gcycleauto j</code> — Côté joueur seulement\n"
        "  <code>/gcycleauto b 6-1436</code> — Plage spécifique\n\n"
        "💡 <i>La correction dresse la liste complète :\n"
        "numéro [costume corrigé] pour chaque jeu qualifiant,\n"
        "comme dans l'analyse PDF.</i>"
    ),
    "canaux": (
        "📡 <b>GESTION DES CANAUX</b>\n\n"
        "<b>/addchannel</b> — Ajouter un canal (ID ou @username)\n\n"
        "<b>/helpcl</b> — Sélectionner le canal actif (menu numéroté)\n"
        "  → Tapez le numéro dans la liste pour activer\n\n"
        "<b>/channels</b> — Voir tous les canaux configurés\n\n"
        "<b>/usechannel -1001234567890</b> — Activer un canal par ID\n\n"
        "<b>/removechannel -1001234567890</b> — Supprimer un canal\n\n"
        "💡 <i>Après /addchannel, utilisez /gload pour charger les jeux du canal actif.</i>"
    ),
    "rechercheB": (
        "🔎 <b>RECHERCHE SPÉCIALE B</b>\n\n"
        "Commande publique — accessible à <b>tous les utilisateurs</b>.\n\n"
        "<b>/recherche</b> — Lancer une recherche dans un canal Baccarat\n\n"
        "<b>Étapes :</b>\n"
        "  1️⃣ Choisir le canal à analyser (liste numérotée)\n"
        "  2️⃣ Saisir la date  <code>10/03/2026</code> ou <code>2026-03-10</code>\n"
        "  3️⃣ Saisir le terme à chercher :\n"
        "      <code>joueur</code>  <code>banquier</code>  <code>nul</code>\n"
        "      <code>♠</code>  <code>♥</code>  <code>♦</code>  <code>♣</code>\n"
        "      <code>K</code>  <code>A</code>  <code>Q</code>  <code>J</code>\n\n"
        "<b>Résultat :</b>\n"
        "  • Aperçu des 20 premiers numéros et costumes\n"
        "  • Puis choix : continuer ou recevoir le fichier complet\n"
        "  • Fichier exporté au format <code>numero:costume</code>\n\n"
        "💡 <i>Tapez <b>annuler</b> à tout moment pour quitter.</i>"
    ),
    "doc": (
        "📚 <b>DOCUMENTATION</b>\n\n"
        "Tapez <b>/documentation</b> pour recevoir le guide PDF complet\n"
        "avec des exemples détaillés pour toutes les commandes.\n\n"
        "<b>Format des dates (toutes commandes) :</b>\n"
        "  <code>from:2026-02-20</code> — depuis le 20 fév.\n"
        "  <code>from:2026-02-20 08:00</code> — depuis le 20 fév. à 8h\n"
        "  <code>to:2026-02-23</code> — jusqu'au 23 fév.\n"
        "  <code>to:2026-02-23 22:00</code> — jusqu'au 23 fév. à 22h\n\n"
        "<b>Format des enregistrements Baccarat :</b>\n"
        "  <code>#N794. ✅3(K♦️4♦️9♦️) - 1(J♦️10♥️A♠️) #T4</code>\n\n"
        "<b>/cancel</b> — Annuler n'importe quelle opération en cours\n"
        "<b>/myid</b> — Afficher votre Telegram ID"
    ),
    "admin": (
        "👥 <b>ADMINISTRATION</b>\n\n"
        "<b>/addadmin 123456789</b> — Ajouter un administrateur\n"
        "  → Menu de sélection des commandes autorisées\n"
        "  → Ex : <code>1,3,5</code> ou <code>1-8,13</code>\n\n"
        "<b>/setperm 123456789</b> — Modifier les permissions d'un admin\n\n"
        "<b>/removeadmin 123456789</b> — Supprimer un administrateur\n\n"
        "<b>/admins</b> — Liste de tous les admins et leurs commandes\n\n"
        "<b>/connect</b> — Connexion Telegram (code SMS)\n"
        "<b>/disconnect</b> — Déconnexion Telegram\n\n"
        "💡 <i>Les sous-admins ne voient que leurs commandes autorisées.</i>"
    ),
}


class Handlers:
    def __init__(self):
        self.syncing = False

    async def _perm(self, update: Update, command: str) -> bool:
        """Vérifie que l'utilisateur est admin ET a accès à cette commande."""
        uid = update.effective_user.id
        if not is_admin(uid):
            return False
        if is_main_admin(uid):
            return True
        if not has_permission(uid, command):
            await update.message.reply_text(f"❌ Vous n'avez pas accès à la commande /{command}.")
            return False
        return True

    # Descriptions courtes pour chaque commande (utilisées dans /start sous-admin et /help)
    _CMD_DESC = {
        'sync':         'Récupérer les messages récents du canal actif',
        'fullsync':     'Récupérer tout l\'historique du canal actif',
        'search':       'Chercher des mots-clés et exporter en PDF',
        'hsearch':      'Chercher dans l\'historique du canal actif',
        'report':       'Générer un PDF de toutes les prédictions',
        'filter':       'Filtrer par couleur ou statut',
        'stats':        'Statistiques des prédictions stockées',
        'clear':        'Effacer toutes les données locales',
        'addchannel':   'Ajouter un nouveau canal',
        'removechannel':'Supprimer un canal de la liste',
        'channels':     'Voir tous les canaux configurés',
        'usechannel':   'Activer un canal par ID',
        'helpcl':       'Sélectionner le canal actif (menu numéroté)',
        'gload':        'Charger des jeux Baccarat depuis le canal',
        'gstats':       'Statistiques des jeux chargés',
        'gclear':       'Effacer les jeux chargés',
        'ganalyze':     'Analyser un enregistrement de jeu (copier-coller)',
        'gvictoire':    'Numéros et écarts par résultat (Joueur/Banquier/Nul)',
        'gparite':      'Numéros et écarts par parité (Pair/Impair)',
        'gstructure':   'Structure des cartes par main (2/2, 2/3, 3/2, 3/3)',
        'gplusmoins':   'Analyse Plus/Moins de 6.5 ou 4.5',
        'gcostume':     'Probabilité costume par main (♠ ❤ ♦ ♣ Joueur/Banquier)',
        'gvaleur':      'Valeurs spéciales par costume (A♠, K♦, Q♥, J♣…)',
        'gcycle':       'Vérifier cycle de costumes sur jeux pairs (sauf ×10)',
        'gcycleauto':   'Recherche auto du meilleur cycle + filtre de numéros',
        'gecartmax':    'Paires ayant l\'écart maximum par catégorie',
        'predictsetup': 'Configurer les canaux de prédiction',
        'gpredictload': 'Charger les jeux depuis les canaux de stats',
        'gpredict':     'Générer des prédictions par catégorie (N1 → N2)',
        'gtop':         'Top N prédictions les plus fiables pour les prochains jeux',
        'searchcard':   'Rechercher les jeux par valeur de carte (A, K, Q, J)',
        'documentation':'Guide complet avec exemples d\'utilisation',
        'recherche':    'Recherche SpécialeB — par canal, date et mot-clé (public)',
    }

    def _back_keyboard(self):
        """Bouton de retour au menu principal."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Menu principal", callback_data="menu:accueil")]
        ])

    async def handle_menu_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gestion des boutons inline du menu."""
        query = update.callback_query
        uid = query.from_user.id
        if not is_admin(uid):
            await query.answer("❌ Accès refusé.")
            return
        await query.answer()

        data = query.data  # ex: "menu:recherche"
        section = data.split(":", 1)[1] if ":" in data else ""

        # Vider les états d'attente lors de la navigation
        _clear_waits(uid)

        if section == "accueil":
            main = is_main_admin(uid)
            channels = get_channels()
            ch_lines = []
            for ch in channels:
                mark = "▶️" if ch.get('active') else "○"
                name = ch.get('name') or str(ch['id'])
                ch_lines.append(f"  {mark} <b>{name}</b>")
            ch_block = ("\n".join(ch_lines)) if ch_lines else "  <i>Aucun canal — tapez /addchannel</i>"
            text = (
                "🎯 <b>Bot VIP KOUAMÉ &amp; JOKER</b>\n\n"
                f"📡 <b>Canaux :</b>\n{ch_block}\n\n"
                "Choisissez une section :"
            )
            await query.edit_message_text(text, parse_mode='HTML',
                                          reply_markup=_main_menu_keyboard(main))
            return

        if section == "admin" and not is_main_admin(uid):
            await query.answer("❌ Réservé à l'administrateur principal.")
            return

        if section not in _MENU_SECTIONS:
            await query.answer("Section inconnue.")
            return

        text = _MENU_SECTIONS[section]
        await query.edit_message_text(text, parse_mode='HTML',
                                      reply_markup=self._back_keyboard())

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/menu — Affiche le menu principal avec les sections de commandes."""
        uid = update.effective_user.id
        if not is_admin(uid):
            return
        main = is_main_admin(uid)
        channels = get_channels()
        ch_lines = []
        for ch in channels:
            mark = "▶️" if ch.get('active') else "○"
            name = ch.get('name') or str(ch['id'])
            ch_lines.append(f"  {mark} <b>{name}</b>")
        ch_block = ("\n".join(ch_lines)) if ch_lines else "  <i>Aucun canal — tapez /addchannel</i>"
        text = (
            "🎯 <b>Bot VIP KOUAMÉ &amp; JOKER</b>\n\n"
            f"📡 <b>Canaux :</b>\n{ch_block}\n\n"
            "Choisissez une section :"
        )
        await update.message.reply_text(text, parse_mode='HTML',
                                        reply_markup=_main_menu_keyboard(main))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id

        # ── Utilisateur non-admin : message d'accueil public ──────────────────
        if not is_admin(uid):
            first_name = update.effective_user.first_name or 'cher utilisateur'
            kb = ReplyKeyboardMarkup(
                [[KeyboardButton("🔎 Recherche SpécialeB — /recherche")]],
                resize_keyboard=True,
                one_time_keyboard=False,
            )
            await update.message.reply_text(
                f"👋 Bonjour <b>{html.escape(first_name)}</b> !\n\n"
                "🎯 <b>Bot VIP KOUAMÉ — Analyse Baccarat</b>\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔎 <b>RECHERCHE SPÉCIALE B</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "<b>/recherche</b> — Rechercher des jeux dans un canal\n\n"
                "  1️⃣ Choisissez le canal\n"
                "  2️⃣ Entrez la date  <code>10/03/2026</code>\n"
                "  3️⃣ Entrez un mot-clé  <code>joueur</code> / <code>banquier</code> / <code>♠</code>\n"
                "  4️⃣ Recevez les numéros + costumes (aperçu + fichier)\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "💡 Appuyez sur le bouton ci-dessous ou tapez /recherche",
                parse_mode='HTML',
                reply_markup=kb,
            )
            return

        main = is_main_admin(uid)

        # ── Sous-admin : afficher ses commandes autorisées avec menu ──
        if not main:
            perms = get_admin_permissions(uid)
            first_name = update.effective_user.first_name or 'Admin'
            if not perms:
                await update.message.reply_text(
                    f"👋 Bonjour <b>{first_name}</b> !\n\n"
                    "❌ Aucune commande n'a encore été accordée à votre compte.\n\n"
                    "Contactez l'administrateur principal pour obtenir vos accès.",
                    parse_mode='HTML'
                )
                return
            lines = []
            for cmd in perms:
                desc = self._CMD_DESC.get(cmd, '')
                lines.append(f"  /{cmd} — {desc}" if desc else f"  /{cmd}")
            cmds_text = '\n'.join(lines)
            await update.message.reply_text(
                f"👋 Bonjour <b>{first_name}</b> !\n\n"
                "🎯 <b>Bot VIP KOUAMÉ &amp; JOKER</b>\n\n"
                "📋 <b>Vos commandes :</b>\n\n"
                f"{cmds_text}\n\n"
                "💡 Tapez /documentation pour les exemples détaillés.",
                parse_mode='HTML',
                reply_markup=_main_menu_keyboard(is_main=False)
            )
            return

        # ── Administrateur principal : tableau de bord avec menu ──
        channels = get_channels()
        if channels:
            ch_lines = []
            for ch in channels:
                mark = "▶️" if ch.get('active') else "○"
                name = ch.get('name') or str(ch['id'])
                added = ch.get('added_at', '')
                date_str = f" <i>({added[:10]})</i>" if added else ''
                ch_lines.append(f"  {mark} <b>{name}</b> <code>{ch['id']}</code>{date_str}")
            ch_block = "\n".join(ch_lines)
            await update.message.reply_text(
                "🎯 <b>Bot VIP KOUAMÉ &amp; JOKER</b>\n\n"
                f"📡 <b>Canaux configurés :</b>\n{ch_block}\n\n"
                "Choisissez une section :",
                parse_mode='HTML',
                reply_markup=_main_menu_keyboard(is_main=True)
            )
        else:
            await update.message.reply_text(
                "🎯 <b>Bot VIP KOUAMÉ &amp; JOKER</b>\n\n"
                "📡 <b>Aucun canal configuré.</b>\n\n"
                "Pour commencer :\n"
                "  /addchannel — Ajouter un canal Telegram\n\n"
                "Ou envoyez directement l'ID du canal (ex : <code>-1001234567890</code>)",
                parse_mode='HTML',
                reply_markup=_main_menu_keyboard(is_main=True)
            )
            _waiting_for_channel[uid] = True
    
    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/help — Liste toutes les commandes par domaine."""
        uid = update.effective_user.id
        if not is_admin(uid):
            return

        main = is_main_admin(uid)

        # Pour un sous-admin : afficher uniquement ses commandes autorisées avec descriptions
        if not main:
            perms = get_admin_permissions(uid)
            if not perms:
                await update.message.reply_text(
                    "❌ <b>Aucune commande accordée.</b>\n\n"
                    "Contactez l'administrateur principal pour obtenir des accès.",
                    parse_mode='HTML'
                )
                return
            lines = []
            for cmd in perms:
                desc = self._CMD_DESC.get(cmd, '')
                lines.append(f"  /{cmd} — {desc}" if desc else f"  /{cmd}")
            cmd_lines = '\n'.join(lines)
            await update.message.reply_text(
                f"📖 <b>VOS COMMANDES AUTORISÉES</b>\n\n"
                f"{cmd_lines}\n\n"
                f"💡 Tapez /documentation pour voir les exemples d'utilisation.\n"
                f"<i>Vos accès sont gérés par l'administrateur principal.</i>",
                parse_mode='HTML'
            )
            return

        sections = []

        sections.append(
            "📋 <b>GÉNÉRAL</b>\n"
            "  /start — Statut du bot et canaux actifs\n"
            "  /help — Cette liste de commandes\n"
            "  /documentation — Guide complet avec exemples\n"
            "  /myid — Afficher votre Telegram ID\n"
            "  /cancel — Annuler toute opération en cours"
        )

        if main:
            sections.append(
                "🔐 <b>CONNEXION TELEGRAM</b>\n"
                "  /connect — Demander le code SMS d'authentification\n"
                "  /code aa12345 — Valider le code reçu par SMS\n"
                "  /disconnect — Supprimer la session active"
            )

        sections.append(
            "💾 <b>DONNÉES LOCALES</b>\n"
            "  /sync — Récupérer les messages récents du canal principal\n"
            "  /fullsync — Récupérer tout l'historique du canal principal\n"
            "  /stats — Statistiques des prédictions stockées\n"
            "  /report — Générer un PDF de toutes les prédictions\n"
            "  /search mot1 mot2 — Chercher et exporter en PDF\n"
            "  /filter — Filtrer par couleur ou statut\n"
            "  /clear — Effacer toutes les données locales\n"
            "  📎 <i>Envoyer un fichier PDF → analyse automatique des numéros</i>"
        )

        sections.append(
            "📡 <b>GESTION DES CANAUX</b>\n"
            "  /helpcl — Sélectionner le canal actif (menu numéroté)\n"
            "  /addchannel — Ajouter un nouveau canal à la liste\n"
            "  /channels — Voir tous les canaux configurés\n"
            "  /usechannel -100XXX — Activer un canal directement par ID\n"
            "  /removechannel -100XXX — Supprimer un canal\n"
            "  /hsearch mots-clés — Chercher dans l'historique du canal actif\n"
            "    ↳ Options : <code>limit:500</code>  <code>from:2024-06-01</code>"
        )

        sections.append(
            "📊 <b>ANALYSE BACCARAT</b>\n"
            "  /gload <code>from:AAAA-MM-JJ</code> — Charger jeux à partir d'une date\n"
            "  /gload <code>limit:N</code> — Charger les N derniers jeux\n"
            "  /gstats — Statistiques des jeux chargés\n"
            "  /ganalyze — Analyser un enregistrement (copier-coller)\n"
            "  /gclear — Effacer les jeux analysés\n\n"
            "  <b>Catégories :</b>\n"
            "  /gvictoire joueur|banquier|nul — Écarts par résultat\n"
            "  /gparite pair|impair — Écarts par parité du total\n"
            "  /gstructure 2/2|2/3|3/2|3/3 — Structure des cartes\n"
            "  /gplusmoins j|b plus|moins — Plus/Moins de 6,5 ou 4,5\n"
            "  /gcostume ♠|♥|♦|♣ j|b — Costume manquant par main\n"
            "  /gvaleur A|K|Q|J j|b — Valeurs spéciales par costume\n"
            "  /gecartmax — Paires avec l'écart maximum (toutes catégories)"
        )

        sections.append(
            "🔄 <b>CORRECTION DE CYCLES DE COSTUMES</b>\n"
            "  /gcycle pair|impair [j|b] [N1-N2] — Tester un cycle prédéfini\n"
            "  /gcycleauto [j|b] [N1-N2] — Trouver le meilleur cycle auto\n\n"
            "  <i>Génère la liste complète numéro [costume] corrigé</i>"
        )

        if main:
            sections.append(
                "👥 <b>ADMINISTRATION</b>\n"
                "  /addadmin USER_ID — Ajouter un admin (menu de sélection des commandes)\n"
                "  /setperm USER_ID — Modifier les permissions d'un admin existant\n"
                "  /removeadmin USER_ID — Supprimer un administrateur\n"
                "  /admins — Voir la liste des admins et leurs permissions"
            )

        header = "📖 <b>AIDE — COMMANDES DU BOT VIP KOUAMÉ</b>\n\n"
        footer = "\n\n💡 <i>/documentation pour des exemples détaillés · /cancel pour annuler</i>"
        full_text = header + "\n\n".join(sections) + footer
        await update.message.reply_text(full_text, parse_mode='HTML')

    async def documentation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/documentation — Génère et envoie un PDF complet de documentation."""
        uid = update.effective_user.id
        if not is_admin(uid):
            return

        main = is_main_admin(uid)
        msg = await update.message.reply_text("📚 Génération du guide PDF en cours…")

        try:
            from pdf_generator import generate_documentation_pdf
            pdf_path = generate_documentation_pdf(is_main_admin=main)

            await update.message.reply_document(
                document=open(pdf_path, 'rb'),
                filename="Documentation_VIP_Kouame.pdf",
                caption="📚 <b>Documentation complète</b> — toutes les commandes avec exemples détaillés",
                parse_mode='HTML'
            )
            await msg.delete()
            import os
            os.remove(pdf_path)
        except Exception as e:
            import html as _html
            await msg.edit_text(f"❌ Erreur lors de la génération : {_html.escape(str(e))}", parse_mode='HTML')

    async def connect(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/connect - Envoie le code SMS (supprime l'ancienne session si elle existe)"""
        if not is_admin(update.effective_user.id):
            return

        msg = await update.message.reply_text(f"📲 Envoi du code à {USER_PHONE}...")

        try:
            success, result = await auth_manager.send_code()
            await msg.edit_text(result, parse_mode='Markdown')
        except Exception as e:
            await msg.edit_text(f"❌ Erreur: {str(e)}")
    
    async def code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/code XXXXXX — Entrer le code reçu par SMS"""
        if not is_admin(update.effective_user.id):
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: `/code aaXXXXXX`\nExemple: `/code aa43481`\n\nAjoutez `aa` avant les chiffres du code reçu.",
                parse_mode='Markdown'
            )
            return

        code = context.args[0]
        msg = await update.message.reply_text("🔐 Vérification du code...")

        try:
            success, result = await auth_manager.verify_code(code)
            await msg.edit_text(result, parse_mode='Markdown')
        except Exception as e:
            await msg.edit_text(f"❌ Erreur: {str(e)}")

    async def disconnect(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/disconnect — Efface la session Telethon"""
        if not is_admin(update.effective_user.id):
            return

        msg = await update.message.reply_text("🔌 Déconnexion...")
        try:
            await auth_manager.reset()
            await msg.edit_text("✅ Session supprimée. Tapez /connect pour vous reconnecter.")
        except Exception as e:
            await msg.edit_text(f"❌ Erreur: {str(e)}")
    
    async def sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._perm(update, 'sync'):
            return

        if not auth_manager.is_connected():
            await update.message.reply_text("❌ Tapez /connect puis /code d'abord")
            return

        if self.syncing:
            await update.message.reply_text("⏳ Synchronisation déjà en cours, patientez...")
            return

        self.syncing = True
        msg = await update.message.reply_text("🔄 Synchronisation lancée en arrière-plan...")

        async def _do_sync():
            try:
                async def progress(n):
                    if n % 500 == 0:
                        try:
                            await msg.edit_text(f"📥 {n} messages parcourus...")
                        except Exception:
                            pass

                result = await scraper.sync(full=False, progress_callback=progress)
                await msg.edit_text(f"✅ **{result['new']}** nouvelles prédictions ajoutées !", parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Sync error: {e}")
                try:
                    if _is_auth_key_dup(e):
                        await msg.edit_text(_AUTH_KEY_DUP_MSG, parse_mode='HTML')
                    else:
                        await msg.edit_text(f"❌ Erreur: {str(e)[:300]}")
                except Exception:
                    pass
            finally:
                self.syncing = False

        context.application.create_task(_do_sync())

    async def fullsync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._perm(update, 'fullsync'):
            return

        if not auth_manager.is_connected():
            await update.message.reply_text("❌ Non connecté")
            return

        if self.syncing:
            await update.message.reply_text("⏳ Synchronisation déjà en cours, patientez...")
            return

        self.syncing = True
        msg = await update.message.reply_text(
            "🔄 Synchronisation complète lancée en arrière-plan...\n"
            "Le bot reste utilisable. Vous recevrez un message à la fin."
        )

        async def _do_fullsync():
            try:
                async def progress(n):
                    if n % 1000 == 0 and n > 0:
                        try:
                            await msg.edit_text(f"📥 {n} messages parcourus en cours...")
                        except Exception:
                            pass

                result = await scraper.sync(full=True, progress_callback=progress)
                await msg.edit_text(f"✅ **{result['new']}** prédictions récupérées !", parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Fullsync error: {e}")
                try:
                    if _is_auth_key_dup(e):
                        await msg.edit_text(_AUTH_KEY_DUP_MSG, parse_mode='HTML')
                    else:
                        await msg.edit_text(f"❌ Erreur: {str(e)[:300]}")
                except Exception:
                    pass
            finally:
                self.syncing = False

        context.application.create_task(_do_fullsync())
    
    async def report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._perm(update, 'report'):
            return
        
        predictions = get_predictions(context.user_data.get('filters'))
        if not predictions:
            await update.message.reply_text("❌ Aucune donnée. Faites /fullsync d'abord")
            return
        
        requester_id = update.effective_chat.id
        msg = await update.message.reply_text("📄 Génération PDF...")
        
        try:
            pdf_path = generate_pdf(predictions, context.user_data.get('filters'))
            
            with open(pdf_path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=requester_id,
                    document=f,
                    caption=f"✅ Rapport: {len(predictions)} prédictions"
                )
            
            os.remove(pdf_path)
            await msg.delete()
        except Exception as e:
            await msg.edit_text(f"❌ Erreur: {str(e)}")
    
    async def filter_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._perm(update, 'filter'):
            return
        
        if not context.args:
            context.user_data['filters'] = {}
            await update.message.reply_text("✅ Filtres réinitialisés")
            return
        
        filters = {'couleur': context.args[0]}
        if len(context.args) > 1:
            filters['statut'] = ' '.join(context.args[1:])
        
        context.user_data['filters'] = filters
        await update.message.reply_text(f"✅ Filtre: {filters}")
    
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._perm(update, 'stats'):
            return
        
        s = get_stats()
        preds = get_predictions()
        gagnes = sum(1 for p in preds if 'gagn' in p['statut'].lower())
        
        await update.message.reply_text(
            f"📊 Stats\n"
            f"• Total: {s['total']}\n"
            f"• Gagnés: {gagnes}\n"
            f"• Taux: {round(gagnes/s['total']*100,1)}%" if s['total'] else "N/A"
        )
    
    async def search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/search <mots-clés> — Recherche dans les messages et génère un PDF"""
        if not await self._perm(update, 'search'):
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: `/search mot1 mot2 ...`\n"
                "Ex: `/search rouge gagné`\n\n"
                "Recherche tous les messages contenant tous ces mots.",
                parse_mode='Markdown'
            )
            return

        keywords = list(context.args)
        bot = context.bot
        requester_id = update.effective_chat.id
        msg = await update.message.reply_text(
            f"🔍 Recherche `{' '.join(keywords)}` lancée en arrière-plan...\n"
            "Le bot reste utilisable. Vous recevrez le PDF à la fin.",
            parse_mode='Markdown'
        )

        async def _do_search():
            # 1. Recherche dans le canal Telegram si connecté
            if auth_manager.is_connected():
                try:
                    async def progress(checked, found):
                        if checked % 500 == 0:
                            try:
                                await msg.edit_text(f"🔍 {checked} messages vérifiés... ({found} trouvés)")
                            except Exception:
                                pass

                    results = await scraper.search_in_channel(keywords, progress_callback=progress)

                    if results:
                        try:
                            await msg.edit_text(f"📄 {len(results)} résultat(s). Génération du PDF...")
                        except Exception:
                            pass
                        pdf_path = generate_channel_search_pdf(results, keywords)
                        with open(pdf_path, 'rb') as f:
                            await bot.send_document(
                                chat_id=requester_id,
                                document=f,
                                caption=f"🔍 Recherche: {' '.join(keywords)}\n✅ {len(results)} message(s) trouvé(s)"
                            )
                        os.remove(pdf_path)
                        try:
                            await msg.delete()
                        except Exception:
                            pass
                    else:
                        await msg.edit_text(
                            f"❌ Aucun message trouvé pour: `{' '.join(keywords)}`",
                            parse_mode='Markdown'
                        )
                    return

                except Exception as e:
                    logger.error(f"Search canal error: {e}")
                    try:
                        if _is_auth_key_dup(e):
                            await msg.edit_text(_AUTH_KEY_DUP_MSG, parse_mode='HTML')
                            return
                        await msg.edit_text(f"⚠️ Erreur canal: {str(e)[:200]}\nRecherche dans les données locales...")
                    except Exception:
                        pass

            # 2. Fallback: recherche dans les données locales
            results = search_predictions(keywords)
            if not results:
                try:
                    await msg.edit_text(
                        f"❌ Aucun résultat pour: `{' '.join(keywords)}`\n\n"
                        "Connectez-vous avec /connect + /code puis /fullsync pour accéder à l'historique complet.",
                        parse_mode='Markdown'
                    )
                except Exception:
                    pass
                return

            try:
                await msg.edit_text(f"📄 {len(results)} résultat(s) local/locaux. Génération du PDF...")
            except Exception:
                pass
            try:
                pdf_path = generate_search_pdf(results, keywords)
                with open(pdf_path, 'rb') as f:
                    await bot.send_document(
                        chat_id=requester_id,
                        document=f,
                        caption=f"🔍 Recherche: {' '.join(keywords)}\n✅ {len(results)} message(s) trouvé(s)"
                    )
                os.remove(pdf_path)
                try:
                    await msg.delete()
                except Exception:
                    pass
            except Exception as e:
                try:
                    await msg.edit_text(f"❌ Erreur PDF: {str(e)}")
                except Exception:
                    pass

        context.application.create_task(_do_search())

    async def searchcard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/searchcard [A|K|Q|J] [joueur|banquier|tous] — Recherche par valeur de carte."""
        if not await self._perm(update, 'searchcard'):
            return

        from game_analyzer import FACE_CARDS
        from storage import get_analyzed_games

        USAGE = (
            "📋 <b>Usage de /searchcard</b>\n\n"
            "<code>/searchcard K</code> — Tous les jeux où K apparaît\n"
            "<code>/searchcard K joueur</code> — K dans la main du Joueur\n"
            "<code>/searchcard A banquier</code> — A dans la main du Banquier\n"
            "<code>/searchcard K Q joueur</code> — K ou Q côté Joueur\n"
            "<code>/searchcard K from:2026-02-20 to:2026-02-23</code> — sur une plage de dates\n\n"
            "Valeurs acceptées : <b>A, K, Q, J</b>\n"
            "Côtés : <b>joueur</b>, <b>banquier</b>, <b>tous</b> (défaut)"
        )

        if not context.args:
            await update.message.reply_text(USAGE, parse_mode='HTML')
            return

        games = get_analyzed_games()
        if not games:
            await update.message.reply_text(
                "❌ Aucun jeu chargé. Tapez /gpredictload d'abord."
            )
            return

        # Extraire les options de date + mots restants
        remaining_kw, _, from_date_sc, to_date_sc = parse_search_options(list(context.args))
        games = _filter_games_by_date(games, from_date_sc, to_date_sc)

        # Parser les arguments : valeurs de cartes + côté optionnel
        args = [a.upper() for a in remaining_kw]

        side = 'tous'
        valeurs = []
        for arg in args:
            if arg in ('JOUEUR',):
                side = 'joueur'
            elif arg in ('BANQUIER',):
                side = 'banquier'
            elif arg in ('TOUS',):
                side = 'tous'
            elif arg in FACE_CARDS:
                valeurs.append(arg)

        if not valeurs:
            await update.message.reply_text(
                "❌ Aucune valeur valide. Utilisez A, K, Q ou J.\n\n" + USAGE,
                parse_mode='HTML'
            )
            return

        # Recherche dans les jeux
        matching = []
        for g in games:
            face_j = g.get('face_j', set())
            face_b = g.get('face_b', set())
            found = False
            for val in valeurs:
                if side == 'joueur' and val in face_j:
                    found = True
                elif side == 'banquier' and val in face_b:
                    found = True
                elif side == 'tous' and (val in face_j or val in face_b):
                    found = True
            if found:
                matching.append(g)

        if not matching:
            side_label = {'joueur': 'Joueur', 'banquier': 'Banquier', 'tous': 'Joueur ou Banquier'}[side]
            await update.message.reply_text(
                f"❌ Aucun jeu trouvé avec <b>{'/ '.join(valeurs)}</b> côté <b>{side_label}</b>.",
                parse_mode='HTML'
            )
            return

        # Statistiques d'écart
        nums = sorted(int(g['numero']) for g in matching)
        total_games = len(games)
        pct = round(len(nums) / total_games * 100, 1)
        ecarts = [nums[i+1] - nums[i] for i in range(len(nums)-1)] if len(nums) >= 2 else []
        avg_ecart = round(sum(ecarts) / len(ecarts), 1) if ecarts else 0
        max_ecart = max(ecarts) if ecarts else 0
        last_num = nums[-1]
        current_ecart = max(int(g['numero']) for g in games) - last_num

        side_label = {'joueur': '🃏 Joueur', 'banquier': '🏦 Banquier', 'tous': '🃏 Joueur + 🏦 Banquier'}[side]
        val_str = ' / '.join(valeurs)

        # En-tête
        header = (
            f"🔍 <b>Recherche cartes : {val_str}</b>\n"
            f"📌 Côté : {side_label}\n"
            f"📊 Basé sur {total_games} jeux\n\n"
            f"✅ <b>{len(nums)}</b> occurrences ({pct}% des jeux)\n"
            f"📐 Écart moyen : <b>{avg_ecart}</b> | Max : <b>{max_ecart}</b>\n"
            f"⏱ Écart actuel depuis #N{last_num} : <b>{current_ecart}</b>\n"
        )

        await update.message.reply_text(header, parse_mode='HTML')

        # Liste des numéros par bloc de 50 lignes max
        lines = [f"#{n}" for n in nums]
        chunk_size = 50
        for i in range(0, len(lines), chunk_size):
            chunk = lines[i:i + chunk_size]
            col1 = chunk[:len(chunk)//2 + len(chunk)%2]
            col2 = chunk[len(chunk)//2 + len(chunk)%2:]
            rows = []
            for a, b in zip(col1, col2):
                rows.append(f"{a:<12}{b}")
            if len(col1) > len(col2):
                rows.append(f"{col1[-1]}")
            block = '\n'.join(rows)
            await update.message.reply_text(
                f"<code>{block}</code>",
                parse_mode='HTML'
            )

    async def handle_pdf(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reçoit un PDF, l'analyse et renvoie la liste des numéros/costumes uniques."""
        if not is_admin(update.effective_user.id):
            return

        doc = update.message.document
        if not doc or doc.mime_type != 'application/pdf':
            return

        msg = await update.message.reply_text("📥 PDF reçu. Analyse en cours...")

        import re as _re
        caption_raw = (update.message.caption or '').strip()
        threshold = 4
        min_match = _re.search(r'(?:min[:\s]?)?(\d+)', caption_raw, _re.IGNORECASE)
        if min_match:
            threshold = max(1, min(int(min_match.group(1)), 500))
        source_name = doc.file_name or 'fichier.pdf'

        async def _do_analyze():
            tmp_path = f"/tmp/analyse_{doc.file_id}.pdf"
            pdf_out = None
            try:
                file = await context.bot.get_file(doc.file_id)
                await file.download_to_drive(tmp_path)

                await msg.edit_text(f"🔍 Extraction en cours… (seuil : ≥{threshold} occurrences)")

                results, raw_sample = analyze_pdf(tmp_path)

                if not results:
                    await msg.edit_text(
                        "❌ Aucun numéro prédit trouvé dans ce PDF.\n\n"
                        "Assurez-vous que le PDF contient des prédictions au format:\n"
                        "`PRÉDICTION #X` et `Couleur: Y`",
                        parse_mode='Markdown'
                    )
                    return

                total_extracted = len(results)
                filtered = [r for r in results if r.get('count', 1) >= threshold]

                await msg.edit_text(
                    f"📊 {total_extracted} numéros extraits  |  "
                    f"{len(filtered)} avec ≥{threshold} occurrence(s)\n"
                    "📄 Génération du PDF…"
                )

                pdf_out = generate_costume_pdf(filtered, threshold, source_name)

                caption_pdf = (
                    f"Joueur 😉😌\n"
                    f"Seuil : ≥{threshold} occurrences  |  {len(filtered)} numéros"
                )

                with open(pdf_out, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=f,
                        caption=caption_pdf,
                        filename=f"costumes_seuil{threshold}.pdf"
                    )
                await msg.delete()

            except Exception as e:
                logger.error(f"PDF analyze error: {e}")
                try:
                    await msg.edit_text(f"❌ Erreur lors de l'analyse: {str(e)[:300]}")
                except Exception:
                    pass
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                if pdf_out and os.path.exists(pdf_out):
                    os.remove(pdf_out)

        context.application.create_task(_do_analyze())

    async def addchannel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/addchannel — Demande l'ID d'un canal à ajouter."""
        if not is_main_admin(update.effective_user.id):
            await update.message.reply_text("❌ Réservé à l'administrateur principal.")
            return
        _waiting_for_channel[update.effective_user.id] = True
        await update.message.reply_text(
            "📡 Envoyez l'ID du canal à ajouter.\n\n"
            "Format attendu : `-1001234567890`\n"
            "Vous pouvez aussi envoyer le @username du canal public.\n\n"
            "_(Tapez /cancel pour annuler)_",
            parse_mode='Markdown'
        )

    async def channels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/channels — Liste les canaux et permet d'en choisir un."""
        if not await self._perm(update, 'channels'):
            return
        channels = get_channels()
        if not channels:
            await update.message.reply_text(
                "Aucun canal configuré. Tapez /addchannel pour en ajouter un."
            )
            return

        lines = ["📡 <b>Canaux de recherche enregistrés :</b>\n"]
        for ch in channels:
            mark = "▶️ <b>ACTIF</b>" if ch.get('active') else "⬜"
            name = ch.get('name') or ch['id']
            lines.append(f"{mark} {html.escape(str(name))} — <code>{ch['id']}</code>")

        lines.append("\n<b>Pour changer de canal actif :</b>")
        lines.append("<code>/usechannel ID</code>  ex: /usechannel -1001234567890")
        lines.append("<code>/removechannel ID</code>  pour supprimer")

        await update.message.reply_text('\n'.join(lines), parse_mode='HTML')

    async def usechannel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/usechannel <id> — Définit le canal actif pour les recherches."""
        if not await self._perm(update, 'usechannel'):
            return
        if not context.args:
            await update.message.reply_text("Usage: `/usechannel -1001234567890`", parse_mode='Markdown')
            return
        channel_id = context.args[0].strip()
        channels = get_channels()
        if not any(ch['id'] == channel_id for ch in channels):
            await update.message.reply_text(f"❌ Canal `{channel_id}` non trouvé. Tapez /channels pour voir la liste.", parse_mode='Markdown')
            return
        set_active_channel(channel_id)
        active = get_active_channel()
        name = active.get('name') or channel_id
        await update.message.reply_text(
            f"✅ Canal actif : <b>{html.escape(str(name))}</b> (<code>{channel_id}</code>)",
            parse_mode='HTML'
        )

    async def helpcl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/helpcl — Menu interactif de sélection du canal actif pour les analyses."""
        if not await self._perm(update, 'helpcl'):
            return
        channels = get_channels()
        if not channels:
            await update.message.reply_text(
                "❌ Aucun canal configuré.\nUtilisez /addchannel pour en ajouter un."
            )
            return
        _waiting_for_helpcl[update.effective_user.id] = True
        await update.message.reply_text(_build_channel_menu(channels), parse_mode='HTML')

    async def handle_helpcl_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reçoit le choix du canal dans le menu /helpcl."""
        uid = update.effective_user.id
        if not _waiting_for_helpcl.get(uid):
            return

        text = update.message.text.strip().lower()

        if text in ('sortir', 'exit', 'quitter', '/cancel', 'cancel', 'annuler'):
            _waiting_for_helpcl.pop(uid, None)
            await update.message.reply_text("↩️ Sélection annulée. Canal inchangé.")
            return

        channels = get_channels()
        if not text.isdigit() or not (1 <= int(text) <= len(channels)):
            await update.message.reply_text(
                f"❌ Tapez un numéro entre <b>1</b> et <b>{len(channels)}</b>, "
                f"ou <b>sortir</b> pour annuler.",
                parse_mode='HTML'
            )
            return

        idx = int(text) - 1
        chosen = channels[idx]
        set_active_channel(chosen['id'])
        _waiting_for_helpcl.pop(uid, None)
        name = chosen.get('name') or chosen['id']

        # Proposer des commandes adaptées selon le profil
        if is_main_admin(uid):
            next_cmds = (
                "📌 <b>Que faire ensuite ?</b>\n\n"
                "  /sync — Récupérer les messages récents\n"
                "  /fullsync — Récupérer tout l'historique\n"
                "  /gload — Charger les jeux Baccarat\n"
                "  /hsearch — Chercher dans l'historique\n"
                "  /addchannel — Ajouter un autre canal\n"
                "  /help — Voir toutes les commandes"
            )
        else:
            perms = get_admin_permissions(uid)
            suggestions = [c for c in ('sync', 'fullsync', 'gload', 'hsearch', 'gstats') if c in perms]
            lines = '\n'.join(f"  /{c} — {self._CMD_DESC.get(c, '')}" for c in suggestions)
            next_cmds = (
                f"📌 <b>Vos prochaines commandes :</b>\n\n{lines}"
                if lines else "💡 Tapez /help pour voir vos commandes."
            )

        await update.message.reply_text(
            f"✅ <b>Canal actif sélectionné :</b>\n\n"
            f"<b>{html.escape(name)}</b>\n"
            f"<code>{chosen['id']}</code>\n\n"
            f"Toutes les analyses utiliseront ce canal.\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{next_cmds}",
            parse_mode='HTML'
        )

    async def removechannel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/removechannel <id> — Supprime un canal de la liste."""
        if not is_main_admin(update.effective_user.id):
            await update.message.reply_text("❌ Réservé à l'administrateur principal.")
            return
        if not context.args:
            await update.message.reply_text("Usage: `/removechannel -1001234567890`", parse_mode='Markdown')
            return
        channel_id = context.args[0].strip()
        remove_channel(channel_id)
        await update.message.reply_text(f"🗑️ Canal `{channel_id}` supprimé.", parse_mode='Markdown')

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/cancel — Annule la recherche en cours et affiche les résultats partiels."""
        uid = update.effective_user.id

        # Annuler une recherche en cours
        if uid in _search_cancel:
            _search_cancel[uid] = True
            await update.message.reply_text(
                "🛑 Annulation demandée...\n"
                "⏳ Attends quelques secondes, les résultats partiels vont s'afficher."
            )
            return

        # Annuler une saisie de canal en attente
        if _waiting_for_channel.pop(uid, None):
            await update.message.reply_text("❌ Saisie de canal annulée.")
            return

        # Annuler une saisie de jeu en attente
        if _waiting_for_game.pop(uid, None):
            await update.message.reply_text("❌ Analyse annulée.")
            return

        # Annuler une saisie de permissions en attente
        if _waiting_for_perm.pop(uid, None):
            await update.message.reply_text("❌ Saisie de permissions annulée.")
            return

        # Annuler le menu helpcl
        if _waiting_for_helpcl.pop(uid, None):
            await update.message.reply_text("❌ Sélection de canal annulée.")
            return

        # Annuler la configuration predict
        if _waiting_for_predict.pop(uid, None):
            await update.message.reply_text("❌ Configuration de prédiction annulée.")
            return

        await update.message.reply_text("ℹ️ Aucune opération en cours à annuler.")

    async def hsearch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/hsearch mot1 mot2 [limit:N] [from:DATE] — Recherche dans l'historique du canal actif."""
        if not await self._perm(update, 'hsearch'):
            return

        active = get_active_channel()
        if not active:
            await update.message.reply_text(
                "❌ Aucun canal actif. Tapez /addchannel pour en ajouter un."
            )
            return

        if not context.args:
            await update.message.reply_text(
                "Usage: `/hsearch mot1 mot2 [limit:N] [from:DATE] [to:DATE]`\n\n"
                "Exemples :\n"
                "`/hsearch GAGNÉ Cœur`\n"
                "`/hsearch GAGNÉ from:2026-02-20 to:2026-02-23`\n"
                "`/hsearch GAGNÉ from:2026-02-20 10:00 to:2026-02-23 23:59`\n"
                "`/hsearch GAGNÉ limit:500`\n\n"
                "Tapez /cancel pour arrêter et voir les résultats partiels.",
                parse_mode='Markdown'
            )
            return

        keywords, limit, from_date, to_date = parse_search_options(list(context.args))
        if not keywords:
            await update.message.reply_text("❌ Aucun mot-clé fourni.")
            return

        uid = update.effective_user.id
        if uid in _search_cancel:
            await update.message.reply_text("⚠️ Une recherche est déjà en cours. Tapez /cancel pour l'arrêter.")
            return

        channel_id = active['id']
        channel_name = active.get('name') or channel_id
        requester_id = update.effective_chat.id

        scope_desc = ''
        if limit:
            scope_desc = f" | 🔢 {limit} derniers messages"
        elif from_date and to_date:
            scope_desc = f" | 📅 {from_date.strftime('%d/%m/%Y %H:%M')} → {to_date.strftime('%d/%m/%Y %H:%M')}"
        elif from_date:
            scope_desc = f" | 📅 depuis {from_date.strftime('%d/%m/%Y %H:%M')}"
        elif to_date:
            scope_desc = f" | 📅 jusqu'au {to_date.strftime('%d/%m/%Y %H:%M')}"

        msg = await update.message.reply_text(
            f"🔍 Recherche `{' '.join(keywords)}` dans *{html.escape(str(channel_name))}*{scope_desc}\n"
            f"⏳ Tapez /cancel pour arrêter et voir les résultats partiels.",
            parse_mode='Markdown'
        )

        _search_cancel[uid] = False

        async def _do_hsearch():
            try:
                async def progress(checked, found):
                    try:
                        cancelled_hint = " | /cancel pour arrêter" if not _search_cancel.get(uid) else ""
                        await msg.edit_text(
                            f"🔍 Recherche dans *{html.escape(str(channel_name))}*...\n"
                            f"📨 {checked} messages analysés — {found} trouvés{scope_desc}{cancelled_hint}",
                            parse_mode='Markdown'
                        )
                    except Exception:
                        pass

                results, title, was_cancelled = await scraper.search_in_any_channel(
                    channel_id, keywords,
                    limit=limit,
                    from_date=from_date,
                    to_date=to_date,
                    progress_callback=progress,
                    cancel_check=lambda: _search_cancel.get(uid, False)
                )

                prefix = "🛑 Résultats partiels" if was_cancelled else "✅ Recherche terminée"

                if not results:
                    status = "annulée, aucun résultat trouvé" if was_cancelled else "aucun résultat"
                    await msg.edit_text(
                        f"🔍 Recherche {status} pour `{' '.join(keywords)}` dans *{html.escape(str(title))}*.",
                        parse_mode='Markdown'
                    )
                    return

                pdf_path = generate_channel_search_pdf(results, keywords, title)
                tag = " (partiel)" if was_cancelled else ""
                safe_caption = f"{prefix}{tag}: {' '.join(keywords)} | {len(results)} résultats | {title}"

                with open(pdf_path, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=requester_id,
                        document=f,
                        caption=safe_caption[:1024],
                        filename=f"hsearch_{len(results)}.pdf"
                    )
                os.remove(pdf_path)
                await msg.delete()

            except Exception as e:
                logger.error(f"hsearch error: {e}")
                try:
                    if _is_auth_key_dup(e):
                        await msg.edit_text(_AUTH_KEY_DUP_MSG, parse_mode='HTML')
                    else:
                        await msg.edit_text(f"❌ Erreur: {str(e)[:300]}")
                except Exception:
                    pass
            finally:
                _search_cancel.pop(uid, None)

        context.application.create_task(_do_hsearch())

    async def handle_channel_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reçoit un ID de canal quand le bot est en attente."""
        if not is_admin(update.effective_user.id):
            return
        if not _waiting_for_channel.get(update.effective_user.id):
            return

        text = update.message.text.strip()

        # Annulation
        if text.lower() in ('/cancel', 'cancel', 'annuler'):
            _waiting_for_channel.pop(update.effective_user.id, None)
            await update.message.reply_text("❌ Annulé.")
            return

        # Vérifier que c'est un ID valide ou un username
        if not (text.lstrip('-').isdigit() or text.startswith('@') or text.startswith('https://t.me/')):
            await update.message.reply_text(
                "❌ Format invalide. Envoyez un ID numérique (ex: `-1001234567890`) "
                "ou un username (ex: `@moncanal`).\n\nOu tapez /cancel pour annuler.",
                parse_mode='Markdown'
            )
            return

        msg = await update.message.reply_text(f"🔄 Vérification du canal <code>{html.escape(text)}</code>...", parse_mode='HTML')

        async def _do_add():
            try:
                # Tenter de résoudre le canal pour récupérer son nom
                from scraper import scraper as _sc
                _sc._make_client()
                await _sc.client.connect()

                try:
                    if text.lstrip('-').isdigit():
                        cid = int(text)
                    else:
                        cid = text

                    entity = await _sc.client.get_entity(cid)
                    channel_name = entity.title if hasattr(entity, 'title') else text
                    real_id = str(-1000000000000 - entity.id) if hasattr(entity, 'id') and not text.lstrip('-').isdigit() else text
                    # Utiliser l'ID que l'utilisateur a fourni si c'est déjà numérique
                    store_id = text if text.lstrip('-').isdigit() else str(entity.id)

                finally:
                    await _sc.client.disconnect()

                added = add_channel(store_id, channel_name)
                _waiting_for_channel.pop(update.effective_user.id, None)

                if added:
                    channels = get_channels()
                    is_first = len(channels) == 1
                    await msg.edit_text(
                        f"✅ Canal ajouté : *{html.escape(channel_name)}*\n"
                        f"ID: `{store_id}`\n\n"
                        f"{'▶️ Ce canal est maintenant actif pour /hsearch' if is_first else 'Utilisez /usechannel pour le sélectionner.'}",
                        parse_mode='Markdown'
                    )
                else:
                    await msg.edit_text(f"⚠️ Ce canal est déjà dans la liste.", parse_mode='Markdown')

            except Exception as e:
                _waiting_for_channel.pop(update.effective_user.id, None)
                if _is_auth_key_dup(e):
                    await msg.edit_text(_AUTH_KEY_DUP_MSG, parse_mode='HTML')
                else:
                    await msg.edit_text(
                        f"❌ Impossible d'accéder à ce canal : {str(e)[:200]}\n\n"
                        "Vérifiez que le compte Telegram est membre de ce canal.",
                        parse_mode='HTML'
                    )

        context.application.create_task(_do_add())

    # ── COMMANDES ANALYSE DE JEUX ─────────────────────────────────────────────

    async def ganalyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/ganalyze — Demande un enregistrement de jeu à analyser."""
        if not await self._perm(update, 'ganalyze'):
            return
        _waiting_for_game[update.effective_user.id] = True
        await update.message.reply_text(
            "🎴 Envoyez un enregistrement de jeu à analyser.\n\n"
            "Exemple :\n`#N794. ✅3(K♦️4♦️9♦️) - 1(J♦️10♥️A♠️) #T4`\n\n"
            "_(Tapez /cancel pour annuler)_",
            parse_mode='Markdown'
        )

    async def gload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gload from:DATE [HH:MM] | limit:N — Charge et analyse les jeux du canal actif."""
        if not await self._perm(update, 'gload'):
            return
        active = get_active_channel()
        if not active:
            await update.message.reply_text("❌ Aucun canal actif. Tapez /addchannel.")
            return

        uid = update.effective_user.id
        if uid in _search_cancel:
            await update.message.reply_text("⚠️ Une recherche est déjà en cours. Tapez /cancel pour l'arrêter.")
            return

        _, limit, from_date, to_date = parse_search_options(list(context.args)) if context.args else ([], None, None, None)

        if not limit and not from_date:
            await update.message.reply_text(
                "⚠️ <b>Paramètre requis</b>\n\n"
                "Vous devez préciser une date de début ou une limite.\n\n"
                "<b>Exemples :</b>\n"
                "<code>/gload from:2026-02-01</code>\n"
                "<code>/gload from:2026-02-20 to:2026-02-23</code>\n"
                "<code>/gload from:2026-02-20 10:00 to:2026-02-23 23:59</code>\n"
                "<code>/gload limit:500</code>",
                parse_mode='HTML'
            )
            return

        channel_id = active['id']
        channel_name = active.get('name') or channel_id

        scope_desc = ''
        if limit:
            scope_desc = f" | 🔢 {limit} derniers messages"
        elif from_date and to_date:
            scope_desc = f" | 📅 {from_date.strftime('%d/%m/%Y %H:%M')} → {to_date.strftime('%d/%m/%Y %H:%M')}"
        elif from_date:
            scope_desc = f" | 📅 depuis {from_date.strftime('%d/%m/%Y %H:%M')}"
        elif to_date:
            scope_desc = f" | 📅 jusqu'au {to_date.strftime('%d/%m/%Y %H:%M')}"

        msg = await update.message.reply_text(
            f"🔄 Chargement des jeux depuis *{html.escape(str(channel_name))}*{scope_desc}\n"
            f"⏳ Tapez /cancel pour arrêter et sauvegarder les jeux trouvés.",
            parse_mode='Markdown'
        )

        _search_cancel[uid] = False

        async def _do_gload():
            try:
                async def progress(checked, found):
                    try:
                        await msg.edit_text(
                            f"🔄 Analyse *{html.escape(str(channel_name))}*...\n"
                            f"📨 {checked} messages vus — {found} jeux trouvés{scope_desc}\n"
                            f"Tapez /cancel pour arrêter.",
                            parse_mode='Markdown'
                        )
                    except Exception:
                        pass

                records, title, was_cancelled = await scraper.get_game_records(
                    channel_id,
                    limit=limit,
                    from_date=from_date,
                    to_date=to_date,
                    progress_callback=progress,
                    cancel_check=lambda: _search_cancel.get(uid, False)
                )

                if not records:
                    await msg.edit_text("❌ Aucun enregistrement de jeu trouvé dans ce canal.")
                    return

                games = []
                for rec in records:
                    text = rec['text'] if isinstance(rec, dict) else rec
                    date_str = rec.get('date', '') if isinstance(rec, dict) else ''
                    g = parse_game(text)
                    if g:
                        if date_str:
                            g['date'] = date_str
                        games.append(g)

                save_analyzed_games(games)
                prefix = "🛑 Chargement interrompu" if was_cancelled else "✅"
                await msg.edit_text(
                    f"{prefix} *{len(games)} jeux analysés* depuis *{html.escape(title)}*{scope_desc}\n\n"
                    f"Commandes disponibles :\n"
                    f"/gstats — Statistiques complètes\n"
                    f"/gvictoire joueur|banquier|nul\n"
                    f"/gparite pair|impair\n"
                    f"/gstructure 2/2|2/3|3/2|3/3\n"
                    f"/gplusmoins j|b plus|moins\n"
                    f"/gcostume ♠|♥|♦|♣ j|b\n"
                    f"/gecartmax",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"gload error: {e}")
                try:
                    if _is_auth_key_dup(e):
                        await msg.edit_text(_AUTH_KEY_DUP_MSG, parse_mode='HTML')
                    else:
                        await msg.edit_text(f"❌ Erreur: {str(e)[:300]}")
                except Exception:
                    pass
            finally:
                _search_cancel.pop(uid, None)

        context.application.create_task(_do_gload())

    # ── GESTION DES ADMINISTRATEURS ────────────────────────────────────────────

    async def addadmin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/addadmin <user_id> — Ajoute un administrateur (menu de sélection des commandes)."""
        if not is_main_admin(update.effective_user.id):
            await update.message.reply_text("❌ Réservé à l'administrateur principal.")
            return
        if not context.args or not context.args[0].lstrip('-').isdigit():
            await update.message.reply_text(
                "Usage : <code>/addadmin USER_ID</code>\n\n"
                "L'utilisateur doit d'abord écrire au bot pour obtenir son ID via /myid.",
                parse_mode='HTML'
            )
            return
        uid = int(context.args[0])
        if uid == ADMIN_ID:
            await update.message.reply_text("ℹ️ C'est déjà l'administrateur principal.")
            return
        if uid in get_admins():
            await update.message.reply_text(
                f"⚠️ <code>{uid}</code> est déjà admin.\n"
                f"Pour modifier ses permissions : /setperm {uid}",
                parse_mode='HTML'
            )
            return
        # Afficher le menu numéroté et attendre la saisie
        _waiting_for_perm[update.effective_user.id] = {'target_uid': uid, 'action': 'add'}
        await update.message.reply_text(_build_cmd_menu(uid, 'add'), parse_mode='HTML')

    async def removeadmin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/removeadmin <user_id> — Supprime un administrateur."""
        if not is_main_admin(update.effective_user.id):
            await update.message.reply_text("❌ Réservé à l'administrateur principal.")
            return
        if not context.args or not context.args[0].lstrip('-').isdigit():
            await update.message.reply_text("Usage: `/removeadmin 123456789`", parse_mode='Markdown')
            return
        uid = int(context.args[0])
        if uid == ADMIN_ID:
            await update.message.reply_text("❌ Impossible de supprimer l'administrateur principal.")
            return
        removed = remove_admin(uid)
        if removed:
            await update.message.reply_text(f"🗑️ Admin supprimé : `{uid}`", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"⚠️ `{uid}` n'est pas dans la liste.", parse_mode='Markdown')

    async def listadmins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/admins — Liste les administrateurs avec leurs permissions."""
        if not is_main_admin(update.effective_user.id):
            return
        all_perms = get_admins_with_permissions()
        lines = ["👥 *Administrateurs autorisés :*\n"]
        for uid, cmds in all_perms.items():
            if uid == ADMIN_ID:
                lines.append(f"👑 `{uid}` _(principal — accès total)_")
            else:
                cmds_str = ', '.join(f'`{c}`' for c in cmds) if cmds else '_aucune_'
                lines.append(f"• `{uid}`\n  🔑 {cmds_str}")
        lines.append(f"\nTotal : {len(all_perms)} admin(s)")
        lines.append("\nAjout : `/addadmin USER_ID` → menu numéroté")
        lines.append("Modifier : `/setperm USER_ID` → menu numéroté")
        lines.append("Supprimer : `/removeadmin USER_ID`")
        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

    async def setperm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/setperm <user_id> — Modifie les permissions d'un admin (menu de sélection)."""
        if not is_main_admin(update.effective_user.id):
            await update.message.reply_text("❌ Réservé à l'administrateur principal.")
            return
        if not context.args or not context.args[0].lstrip('-').isdigit():
            await update.message.reply_text(
                "Usage : <code>/setperm USER_ID</code>",
                parse_mode='HTML'
            )
            return
        uid = int(context.args[0])
        if uid == ADMIN_ID:
            await update.message.reply_text("❌ Impossible de modifier l'admin principal.")
            return
        if uid not in get_admins():
            await update.message.reply_text(
                f"⚠️ <code>{uid}</code> n'est pas admin.", parse_mode='HTML'
            )
            return
        # Afficher le menu numéroté et attendre la saisie
        _waiting_for_perm[update.effective_user.id] = {'target_uid': uid, 'action': 'update'}
        await update.message.reply_text(_build_cmd_menu(uid, 'update'), parse_mode='HTML')

    async def myid(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/myid — Affiche votre Telegram user ID."""
        uid = update.effective_user.id
        name = update.effective_user.full_name or "Inconnu"
        await update.message.reply_text(
            f"👤 *{html.escape(name)}*\nVotre ID : `{uid}`",
            parse_mode='Markdown'
        )

    async def gstats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gstats — Bilan des écarts des jeux analysés."""
        if not await self._perm(update, 'gstats'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt
        cats = build_category_stats(games)
        heure = _dt.now().strftime('%H:%M')
        nb = len(games)

        def _em(nums):
            """Retourne (total, écart_max) pour une liste de numéros."""
            total = len(nums)
            if total < 2:
                return total, 0
            sorted_nums = sorted(int(n) for n in nums)
            ecarts = [sorted_nums[i+1] - sorted_nums[i] for i in range(len(sorted_nums)-1)]
            return total, max(ecarts)

        v = cats['victoire']
        p = cats['parite']
        s = cats['structure']

        def line(emoji, label, nums):
            t, em = _em(nums)
            return f"{emoji} {label} : {t} | Écart max : {em}"

        lines = [
            "🌸 <b>BILAN DES ÉCARTS</b> 🌸",
            f"⏰ {heure} | 🎲 {nb} jeux",
            "",
            line("👤", "Victoire Joueur", v.get('JOUEUR', [])),
            line("🏦", "Victoire Banquier", v.get('BANQUIER', [])),
            line("⚖️", "Match Nul", v.get('NUL', [])),
            line("🔵", "Pair", p.get('PAIR', [])),
            line("🔴", "Impair", p.get('IMPAIR', [])),
            line("🧡", "3/2", s.get('3/2', [])),
            line("❤️", "3/3", s.get('3/3', [])),
            line("🖤", "2/2", s.get('2/2', [])),
            line("💚", "2/3", s.get('2/3', [])),
            "",
            line("👤", "Joueur 2K (2/2+2/3)", s.get('2/2', []) + s.get('2/3', [])),
            line("👤", "Joueur 3K (3/2+3/3)", s.get('3/2', []) + s.get('3/3', [])),
            line("🏦", "Banquier 2K (2/2+3/2)", s.get('2/2', []) + s.get('3/2', [])),
            line("🏦", "Banquier 3K (2/3+3/3)", s.get('2/3', []) + s.get('3/3', [])),
            "",
            "🃏 <b>Cartes de Valeur</b>",
        ]

        fj = cats.get('face_j', {})
        fb = cats.get('face_b', {})
        for fc, label in [('A', 'As'), ('K', 'Roi'), ('Q', 'Dame'), ('J', 'Valet')]:
            lines.append(line("👤", f"Joueur {label}", fj.get(fc, [])))
            lines.append(line("🏦", f"Banquier {label}", fb.get(fc, [])))

        lines.append("")
        lines.append("🃏 <b>Valeurs Spéciales (par costume)</b>")
        fsj = cats.get('face_suit_j', {})
        fsb = cats.get('face_suit_b', {})
        for fc, label in [('A', 'As'), ('K', 'Roi'), ('Q', 'Dame'), ('J', 'Valet')]:
            for side_k, side_l, side_e in [('face_suit_j', 'Joueur', '👤'), ('face_suit_b', 'Banquier', '🏦')]:
                row = []
                sd = cats.get(side_k, {})
                for suit in ['♠', '♥', '♦', '♣']:
                    key = f'{fc}{suit}'
                    t, em = _em(sd.get(key, []))
                    row.append(f"{SUIT_EMOJI[suit]}:{em}")
                lines.append(f"{side_e} {label} {side_l} — {' | '.join(row)}")

        await update.message.reply_text('\n'.join(lines), parse_mode='HTML')

    async def gvictoire(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gvictoire [joueur|banquier|nul] — Numéros et écarts par victoire."""
        if not await self._perm(update, 'gvictoire'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt
        cats = build_category_stats(games)
        arg = ' '.join(context.args).upper().strip() if context.args else ''

        victoire_cats = cats['victoire']
        keys_to_show = [arg] if arg in ('JOUEUR', 'BANQUIER', 'NUL') else list(victoire_cats.keys())

        for k in keys_to_show:
            nums = victoire_cats[k]
            result = format_ecarts(nums, f"🏆 Victoire {k}")
            sent = await update.message.reply_text(f"```\n{result}\n```", parse_mode='Markdown')
            _schedule_delete(sent, delay=10)

        heure = _dt.now().strftime('%H:%M')
        nb = len(games)
        bilan_lines = [f"🌸 <b>BILAN DES VICTOIRES</b> 🌸", f"⏰ {heure} | 🎲 {nb} jeux\n"]
        for k in keys_to_show:
            em = _max_ecart(victoire_cats[k])
            bilan_lines.append(f"🏆 Écart max {k.capitalize()} : {em}")
        await update.message.reply_text('\n'.join(bilan_lines), parse_mode='HTML')

    async def gparite(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gparite [pair|impair] — Numéros et écarts par parité."""
        if not await self._perm(update, 'gparite'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt
        cats = build_category_stats(games)
        arg = ' '.join(context.args).upper().strip() if context.args else ''

        parite_cats = cats['parite']
        keys_to_show = [arg] if arg in ('PAIR', 'IMPAIR') else list(parite_cats.keys())

        for k in keys_to_show:
            nums = parite_cats[k]
            result = format_ecarts(nums, f"📊 {k}")
            sent = await update.message.reply_text(f"```\n{result}\n```", parse_mode='Markdown')
            _schedule_delete(sent, delay=10)

        heure = _dt.now().strftime('%H:%M')
        nb = len(games)
        bilan_lines = [f"🌸 <b>BILAN DE PARITÉ</b> 🌸", f"⏰ {heure} | 🎲 {nb} jeux\n"]
        for k in keys_to_show:
            em = _max_ecart(parite_cats[k])
            bilan_lines.append(f"📊 Écart max {k.capitalize()} : {em}")
        await update.message.reply_text('\n'.join(bilan_lines), parse_mode='HTML')

    async def gstructure(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gstructure [2/2|2/3|3/2|3/3] — Numéros et écarts par structure de cartes."""
        if not await self._perm(update, 'gstructure'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt
        cats = build_category_stats(games)
        arg = ' '.join(context.args).strip() if context.args else ''

        valid = ['2/2', '2/3', '3/2', '3/3']
        keys_to_show = [arg] if arg in valid else valid

        for k in keys_to_show:
            nums = cats['structure'][k]
            if nums:
                result = format_ecarts(nums, f"🎴 Structure {k}")
                sent = await update.message.reply_text(f"```\n{result}\n```", parse_mode='Markdown')
                _schedule_delete(sent, delay=10)

        heure = _dt.now().strftime('%H:%M')
        nb = len(games)
        bilan_lines = [f"🌸 <b>BILAN DES STRUCTURES</b> 🌸", f"⏰ {heure} | 🎲 {nb} jeux\n"]
        for k in keys_to_show:
            nums = cats['structure'][k]
            em = _max_ecart(nums)
            bilan_lines.append(f"🎴 Écart max {k} : {em}")

        # Bilans Banquier 2K et 3K (regroupement par nb de cartes Banquier)
        if not arg:
            bk2 = cats['structure']['2/2'] + cats['structure']['3/2']
            bk3 = cats['structure']['2/3'] + cats['structure']['3/3']
            bilan_lines.append("")
            bilan_lines.append("🏦 <b>Banquier par nombre de cartes :</b>")
            bilan_lines.append(f"  2K (2 cartes) : {len(bk2)} jeux | Écart max : {_max_ecart(bk2)}")
            bilan_lines.append(f"  3K (3 cartes) : {len(bk3)} jeux | Écart max : {_max_ecart(bk3)}")

        await update.message.reply_text('\n'.join(bilan_lines), parse_mode='HTML')

    async def gplusmoins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gplusmoins [j|b] [plus|moins] — Numéros et écarts par Plus/Moins."""
        if not await self._perm(update, 'gplusmoins'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt
        cats = build_category_stats(games)
        args = [a.lower() for a in context.args] if context.args else []

        side_map = {'j': 'plusmoins_j', 'joueur': 'plusmoins_j',
                    'b': 'plusmoins_b', 'banquier': 'plusmoins_b'}
        cat_map = {'plus': 'Plus de 6,5', 'moins': 'Moins de 4,5', 'neutre': 'Neutre'}

        side_key = side_map.get(args[0]) if args else None
        cat_key = cat_map.get(args[1]) if len(args) > 1 else None

        all_sides = [('plusmoins_j', 'Joueur'), ('plusmoins_b', 'Banquier')]
        sides_to_show = [(side_key, side_key.split('_')[1].capitalize())] if side_key else all_sides

        for side_k, side_label in sides_to_show:
            cats_to_show = {cat_key: cats[side_k][cat_key]} if cat_key else cats[side_k]
            for cat_label, nums in cats_to_show.items():
                if nums:
                    label = f"🎯 {side_label} — {cat_label}"
                    result = format_ecarts(nums, label)
                    sent = await update.message.reply_text(f"```\n{result}\n```", parse_mode='Markdown')
                    _schedule_delete(sent, delay=10)

        heure = _dt.now().strftime('%H:%M')
        nb = len(games)
        bilan_lines = [f"🌸 <b>BILAN PLUS/MOINS</b> 🌸", f"⏰ {heure} | 🎲 {nb} jeux\n"]
        for side_k, side_label in sides_to_show:
            cats_to_show = {cat_key: cats[side_k][cat_key]} if cat_key else cats[side_k]
            bilan_lines.append(f"<b>{'👤' if 'j' in side_k else '🏦'} {side_label} :</b>")
            for cat_label, nums in cats_to_show.items():
                em = _max_ecart(nums)
                bilan_lines.append(f"  Écart max {cat_label} : {em}")
        await update.message.reply_text('\n'.join(bilan_lines), parse_mode='HTML')

    async def gcostume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gcostume [♠|♥|♦|♣] [j|b] — Costumes manquants avec écarts."""
        if not await self._perm(update, 'gcostume'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt

        cats = build_category_stats(games)
        args = context.args if context.args else []

        suit_arg = normalize_suit(args[0]) if args else None
        side_arg = args[1].lower() if len(args) > 1 else None
        side_map = {'j': 'missing_j', 'joueur': 'missing_j',
                    'b': 'missing_b', 'banquier': 'missing_b'}
        side_key = side_map.get(side_arg) if side_arg else None

        def _bilan(suit):
            heure = _dt.now().strftime('%H:%M')
            nb = len(games)
            emoji = SUIT_EMOJI[suit]
            em_j = _max_ecart(cats['missing_j'][suit])
            em_b = _max_ecart(cats['missing_b'][suit])
            return (
                f"🌸 <b>BILAN DES ÉCARTS {emoji}</b> 🌸\n"
                f"⏰ {heure} | 🎲 {nb} jeux\n\n"
                f"👤 Nombre d'Écart max Joueur : {em_j}\n"
                f"🏦 Nombre d'Écart max Banquier : {em_b}"
            )

        suits_to_show = [suit_arg] if suit_arg else ['♠', '♥', '♦', '♣']

        for suit in suits_to_show:
            sides = [(side_key, side_key.split('_')[1].capitalize())] if side_key else [
                ('missing_j', 'Joueur'), ('missing_b', 'Banquier')
            ]
            for sk, sl in sides:
                nums = cats[sk][suit]
                label = f"{SUIT_EMOJI[suit]} Manquant {sl}"
                result = format_ecarts(nums, label)
                sent = await update.message.reply_text(f"```\n{result}\n```", parse_mode='Markdown')
                _schedule_delete(sent, delay=10)

            # Message bilan compact séparé — conservé indéfiniment
            await update.message.reply_text(_bilan(suit), parse_mode='HTML')

    async def gvaleur(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gvaleur [A|K|Q|J] [j|b] — Valeurs spéciales par costume avec écarts."""
        if not await self._perm(update, 'gvaleur'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt

        cats = build_category_stats(games)
        args = [a.upper() if a.upper() in ('A', 'K', 'Q', 'J') else a.lower() for a in (context.args or [])]

        face_arg = None
        side_arg = None
        for a in args:
            if a in ('A', 'K', 'Q', 'J'):
                face_arg = a
            elif a in ('j', 'joueur'):
                side_arg = 'face_suit_j'
            elif a in ('b', 'banquier'):
                side_arg = 'face_suit_b'

        face_labels = {'A': 'As', 'K': 'Roi', 'Q': 'Dame', 'J': 'Valet'}
        faces_to_show = [face_arg] if face_arg else ['A', 'K', 'Q', 'J']
        sides = [(side_arg, 'Joueur' if 'j' in side_arg else 'Banquier')] if side_arg else [
            ('face_suit_j', 'Joueur'), ('face_suit_b', 'Banquier')
        ]

        for fc in faces_to_show:
            for sk, sl in sides:
                for suit in ['♠', '♥', '♦', '♣']:
                    key = f'{fc}{suit}'
                    nums = cats[sk].get(key, [])
                    if nums:
                        label = f"🃏 {face_labels[fc]}{SUIT_EMOJI[suit]} {sl}"
                        result = format_ecarts(nums, label)
                        sent = await update.message.reply_text(f"```\n{result}\n```", parse_mode='Markdown')
                        _schedule_delete(sent, delay=10)

        heure = _dt.now().strftime('%H:%M')
        nb = len(games)
        bilan_lines = [f"🌸 <b>BILAN DES VALEURS SPÉCIALES</b> 🌸", f"⏰ {heure} | 🎲 {nb} jeux\n"]
        for fc in faces_to_show:
            bilan_lines.append(f"<b>🃏 {face_labels[fc]}</b>")
            for sk, sl in sides:
                emoji_side = '👤' if 'j' in sk else '🏦'
                bilan_lines.append(f"  {emoji_side} {sl}")
                for suit in ['♠', '♥', '♦', '♣']:
                    key = f'{fc}{suit}'
                    nums = cats[sk].get(key, [])
                    em = _max_ecart(nums)
                    cnt = len(nums)
                    bilan_lines.append(f"    {SUIT_EMOJI[suit]} Écart max : <b>{em}</b>  ({cnt} apparitions)")
        await update.message.reply_text('\n'.join(bilan_lines), parse_mode='HTML')

    async def gcycle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gcycle pair|impair [j|b] [N1-N2] — Analyse du cycle de costumes manquants."""
        if not await self._perm(update, 'gcycle'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt
        from itertools import product as _product

        CYCLE_PAIR = ['♥', '♦', '♣', '♠', '♦', '♥', '♠']
        CYCLE_IMPAIR = ['♥', '♦', '♣', '♠', '♦', '♥', '♠', '♣']
        SUIT_TO_EMOJI = {'♠': '♠️', '♥': '❤️', '♦': '♦️', '♣': '♣️'}
        # Variation selectors for suits
        EMOJI_TO_SUIT = {
            '♠️': '♠', '❤️': '♥', '♦️': '♦', '♣️': '♣',
            '♠': '♠', '♥': '♥', '♦': '♦', '♣': '♣'
        }
        
        args = context.args or []
        from_num = 6
        to_num = 1436
        side_key = None
        mode = None

        for a in args:
            al = a.lower()
            if al in ('pair', 'p'):
                mode = 'pair'
            elif al in ('impair', 'i'):
                mode = 'impair'
            elif al in ('j', 'joueur'):
                side_key = 'missing_j'
            elif al in ('b', 'banquier'):
                side_key = 'missing_b'
            elif '-' in a or '_' in a:
                parts = a.replace('_', '-').split('-')
                if len(parts) == 2:
                    try:
                        from_num = int(parts[0])
                        to_num = int(parts[1])
                    except ValueError:
                        pass
            elif a in EMOJI_TO_SUIT or (len(a) > 1 and a[:2] in EMOJI_TO_SUIT):
                # If someone passes a suit emoji as an argument, we could handle it
                pass

        if not mode:
            await update.message.reply_text(
                "📋 <b>Usage de /gcycle</b>\n\n"
                "<b>/gcycle pair</b> — Numéros pairs (sauf ×10)\n"
                "  Cycle : ❤️♦️♣️♠️♦️❤️♠️ (7 éléments)\n\n"
                "<b>/gcycle impair</b> — Numéros impairs + terminant par 0\n"
                "  Cycle : ❤️♦️♣️♠️♦️❤️♠️♣️ (8 éléments)\n\n"
                "Options : <code>j</code>/<code>b</code> (côté) · <code>6-1436</code> (plage)\n"
                "Ex : <code>/gcycle pair j 6-1436</code>",
                parse_mode='HTML'
            )
            return

        if mode == 'pair':
            cycle = CYCLE_PAIR
            mode_label = "PAIR (sauf ×10)"
            qualifying = [n for n in range(from_num, to_num + 1) if n % 2 == 0 and n % 10 != 0]
        else:
            cycle = CYCLE_IMPAIR
            mode_label = "IMPAIR + ×10"
            qualifying = [n for n in range(from_num, to_num + 1) if n % 2 != 0 or n % 10 == 0]

        cycle_display = [SUIT_TO_EMOJI[s] for s in cycle]
        cycle_len = len(cycle)

        game_map = {int(g['numero']): g for g in games}

        sides_to_check = [
            (side_key, 'Joueur' if side_key == 'missing_j' else 'Banquier')
        ] if side_key else [
            ('missing_j', 'Joueur'), ('missing_b', 'Banquier')
        ]

        def _check_cycle(test_cycle, sk, qualifying_nums):
            tlen = len(test_cycle)
            m, mm, nf = 0, 0, 0
            d_miss = []
            for idx, n in enumerate(qualifying_nums):
                exp = test_cycle[idx % tlen]
                if n not in game_map:
                    nf += 1
                    continue
                g = game_map[n]
                missing = g.get(sk, [])
                if exp in missing:
                    m += 1
                else:
                    actual = ', '.join(SUIT_TO_EMOJI.get(s, s) for s in missing) if missing else 'aucun'
                    d_miss.append(f"#{n} ❌ attendu {SUIT_TO_EMOJI[exp]}, manquant: {actual}")
                    mm += 1
            tot = m + mm
            return {'matches': m, 'mismatches': mm, 'not_found': nf, 'total': tot,
                    'pct': (m / tot * 100) if tot else 0, 'detail_miss': d_miss}

        def _find_best_cycle(sk, qualifying_nums, target_len):
            suits = ['♠', '♥', '♦', '♣']
            actual_suits = []
            for n in qualifying_nums:
                if n in game_map:
                    missing = game_map[n].get(sk, [])
                    actual_suits.append(missing)
                else:
                    actual_suits.append(None)

            best_cycle = None
            best_pct = 0

            for length in range(target_len - 1, target_len + 2):
                if length < 3 or length > 12:
                    continue
                counts = {}
                for idx, n in enumerate(qualifying_nums):
                    pos = idx % length
                    if n not in game_map:
                        continue
                    missing = game_map[n].get(sk, [])
                    if pos not in counts:
                        counts[pos] = {'♠': 0, '♥': 0, '♦': 0, '♣': 0}
                    for s in missing:
                        if s in counts[pos]:
                            counts[pos][s] += 1

                candidate = []
                for pos in range(length):
                    if pos in counts:
                        best_s = max(counts[pos], key=lambda s: counts[pos][s])
                        candidate.append(best_s)
                    else:
                        candidate.append('♠')

                r = _check_cycle(candidate, sk, qualifying_nums)
                if r['pct'] > best_pct:
                    best_pct = r['pct']
                    best_cycle = candidate

            return best_cycle, best_pct

        all_results = []
        suggested_cycles = []

        for sk, sl in sides_to_check:
            r = _check_cycle(cycle, sk, qualifying)
            r['side'] = sl
            r['side_emoji'] = '👤' if 'j' in sk else '🏦'
            all_results.append(r)

            best_c, best_p = _find_best_cycle(sk, qualifying, cycle_len)
            if best_c:
                suggested_cycles.append({
                    'side': sl, 'side_emoji': r['side_emoji'],
                    'cycle': best_c, 'pct': best_p,
                    'display': ''.join(SUIT_TO_EMOJI[s] for s in best_c),
                    'length': len(best_c),
                })

        heure = _dt.now().strftime('%H:%M')
        cycle_str = ''.join(cycle_display)

        for r in all_results:
            detail_lines = r['detail_miss'][:50]
            if len(r['detail_miss']) > 50:
                detail_lines.append(f"... et {len(r['detail_miss']) - 50} autres")
            if detail_lines:
                detail_text = (
                    f"🔍 <b>Détails {r['side_emoji']} {r['side']} — Écarts au cycle</b>\n\n"
                    + '\n'.join(detail_lines)
                )
                if len(detail_text) > 4000:
                    detail_text = detail_text[:3950] + "\n... (tronqué)"
                sent = await update.message.reply_text(detail_text, parse_mode='HTML')
                _schedule_delete(sent, delay=15)

        bilan_lines = [
            f"🔄 <b>ANALYSE DU CYCLE DE COSTUMES — {mode_label}</b>",
            f"⏰ {heure} | 🎲 Jeux #{from_num}→#{to_num}",
            f"📋 Cycle testé : {cycle_str} (longueur {cycle_len})",
            f"🔢 Numéros qualifiants : {len(qualifying)}",
            "",
        ]

        for r in all_results:
            bilan_lines.append(f"{r['side_emoji']} <b>{r['side']}</b>")
            bilan_lines.append(f"  ✅ Correspondances : {r['matches']}/{r['total']} ({r['pct']:.1f}%)")
            bilan_lines.append(f"  ❌ Écarts : {r['mismatches']}")
            if r['not_found']:
                bilan_lines.append(f"  ⚠️ Jeux non chargés : {r['not_found']}")
            bilan_lines.append("")

        if suggested_cycles:
            bilan_lines.append("🧠 <b>CYCLE CORRIGÉ SUGGÉRÉ</b>")
            bilan_lines.append("")
            for sc in suggested_cycles:
                bilan_lines.append(f"{sc['side_emoji']} <b>{sc['side']}</b>")
                bilan_lines.append(f"  📋 {sc['display']} (longueur {sc['length']})")
                bilan_lines.append(f"  ✅ Taux : <b>{sc['pct']:.1f}%</b>")
                improvement = sc['pct'] - [r for r in all_results if r['side'] == sc['side']][0]['pct']
                if improvement > 0:
                    bilan_lines.append(f"  📈 Amélioration : +{improvement:.1f}%")
                bilan_lines.append("")

        await update.message.reply_text('\n'.join(bilan_lines), parse_mode='HTML')

        for sc in suggested_cycles:
            sk = 'missing_j' if 'Joueur' in sc['side'] else 'missing_b'
            corr_cycle = sc['cycle']
            clen = len(corr_cycle)
            corr_lines = [f"{sc['side_emoji']} {sc['side']}", ""]
            for idx, n in enumerate(qualifying):
                expected_suit = corr_cycle[idx % clen]
                emoji = SUIT_TO_EMOJI[expected_suit]
                corr_lines.append(f"{n} [{emoji}]")
            corr_text = '\n'.join(corr_lines)
            if len(corr_text) > 4000:
                import tempfile, os as _os
                txt_path = f"/tmp/correction_{sc['side']}_{mode_label}.txt"
                with open(txt_path, 'w', encoding='utf-8') as fout:
                    fout.write(corr_text)
                with open(txt_path, 'rb') as fin:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=fin,
                        caption=f"📋 Correction {sc['side_emoji']} {sc['side']} — {sc['display']}",
                        filename=f"correction_{sc['side'].lower()}_{mode_label.lower()}.txt"
                    )
                _os.remove(txt_path)
            else:
                await update.message.reply_text(corr_text)

    async def gcycleauto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gcycleauto [j|b] [N1-N2] — Recherche auto du meilleur cycle + filtre de numéros."""
        if not await self._perm(update, 'gcycleauto'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt

        SUIT_TO_EMOJI = {'♠': '♠️', '♥': '❤️', '♦': '♦️', '♣': '♣️'}
        SUITS = ['♠', '♥', '♦', '♣']

        args = context.args or []
        from_num = 6
        to_num = 1436
        side_key = None

        for a in args:
            al = a.lower()
            if al in ('j', 'joueur'):
                side_key = 'missing_j'
            elif al in ('b', 'banquier'):
                side_key = 'missing_b'
            elif '-' in a or '_' in a:
                parts = a.replace('_', '-').split('-')
                if len(parts) == 2:
                    try:
                        from_num = int(parts[0])
                        to_num = int(parts[1])
                    except ValueError:
                        pass

        msg = await update.message.reply_text(
            "🔬 <b>Recherche du meilleur cycle en cours…</b>\n"
            "Analyse de toutes les combinaisons de filtres et longueurs.",
            parse_mode='HTML'
        )

        game_map = {int(g['numero']): g for g in games}
        all_nums = sorted(n for n in range(from_num, to_num + 1) if n in game_map)

        FILTERS = {
            'Tous les numéros':          lambda n: True,
            'Pairs (sauf ×10)':          lambda n: n % 2 == 0 and n % 10 != 0,
            'Impairs + ×10':             lambda n: n % 2 != 0 or n % 10 == 0,
            'Pairs uniquement':          lambda n: n % 2 == 0,
            'Impairs uniquement':        lambda n: n % 2 != 0,
            'Sauf ×10':                  lambda n: n % 10 != 0,
            'Sauf ×5':                   lambda n: n % 5 != 0,
            'Terminant par 2,4,6,8':     lambda n: n % 2 == 0 and n % 10 != 0,
            'Terminant par 1,3,7,9':     lambda n: n % 10 in (1,3,7,9),
            'Terminant par 2,8':         lambda n: n % 10 in (2,8),
            'Terminant par 4,6':         lambda n: n % 10 in (4,6),
            'Terminant par 1,3,5,7,9':   lambda n: n % 2 != 0,
            'Sauf ×3':                   lambda n: n % 3 != 0,
            'Multiple de 3 sauf ×10':    lambda n: n % 3 == 0 and n % 10 != 0,
        }

        sides_to_check = [
            (side_key, 'Joueur' if side_key == 'missing_j' else 'Banquier')
        ] if side_key else [
            ('missing_j', 'Joueur'), ('missing_b', 'Banquier')
        ]

        def _build_best_cycle(sk, nums, length):
            counts = {}
            for idx, n in enumerate(nums):
                pos = idx % length
                if n not in game_map:
                    continue
                missing = game_map[n].get(sk, [])
                if pos not in counts:
                    counts[pos] = {s: 0 for s in SUITS}
                for s in missing:
                    if s in counts[pos]:
                        counts[pos][s] += 1
            cycle = []
            for pos in range(length):
                if pos in counts and any(counts[pos][s] > 0 for s in SUITS):
                    cycle.append(max(counts[pos], key=lambda s: counts[pos][s]))
                else:
                    cycle.append('♠')
            return cycle

        def _score_cycle(cycle, sk, nums):
            clen = len(cycle)
            m, tot = 0, 0
            for idx, n in enumerate(nums):
                if n not in game_map:
                    continue
                missing = game_map[n].get(sk, [])
                tot += 1
                if cycle[idx % clen] in missing:
                    m += 1
            return m, tot

        top_results = []

        for sk, sl in sides_to_check:
            side_best = []
            for filter_name, filter_fn in FILTERS.items():
                filtered = [n for n in all_nums if filter_fn(n)]
                if len(filtered) < 20:
                    continue

                for length in range(5, 13):
                    best_cycle = _build_best_cycle(sk, filtered, length)
                    m, tot = _score_cycle(best_cycle, sk, filtered)
                    if tot == 0:
                        continue
                    pct = m / tot * 100
                    side_best.append({
                        'filter': filter_name,
                        'length': length,
                        'cycle': best_cycle,
                        'matches': m,
                        'total': tot,
                        'pct': pct,
                        'display': ''.join(SUIT_TO_EMOJI[s] for s in best_cycle),
                        'side': sl,
                        'side_emoji': '👤' if 'j' in sk else '🏦',
                        'side_key': sk,
                        'nums': filtered,
                    })

            side_best.sort(key=lambda x: -x['pct'])
            seen_filters = set()
            for r in side_best:
                if r['filter'] not in seen_filters and len(top_results) < 10:
                    seen_filters.add(r['filter'])
                    top_results.append(r)
                if len(seen_filters) >= 5:
                    break

        top_results.sort(key=lambda x: -x['pct'])

        heure = _dt.now().strftime('%H:%M')
        bilan_lines = [
            f"🔬 <b>RECHERCHE AUTOMATIQUE DU MEILLEUR CYCLE</b>",
            f"⏰ {heure} | 🎲 Jeux #{from_num}→#{to_num}",
            f"🔢 Jeux disponibles : {len(all_nums)}",
            f"🧪 {len(FILTERS)} filtres × 8 longueurs testés",
            "",
        ]

        if not top_results:
            bilan_lines.append("❌ Aucune combinaison trouvée.")
        else:
            bilan_lines.append("🏆 <b>TOP RÉSULTATS</b>")
            bilan_lines.append("")

            for rank, r in enumerate(top_results[:5], 1):
                medal = {1: '🥇', 2: '🥈', 3: '🥉'}.get(rank, f'{rank}.')
                bilan_lines.append(f"{medal} {r['side_emoji']} <b>{r['side']}</b> — {r['filter']}")
                bilan_lines.append(f"   📋 Cycle : {r['display']} (longueur {r['length']})")
                bilan_lines.append(f"   ✅ <b>{r['pct']:.1f}%</b> ({r['matches']}/{r['total']} jeux)")

                sample = r['nums'][:8]
                sample_str = ', '.join(f'#{n}' for n in sample)
                num_count = len(r['nums'])
                if num_count > 8:
                    sample_str += f'… ({num_count} total)'
                bilan_lines.append(f"   🔢 Numéros : {sample_str}")
                bilan_lines.append("")

        best = top_results[0] if top_results else None
        if best:
            bilan_lines.append("━━━━━━━━━━━━━━━━━━")
            bilan_lines.append(f"💡 <b>MEILLEUR CYCLE TROUVÉ</b>")
            bilan_lines.append(f"   {best['side_emoji']} {best['side']} — {best['filter']}")
            bilan_lines.append(f"   📋 <b>{best['display']}</b> (longueur {best['length']})")
            bilan_lines.append(f"   ✅ Taux : <b>{best['pct']:.1f}%</b>")
            bilan_lines.append("")

            detail_miss = []
            clen = len(best['cycle'])
            for idx, n in enumerate(best['nums']):
                if n not in game_map:
                    continue
                missing = game_map[n].get(best['side_key'], [])
                exp = best['cycle'][idx % clen]
                if exp not in missing:
                    actual = ', '.join(SUIT_TO_EMOJI.get(s, s) for s in missing) if missing else 'aucun'
                    detail_miss.append(f"#{n} ❌ attendu {SUIT_TO_EMOJI[exp]}, manquant: {actual}")
            if detail_miss:
                detail_lines = detail_miss[:40]
                if len(detail_miss) > 40:
                    detail_lines.append(f"... et {len(detail_miss) - 40} autres")
                detail_text = (
                    f"🔍 <b>Écarts au meilleur cycle — {best['side_emoji']} {best['side']}</b>\n"
                    f"📋 {best['display']} | {best['filter']}\n\n"
                    + '\n'.join(detail_lines)
                )
                if len(detail_text) > 4000:
                    detail_text = detail_text[:3950] + "\n... (tronqué)"
                sent = await update.message.reply_text(detail_text, parse_mode='HTML')
                _schedule_delete(sent, delay=20)

        try:
            await msg.delete()
        except Exception:
            pass
        await update.message.reply_text('\n'.join(bilan_lines), parse_mode='HTML')

        if best:
            corr_cycle = best['cycle']
            clen = len(corr_cycle)
            best_nums = best['nums']
            corr_lines = [f"{best['side_emoji']} {best['side']} — {best['filter']}", ""]
            for idx, n in enumerate(best_nums):
                expected_suit = corr_cycle[idx % clen]
                emoji = SUIT_TO_EMOJI[expected_suit]
                corr_lines.append(f"{n} [{emoji}]")
            corr_text = '\n'.join(corr_lines)
            if len(corr_text) > 4000:
                import os as _os
                side_name = best['side'].lower().replace(' ', '_')
                txt_path = f"/tmp/correction_auto_{side_name}.txt"
                with open(txt_path, 'w', encoding='utf-8') as fout:
                    fout.write(corr_text)
                with open(txt_path, 'rb') as fin:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=fin,
                        caption=f"📋 Correction {best['side_emoji']} {best['side']} — {best['display']} | {best['filter']}",
                        filename=f"correction_{side_name}.txt"
                    )
                _os.remove(txt_path)
            else:
                await update.message.reply_text(corr_text)

    async def gecartmax(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gecartmax — Paires de numéros formant l'écart max par catégorie + bilan global."""
        if not await self._perm(update, 'gecartmax'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text("❌ Aucun jeu chargé. Tapez /gload d'abord.")
            return

        from datetime import datetime as _dt
        cats = build_category_stats(games)

        all_nums_global = [int(g['numero']) for g in games]
        first_game = min(all_nums_global)
        last_game  = max(all_nums_global)

        def find_max_gap(nums):
            """
            Retourne (label_gauche, label_droit, ecart_max).
            Prend en compte :
              - L'absence initiale (début du dataset → 1ère occurrence)
              - Les absences entre occurrences consécutives
              - L'absence terminale (dernière occurrence → fin du dataset)
            """
            if not nums:
                return ('Début', 'Fin', last_game - first_game)
            s = sorted(int(n) for n in nums)

            best_diff = 0
            best_left = 'Début'
            best_right = str(s[0])

            # Gap initial : début dataset → première occurrence
            init_gap = s[0] - first_game
            if init_gap > best_diff:
                best_diff  = init_gap
                best_left  = f'Début(#{first_game})'
                best_right = str(s[0])

            # Gaps entre occurrences consécutives
            for i in range(len(s) - 1):
                diff = s[i + 1] - s[i]
                if diff > best_diff:
                    best_diff  = diff
                    best_left  = str(s[i])
                    best_right = str(s[i + 1])

            # Gap terminal : dernière occurrence → fin dataset
            term_gap = last_game - s[-1]
            if term_gap > best_diff:
                best_diff  = term_gap
                best_left  = str(s[-1])
                best_right = f'Fin(#{last_game})'

            return (best_left, best_right, best_diff)

        all_categories = [
            ("🏆 Victoire Joueur",        cats['victoire']['JOUEUR']),
            ("🏆 Victoire Banquier",       cats['victoire']['BANQUIER']),
            ("🏆 Victoire Nul",            cats['victoire']['NUL']),
            ("📊 Parité Pair",             cats['parite']['PAIR']),
            ("📊 Parité Impair",           cats['parite']['IMPAIR']),
            ("🎴 Structure 2/2",           cats['structure']['2/2']),
            ("🎴 Structure 2/3",           cats['structure']['2/3']),
            ("🎴 Structure 3/2",           cats['structure']['3/2']),
            ("🎴 Structure 3/3",           cats['structure']['3/3']),
            ("🎯 Plus/Moins Joueur +6.5",  cats['plusmoins_j']['Plus de 6,5']),
            ("🎯 Plus/Moins Joueur -4.5",  cats['plusmoins_j']['Moins de 4,5']),
            ("🎯 Plus/Moins Joueur Neutre", cats['plusmoins_j']['Neutre']),
            ("🎯 Plus/Moins Banquier +6.5", cats['plusmoins_b']['Plus de 6,5']),
            ("🎯 Plus/Moins Banquier -4.5", cats['plusmoins_b']['Moins de 4,5']),
            ("🎯 Plus/Moins Banquier Neutre", cats['plusmoins_b']['Neutre']),
            ("♠️ Manquant Joueur ♠",       cats['missing_j']['♠']),
            ("♥️ Manquant Joueur ♥",       cats['missing_j']['♥']),
            ("♦️ Manquant Joueur ♦",       cats['missing_j']['♦']),
            ("♣️ Manquant Joueur ♣",       cats['missing_j']['♣']),
            ("♠️ Manquant Banquier ♠",     cats['missing_b']['♠']),
            ("♥️ Manquant Banquier ♥",     cats['missing_b']['♥']),
            ("♦️ Manquant Banquier ♦",     cats['missing_b']['♦']),
            ("♣️ Manquant Banquier ♣",     cats['missing_b']['♣']),
            ("🃏 Joueur As",               cats.get('face_j', {}).get('A', [])),
            ("🃏 Joueur Roi",              cats.get('face_j', {}).get('K', [])),
            ("🃏 Joueur Dame",             cats.get('face_j', {}).get('Q', [])),
            ("🃏 Joueur Valet",            cats.get('face_j', {}).get('J', [])),
            ("🃏 Banquier As",             cats.get('face_b', {}).get('A', [])),
            ("🃏 Banquier Roi",            cats.get('face_b', {}).get('K', [])),
            ("🃏 Banquier Dame",           cats.get('face_b', {}).get('Q', [])),
            ("🃏 Banquier Valet",          cats.get('face_b', {}).get('J', [])),
        ]
        face_labels = {'A': 'As', 'K': 'Roi', 'Q': 'Dame', 'J': 'Valet'}
        fsj = cats.get('face_suit_j', {})
        fsb = cats.get('face_suit_b', {})
        for fc in ['A', 'K', 'Q', 'J']:
            for suit in ['♠', '♥', '♦', '♣']:
                key = f'{fc}{suit}'
                all_categories.append((f"🃏 {face_labels[fc]}{SUIT_EMOJI[suit]} Joueur", fsj.get(key, [])))
                all_categories.append((f"🃏 {face_labels[fc]}{SUIT_EMOJI[suit]} Banquier", fsb.get(key, [])))

        detail_lines = ["🔍 <b>PAIRES D'ÉCART MAXIMUM PAR CATÉGORIE</b>\n"]
        bilan_lines = []

        for label, nums in all_categories:
            left, right, diff = find_max_gap(nums)
            if diff == 0:
                continue
            detail_lines.append(f"<b>{label}</b>")
            detail_lines.append(f"  N° {left}  →  N° {right}  =  <b>{diff}</b>\n")
            bilan_lines.append(f"{label} : {diff}")

        detail_text = '\n'.join(detail_lines)
        sent = await update.message.reply_text(detail_text, parse_mode='HTML')
        _schedule_delete(sent, delay=10)

        heure = _dt.now().strftime('%H:%M')
        nb = len(games)
        bilan_text = (
            f"🌸 <b>BILAN GLOBAL DES ÉCARTS MAX</b> 🌸\n"
            f"⏰ {heure} | 🎲 {nb} jeux\n\n"
            + '\n'.join(bilan_lines)
        )
        await update.message.reply_text(bilan_text, parse_mode='HTML')

    async def gclear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gclear — Efface les jeux analysés."""
        if not await self._perm(update, 'gclear'):
            return
        clear_analyzed_games()
        await update.message.reply_text("🗑️ Jeux analysés effacés.")

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Routeur de texte : canal, helpcl, predict, permissions, recherche ou analyse de jeu."""
        uid = update.effective_user.id
        if _ds_load(uid):
            await self.handle_dsearch_input(update, context)
        elif _waiting_for_helpcl.get(uid):
            await self.handle_helpcl_input(update, context)
        elif _waiting_for_predict.get(uid):
            await self.handle_predict_input(update, context)
        elif _waiting_for_perm.get(uid):
            await self.handle_perm_input(update, context)
        elif _waiting_for_game.get(uid):
            await self.handle_game_input(update, context)
        elif _waiting_for_channel.get(uid):
            await self.handle_channel_input(update, context)
        # Sinon, on ignore le message

    async def handle_perm_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reçoit la saisie numérotée de commandes pour addadmin/setperm."""
        uid = update.effective_user.id
        state = _waiting_for_perm.get(uid)
        if not state:
            return

        text = update.message.text.strip()
        if text.lower() in ('/cancel', 'cancel', 'annuler'):
            _waiting_for_perm.pop(uid, None)
            await update.message.reply_text("❌ Annulé.")
            return

        target_uid = state['target_uid']
        action = state['action']

        # Analyse de la saisie : supporte "1,3,4" et "1-5,8,13"
        indices = set()
        for part in text.replace(' ', ',').split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                bounds = part.split('-', 1)
                try:
                    a, b = int(bounds[0]), int(bounds[1])
                    indices.update(range(a, b + 1))
                except ValueError:
                    pass
            elif part.isdigit():
                indices.add(int(part))

        # Filtrer les indices valides
        valid = [i for i in sorted(indices) if 1 <= i <= len(ALL_COMMANDS)]
        if not valid:
            await update.message.reply_text(
                "❌ Aucun numéro valide reconnu.\n"
                f"Tapez des numéros entre 1 et {len(ALL_COMMANDS)}, ex : <code>1,3,5</code>",
                parse_mode='HTML'
            )
            return

        granted = [ALL_COMMANDS[i - 1] for i in valid]
        _waiting_for_perm.pop(uid, None)

        if action == 'add':
            add_admin(target_uid, granted)
            verb = "Nouvel admin ajouté"
        else:
            update_admin_permissions(target_uid, granted)
            verb = "Permissions mises à jour"

        cmds_str = '\n'.join(f"  {i}. {c}" for i, c in zip(valid, granted))
        await update.message.reply_text(
            f"✅ <b>{verb}</b> : <code>{target_uid}</code>\n\n"
            f"🔑 Commandes accordées :\n{cmds_str}",
            parse_mode='HTML'
        )

    async def handle_game_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reçoit le texte de jeu quand le bot attend une analyse."""
        if not is_admin(update.effective_user.id):
            return
        if not _waiting_for_game.get(update.effective_user.id):
            return

        text = update.message.text.strip()
        if text.lower() in ('/cancel', 'cancel', 'annuler'):
            _waiting_for_game.pop(update.effective_user.id, None)
            await update.message.reply_text("❌ Annulé.")
            return

        game = parse_game(text)
        _waiting_for_game.pop(update.effective_user.id, None)

        if not game:
            await update.message.reply_text(
                "❌ Format non reconnu.\n\n"
                "Exemple attendu :\n`#N794. ✅3(K♦️4♦️9♦️) - 1(J♦️10♥️A♠️) #T4`",
                parse_mode='Markdown'
            )
            return

        analysis = format_analysis(game)
        await update.message.reply_text(analysis)

    # ── SYSTÈME DE PRÉDICTION ─────────────────────────────────────────────────

    async def predictsetup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/predictsetup — Configure les canaux de prédiction (rôles stats/prédicteur)."""
        if not await self._perm(update, 'predictsetup'):
            return
        channels = get_channels()
        if not channels:
            await update.message.reply_text(
                "❌ Aucun canal configuré.\n"
                "Ajoutez au moins 2 canaux avec /addchannel avant de configurer les prédictions."
            )
            return
        if len(channels) < 2:
            await update.message.reply_text(
                "⚠️ Vous n'avez qu'un seul canal configuré.\n"
                "Le système de prédiction nécessite au moins :\n"
                "• 1 canal <b>statistiques</b> (résultats #N)\n"
                "• 1 canal <b>prédicteur</b> (optionnel, pour cross-analyse)\n\n"
                "Ajoutez d'autres canaux avec /addchannel.",
                parse_mode='HTML'
            )
            return

        cfg = get_predict_config()
        roles = cfg.get('channels', {})

        _waiting_for_predict[update.effective_user.id] = {'channels': channels}

        role_labels = {'stats': '📊 STATS', 'predictor': '🎯 PRÉDICTEUR'}
        lines = ["🔧 <b>CONFIGURATION DES CANAUX DE PRÉDICTION</b>\n"]
        lines.append("Assignez un rôle à chaque canal :\n")
        for i, ch in enumerate(channels, 1):
            name = ch.get('name') or ch['id']
            role = roles.get(ch['id'], '—')
            role_txt = role_labels.get(role, '❔ non assigné')
            lines.append(f"<b>{i}.</b> {name}\n   <code>{ch['id']}</code>  →  {role_txt}")

        lines.append("\n<b>Rôles disponibles :</b>")
        lines.append("  <code>S</code> = Statistiques (canal avec résultats #N)")
        lines.append("  <code>P</code> = Prédicteur (canal source de prédictions)")
        lines.append("\n✏️ Tapez les assignations :")
        lines.append("  Ex : <code>1=S 2=S 3=P</code>")
        lines.append("  Ex : <code>1=S</code> (un seul canal stats suffit)")
        lines.append("\nTapez <code>reset</code> pour effacer la configuration.")
        lines.append("Tapez <code>sortir</code> pour annuler.")
        await update.message.reply_text('\n'.join(lines), parse_mode='HTML')

    async def handle_predict_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reçoit la saisie des rôles dans /predictsetup."""
        uid = update.effective_user.id
        state = _waiting_for_predict.get(uid)
        if not state:
            return

        text = update.message.text.strip().lower()

        if text in ('sortir', 'exit', 'cancel', 'annuler', '/cancel'):
            _waiting_for_predict.pop(uid, None)
            await update.message.reply_text("↩️ Configuration annulée.")
            return

        if text == 'reset':
            reset_predict_config()
            _waiting_for_predict.pop(uid, None)
            await update.message.reply_text("🗑️ Configuration de prédiction réinitialisée.")
            return

        channels = state['channels']
        # Parser "1=S 2=P 3=S" etc.
        role_map = {'s': 'stats', 'stats': 'stats', 'p': 'predictor', 'predicteur': 'predictor', 'predictor': 'predictor'}
        assignments = {}
        errors = []
        for token in text.replace(',', ' ').split():
            if '=' in token:
                parts = token.split('=', 1)
                idx_str, role_str = parts[0].strip(), parts[1].strip()
                if not idx_str.isdigit():
                    errors.append(f"'{token}' invalide")
                    continue
                idx = int(idx_str)
                if not (1 <= idx <= len(channels)):
                    errors.append(f"Canal {idx} n'existe pas")
                    continue
                role = role_map.get(role_str)
                if not role:
                    errors.append(f"Rôle '{role_str}' inconnu (S ou P)")
                    continue
                assignments[channels[idx - 1]['id']] = role

        if errors:
            await update.message.reply_text(
                "❌ Erreurs :\n" + '\n'.join(f'  • {e}' for e in errors) +
                "\n\nFormat : <code>1=S 2=P</code>", parse_mode='HTML'
            )
            return

        if not assignments:
            await update.message.reply_text(
                "❌ Aucune assignation reconnue.\nFormat : <code>1=S 2=P</code>",
                parse_mode='HTML'
            )
            return

        # Sauvegarder
        for cid, role in assignments.items():
            set_channel_role(cid, role)
        _waiting_for_predict.pop(uid, None)

        cfg = get_predict_config()
        roles_saved = cfg.get('channels', {})
        role_labels = {'stats': '📊 STATS', 'predictor': '🎯 PRÉDICTEUR'}
        lines = ["✅ <b>Configuration sauvegardée !</b>\n"]
        for ch in channels:
            role = roles_saved.get(ch['id'], '—')
            role_txt = role_labels.get(role, '❔ non assigné')
            name = ch.get('name') or ch['id']
            lines.append(f"• {name} → {role_txt}")

        stats_chs = get_stats_channels()
        lines.append(f"\n<b>Étapes suivantes :</b>")
        if stats_chs:
            lines.append("1. Tapez /gpredictload pour charger les jeux des canaux statistiques")
            lines.append("2. Tapez /gpredict N1 N2 pour générer des prédictions")
        else:
            lines.append("⚠️ Aucun canal STATS défini — ajoutez au moins un canal S.")
        await update.message.reply_text('\n'.join(lines), parse_mode='HTML')

    async def gpredictload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gpredictload — Charge les jeux depuis tous les canaux statistiques configurés."""
        if not await self._perm(update, 'gpredictload'):
            return
        stats_chs = get_stats_channels()
        if not stats_chs:
            await update.message.reply_text(
                "❌ Aucun canal statistiques configuré.\n"
                "Utilisez /predictsetup d'abord pour assigner les rôles."
            )
            return

        from config import API_ID, API_HASH, SESSION_PATH, TELETHON_SESSION_STRING
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from game_analyzer import parse_game
        import asyncio

        msg = await update.message.reply_text(
            f"⏳ Chargement des jeux depuis <b>{len(stats_chs)}</b> canal(aux) statistiques…",
            parse_mode='HTML'
        )

        all_games = []
        seen_nums = set()

        async def _load_from_stats():
            try:
                session = StringSession(TELETHON_SESSION_STRING) if TELETHON_SESSION_STRING else SESSION_PATH
                client = TelegramClient(session, API_ID, API_HASH)
                await client.connect()
                for cid in stats_chs:
                    count = 0
                    async for message in client.iter_messages(int(cid), limit=5000):
                        if not message.text:
                            continue
                        game = parse_game(message.text)
                        if game and game['numero'] not in seen_nums:
                            seen_nums.add(game['numero'])
                            all_games.append(game)
                            count += 1
                await client.disconnect()
                all_games.sort(key=lambda g: int(g['numero']))
                save_analyzed_games(all_games)
                await msg.edit_text(
                    f"✅ <b>{len(all_games)}</b> jeux chargés depuis {len(stats_chs)} canal(aux) statistiques.\n\n"
                    f"Tapez /gpredict N1 N2 pour générer des prédictions.\n"
                    f"Tapez /gstats pour voir le résumé.",
                    parse_mode='HTML'
                )
            except Exception as e:
                await msg.edit_text(f"❌ Erreur lors du chargement : {e}")

        context.application.create_task(_load_from_stats())

    async def gtop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gtop [N] — Top N prédictions les plus fiables pour les N prochains jeux."""
        if not await self._perm(update, 'gtop'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text(
                "❌ Aucun jeu chargé.\nTapez /gload ou /gpredictload d'abord."
            )
            return

        args = context.args or []
        next_n = 30
        if args and args[0].isdigit():
            next_n = max(5, min(int(args[0]), 200))

        all_nums = sorted(int(g['numero']) for g in games)
        last_known = all_nums[-1]

        import asyncio as _asyncio
        from datetime import datetime as _dt

        msg = await update.message.reply_text(
            f"🔝 Calcul du TOP pour les <b>{next_n}</b> prochains jeux…\n"
            f"(#N{last_known+1} → #N{last_known+next_n})",
            parse_mode='HTML'
        )

        entries = generate_top_predictions(games, next_n=next_n, min_confidence=38)
        await msg.delete()

        if not entries:
            await update.message.reply_text(
                "❌ Aucune prédiction fiable trouvée.\n"
                "Essayez d'augmenter N ou de charger plus de jeux."
            )
            return

        heure = _dt.now().strftime('%H:%M')
        nb_games = len(games)
        lines = [
            f"🔝 <b>TOP PRÉDICTIONS</b>  ({heure})",
            f"🎲 {nb_games} jeux  |  #N{last_known+1} → #N{last_known+next_n}",
            f"🎯 {len(entries)} prédiction(s) classées par confiance",
            "",
        ]

        from predictor import _DISPLAY_NAMES
        for rank, (num, notation, conf, cat_name) in enumerate(entries, 1):
            clean = _DISPLAY_NAMES.get(cat_name,
                    cat_name.lstrip('🏆📊🎴👤🏦📈📉↔️♠️♥️♦️♣️🤝🃏 '))
            bar_filled = round(conf / 10)
            bar = '█' * bar_filled + '░' * (10 - bar_filled)
            lines.append(f"<b>{rank:>2}.</b> #N{num}  <b>{notation}</b>  {conf}%  <code>{bar}</code>")
            lines.append(f"     <i>{clean}</i>")

        full_text = '\n'.join(lines)

        chunk_size = 4000
        if len(full_text) <= chunk_size:
            await update.message.reply_text(full_text, parse_mode='HTML')
        else:
            parts = []
            current = []
            current_len = 0
            for line in lines:
                if current_len + len(line) + 1 > chunk_size:
                    parts.append('\n'.join(current))
                    current = []
                    current_len = 0
                current.append(line)
                current_len += len(line) + 1
            if current:
                parts.append('\n'.join(current))
            for part in parts:
                await update.message.reply_text(part, parse_mode='HTML')
                await _asyncio.sleep(0.3)

    async def gpredict(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/gpredict N1 N2 — Liste de prédictions par catégorie pour les jeux N1 à N2."""
        if not await self._perm(update, 'gpredict'):
            return
        games = get_analyzed_games()
        if not games:
            await update.message.reply_text(
                "❌ Aucun jeu chargé.\n"
                "Tapez /gload ou /gpredictload d'abord."
            )
            return

        raw_args = context.args if context.args else []

        # Extraire options de date si présentes
        num_kw, _, from_date_gp, to_date_gp = parse_search_options(raw_args)
        games = _filter_games_by_date(games, from_date_gp, to_date_gp)
        if not games:
            await update.message.reply_text(
                "❌ Aucun jeu dans cette plage de dates. Vérifiez les paramètres from:/to:."
            )
            return

        args = num_kw  # arguments restants (numéros)
        all_nums = sorted(int(g['numero']) for g in games)
        last_known = all_nums[-1]

        from_num = to_num = None
        if len(args) >= 2 and args[0].isdigit() and args[1].isdigit():
            from_num = int(args[0])
            to_num = int(args[1])
        elif len(args) == 1 and args[0].isdigit():
            n = int(args[0])
            if n <= 100:
                from_num = last_known + 1
                to_num = last_known + n
            else:
                from_num = n
                to_num = n + 19
        else:
            date_hint = ''
            if from_date_gp and to_date_gp:
                date_hint = (f"\n📅 Filtre actif : {from_date_gp.strftime('%d/%m/%Y')} → "
                             f"{to_date_gp.strftime('%d/%m/%Y')} ({len(games)} jeux)")
            await update.message.reply_text(
                "📋 <b>Usage de /gpredict</b>\n\n"
                "<code>/gpredict N1 N2</code> — de #N1 à #N2\n"
                "<code>/gpredict N</code> — les N prochains jeux\n"
                "<code>/gpredict N1 N2 from:2026-02-20 to:2026-02-23</code> — sur plage de dates\n\n"
                f"Dernier jeu connu : <b>#N{last_known}</b>{date_hint}\n\n"
                f"Exemples :\n"
                f"  <code>/gpredict {last_known+1} {last_known+50}</code>\n"
                f"  <code>/gpredict 30</code> — les 30 prochains\n"
                f"  <code>/gpredict 30 from:2026-02-20 to:2026-02-23</code>",
                parse_mode='HTML'
            )
            return

        if from_num > to_num:
            from_num, to_num = to_num, from_num

        nb_range = to_num - from_num + 1
        if nb_range > 200:
            await update.message.reply_text(
                f"⚠️ Plage trop grande ({nb_range} jeux).\n"
                f"Maximum : 200 jeux par appel."
            )
            return

        msg = await update.message.reply_text(
            f"🔮 Analyse de <b>{nb_range}</b> jeu(x) en cours…\n"
            f"Plage : <b>#N{from_num}</b> → <b>#N{to_num}</b>",
            parse_mode='HTML'
        )

        from datetime import datetime as _dt
        import asyncio as _asyncio

        nb_games = len(games)
        cat_results = generate_category_list(games, from_num, to_num, min_confidence=35)

        await msg.delete()

        if not cat_results:
            await update.message.reply_text(
                "❌ Aucune prédiction trouvée pour cette plage.\n\n"
                "Conseils :\n"
                "• Élargissez la plage (#N plus éloignés)\n"
                "• Chargez plus de jeux avec /gpredictload\n"
                "• Le seuil de confiance est de 35% — les catégories analysées "
                "ne montrent pas encore de retard significatif."
            )
            return

        # En-tête
        heure = _dt.now().strftime('%H:%M')
        total_preds = sum(len(v['nums']) for v in cat_results.values())
        header = (
            f"🔮 <b>LISTE DE PRÉDICTIONS</b>\n"
            f"⏰ {heure}  |  🎲 {nb_games} jeux analysés\n"
            f"📐 Plage : <b>#N{from_num}</b> → <b>#N{to_num}</b>\n"
            f"🎯 <b>{total_preds}</b> prédiction(s) en <b>{len(cat_results)}</b> catégorie(s)\n"
            f"<i>Chaque numéro n'apparaît que dans une seule catégorie.</i>"
        )
        await update.message.reply_text(header, parse_mode='HTML')
        await _asyncio.sleep(0.2)

        # Un message par catégorie + résumé final
        msgs = format_category_list(cat_results, nb_games, from_num, to_num)
        for m in msgs:
            await update.message.reply_text(m, parse_mode='HTML')
            await _asyncio.sleep(0.3)

    # ── Recherche publique par jour (/recherche) ─────────────────────────────

    async def recherche(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/recherche — Recherche publique dans les jeux d'une journée. Ouvert à tous."""
        uid = update.effective_user.id
        channels = get_channels()

        if not channels:
            await update.message.reply_text(
                "❌ <b>Aucun canal configuré.</b>\n\n"
                "Un administrateur doit d'abord ajouter un canal avec /addchannel.",
                parse_mode='HTML'
            )
            return

        # Construire le menu numéroté des canaux
        lines = []
        for i, ch in enumerate(channels, 1):
            mark = "▶️" if ch.get('active') else "  "
            name = ch.get('name') or ch['id']
            lines.append(f"  {mark} <b>{i}.</b> {html.escape(name)}")
        menu_text = '\n'.join(lines)

        _ds_save(uid, {
            'step': 'wait_channel',
            'channels_snapshot': [{'id': ch['id'], 'name': ch.get('name', ch['id'])}
                                   for ch in channels],
            'channel_id': '',
            'channel_name': '',
            'date_str': '',
            'date_display': '',
            'kw': '',
            'results': [],
        })

        await update.message.reply_text(
            "🔍 <b>RECHERCHE PAR JOUR</b>\n\n"
            "📡 <b>Choisissez le canal à analyser :</b>\n\n"
            f"{menu_text}\n\n"
            "Tapez le <b>numéro</b> du canal (ex : <code>1</code>)\n"
            "Tapez <b>annuler</b> pour quitter.",
            parse_mode='HTML'
        )

    def _dsearch_costume(self, game: dict) -> str:
        """Extrait les emojis de costumes présents dans un jeu (J + B combinés)."""
        all_suits = {'♠', '♥', '♦', '♣'}
        missing_j = set(game.get('missing_j') or [])
        missing_b = set(game.get('missing_b') or [])
        present = (all_suits - missing_j) | (all_suits - missing_b)
        order = ['♠', '♥', '♦', '♣']
        return ''.join(_DS_SUIT_EMOJI.get(s, s) for s in order if s in present)

    def _dsearch_filter_games(self, date_iso: str, kw: str) -> list:
        """Filtre les jeux chargés par date (YYYY-MM-DD) et mot-clé.
        Retourne liste de [numero, costume_str] (JSON-sérialisable)."""
        games = get_analyzed_games()
        if not games:
            return []

        kw_low = kw.strip().lower()
        results = []

        for g in games:
            # Filtre par date (comparer les 10 premiers chars "YYYY-MM-DD")
            if date_iso:
                raw_date = str(g.get('date', ''))
                if not raw_date or raw_date[:10] != date_iso:
                    continue

            # Filtre par mot-clé dans le texte brut
            raw = g.get('raw', '')
            if kw_low and kw_low not in raw.lower():
                continue

            numero = str(g.get('numero', '?'))
            costume = self._dsearch_costume(g)
            results.append([numero, costume])  # liste pour JSON

        return results

    async def handle_dsearch_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gère les entrées texte de la conversation /recherche (état persistant fichier)."""
        uid = update.effective_user.id
        state = _ds_load(uid)
        if not state:
            return

        text = update.message.text.strip()

        # Annulation universelle
        if text.lower() in ('annuler', 'cancel', '/cancel', 'quitter', 'stop'):
            _ds_clear(uid)
            await update.message.reply_text("❌ Recherche annulée.")
            return

        step = state['step']

        # ── Étape 0 : sélection du canal ─────────────────────────────────────
        if step == 'wait_channel':
            channels_snap = state.get('channels_snapshot', [])
            if not text.isdigit() or not (1 <= int(text) <= len(channels_snap)):
                await update.message.reply_text(
                    f"⚠️ Tapez un numéro entre <b>1</b> et <b>{len(channels_snap)}</b>.",
                    parse_mode='HTML'
                )
                return
            idx = int(text) - 1
            chosen = channels_snap[idx]
            state['channel_id'] = chosen['id']
            state['channel_name'] = chosen['name']
            state['step'] = 'wait_date'
            _ds_save(uid, state)
            await update.message.reply_text(
                f"✅ Canal : <b>{html.escape(chosen['name'])}</b>\n\n"
                "📅 <b>Quelle date voulez-vous analyser ?</b>\n\n"
                "Formats acceptés :\n"
                "  <code>10/03/2026</code>\n"
                "  <code>2026-03-10</code>\n"
                "  <code>10-03-2026</code>",
                parse_mode='HTML'
            )
            return

        # ── Étape 1 : réception de la date ──────────────────────────────────
        if step == 'wait_date':
            dt = parse_date(text)
            if not dt:
                await update.message.reply_text(
                    "⚠️ Date non reconnue. Essayez :\n"
                    "  <code>10/03/2026</code>  ou  <code>2026-03-10</code>",
                    parse_mode='HTML'
                )
                return
            state['date_str'] = dt.strftime('%Y-%m-%d')       # ISO pour la comparaison
            state['date_display'] = dt.strftime('%d/%m/%Y')   # Pour l'affichage
            state['step'] = 'wait_kw'
            _ds_save(uid, state)
            await update.message.reply_text(
                f"✅ Date : <b>{state['date_display']}</b>\n\n"
                f"🔎 <b>Que voulez-vous rechercher dans cette journée ?</b>\n\n"
                f"Exemples : <code>joueur</code>  <code>banquier</code>  "
                f"<code>♠</code>  <code>K</code>  <code>nul</code>\n\n"
                f"💡 La recherche couvre les jeux <b>#1 → #1440</b> de ce jour.",
                parse_mode='HTML'
            )
            return

        # ── Étape 2 : réception du mot-clé et lancement de la recherche ─────
        if step == 'wait_kw':
            kw = text
            state['kw'] = kw
            state['step'] = 'wait_again'
            _ds_save(uid, state)

            date_display = state.get('date_display', state.get('date_str', '?'))
            channel_id  = state.get('channel_id', '')
            channel_name = state.get('channel_name', channel_id)
            date_iso = state.get('date_str', '')

            msg = await update.message.reply_text(
                f"⏳ Recherche de <b>{html.escape(kw)}</b> "
                f"le <b>{date_display}</b> dans <b>{html.escape(channel_name)}</b>…\n"
                f"(peut prendre quelques secondes)",
                parse_mode='HTML'
            )

            found = []
            error_txt = None

            # ── Recherche live Telethon (tous les messages) ─────────────────
            try:
                import re as _re
                from datetime import timezone as _tz, timedelta as _td
                from datetime import datetime as _dt2

                # Fenêtre élargie de ±6h pour couvrir tous les fuseaux horaires
                day       = _dt2.strptime(date_iso, '%Y-%m-%d').replace(tzinfo=_tz.utc)
                from_date = day - _td(hours=6)
                to_date   = day + _td(hours=30)   # fin de journée + 6h de marge

                # Regex pour extraire le numéro de jeu #N794 → 794
                _NUM_RE = _re.compile(r'#[Nn](\d{1,4})')

                async def _prog(checked, total_found_so_far):
                    try:
                        await msg.edit_text(
                            f"⏳ Analyse en cours… <b>{checked}</b> messages parcourus "
                            f"(<b>{total_found_so_far}</b> trouvés)",
                            parse_mode='HTML'
                        )
                    except Exception:
                        pass

                # search_in_any_channel cherche dans TOUS les messages (prédictions,
                # résultats, jeux bruts, etc.) — pas seulement les jeux Baccarat structurés
                results_raw, _title, _cancelled = await scraper.search_in_any_channel(
                    channel_id,
                    keywords=[kw],
                    from_date=from_date,
                    to_date=to_date,
                    progress_callback=_prog,
                )

                # ── Extraction générique : fonctionne avec TOUT format ──────
                #
                # Principe :
                #   1. Trouver tous les nombres dans le texte (avec position)
                #   2. Trouver tous les costumes dans le texte (avec position)
                #   3. Associer chaque nombre au costume le plus proche
                #   4. Retenir la paire (nombre, costume) avec la plus petite distance
                #
                # Normalisation costume → emoji canonique
                _SUIT_NORM = {
                    '♠️': '♠️', '♠': '♠️',
                    '♥️': '❤️', '♥': '❤️', '❤️': '❤️',
                    '♦️': '♦️', '♦': '♦️',
                    '♣️': '♣️', '♣': '♣️',
                }
                # Regex : tous les nombres standalone (1-6 chiffres)
                _ALL_NUMS_RE  = _re.compile(r'(?<!\d)(\d{1,6})(?!\d)')
                # Regex : tous les costumes (avec ou sans variante Unicode)
                _ALL_SUITS_RE = _re.compile(r'♠️|♥️|♦️|♣️|❤️|♠|♥|♦|♣')

                def _extract_num_suit(text):
                    """
                    Extrait la paire (numero, costume) la plus pertinente
                    d'un texte quelconque, sans hypothèse sur le format.
                    """
                    # Tous les nombres avec leur position de début
                    nums  = [(m.start(), m.group()) for m in _ALL_NUMS_RE.finditer(text)]
                    # Tous les costumes avec leur position de début
                    suits = [(m.start(), _SUIT_NORM.get(m.group(), m.group()))
                             for m in _ALL_SUITS_RE.finditer(text)]

                    if not nums and not suits:
                        return None, None

                    # Si aucun costume → retourner juste le 1er nombre
                    if not suits:
                        return nums[0][1], '—'

                    # Si aucun nombre → retourner juste le 1er costume
                    if not nums:
                        return None, suits[0][1]

                    # Trouver la paire (num, suit) avec distance minimale
                    best_dist = float('inf')
                    best_num  = nums[0][1]
                    best_suit = suits[0][1]
                    for npos, nval in nums:
                        for spos, sval in suits:
                            dist = abs(spos - npos)
                            if dist < best_dist:
                                best_dist = dist
                                best_num  = nval
                                best_suit = sval

                    return best_num, best_suit

                for i, rec in enumerate(results_raw, 1):
                    raw_txt = rec['text'] if isinstance(rec, dict) else str(rec)

                    numero, costume = _extract_num_suit(raw_txt)

                    # Fallback numéro : index dans les résultats
                    if numero is None:
                        numero = str(i)
                    # Fallback costume
                    if costume is None:
                        costume = '—'

                    found.append([numero, costume])

            except Exception as e:
                err_str = str(e)
                if _is_auth_key_dup(e):
                    error_txt = _AUTH_KEY_DUP_MSG
                elif 'Non authentifié' in err_str or 'authorized' in err_str.lower():
                    error_txt = (
                        "🔒 Le bot n'est pas connecté à Telethon.\n"
                        "Un administrateur doit taper /connect pour activer la recherche live."
                    )
                else:
                    # Repli sur les jeux locaux
                    found = self._dsearch_filter_games(date_iso, kw)
                    if not found:
                        error_txt = (
                            f"⚠️ Recherche live échouée (<i>{html.escape(err_str[:80])}</i>).\n"
                            f"Aucun résultat dans les jeux locaux non plus."
                        )

            # Supprimer le message de progression
            try:
                await msg.delete()
            except Exception:
                pass

            if error_txt:
                await update.message.reply_text(error_txt, parse_mode='HTML')
                await update.message.reply_text(
                    "🔄 Voulez-vous faire une autre recherche ?\n"
                    "Tapez <b>oui</b> ou <b>non</b>.",
                    parse_mode='HTML'
                )
                return

            if not found:
                await update.message.reply_text(
                    f"❌ Aucun résultat pour <b>{html.escape(kw)}</b> "
                    f"le <b>{date_display}</b> dans <b>{html.escape(channel_name)}</b>.\n\n"
                    f"Conseil : vérifiez la date ou le terme de recherche.",
                    parse_mode='HTML'
                )
                await update.message.reply_text(
                    "🔄 Voulez-vous faire une autre recherche ?\n"
                    "Tapez <b>oui</b> pour continuer ou <b>non</b> pour quitter.",
                    parse_mode='HTML'
                )
                return

            # Accumulation dans la session
            state['results'].extend(found)
            _ds_save(uid, state)
            total_found = len(found)

            # Envoyer les 20 premiers en aperçu
            preview = found[:20]
            lines = [f"<code>{html.escape(str(num))}:{html.escape(str(cos))}</code>" for num, cos in preview]
            preview_text = (
                f"🎯 <b>{total_found} résultat(s)</b> pour "
                f"<b>{html.escape(kw)}</b> le <b>{date_display}</b>\n"
                f"📡 Canal : <b>{html.escape(channel_name)}</b>\n\n"
                + '\n'.join(lines)
            )
            if total_found > 20:
                preview_text += f"\n\n<i>…et {total_found - 20} autre(s) résultat(s).</i>"

            await update.message.reply_text(preview_text, parse_mode='HTML')

            await update.message.reply_text(
                "🔄 Voulez-vous faire une autre recherche ?\n"
                "Tapez <b>oui</b> pour continuer ou <b>non</b> pour recevoir le fichier.",
                parse_mode='HTML'
            )
            return

        # ── Étape 3 : continuer ou terminer ─────────────────────────────────
        if step == 'wait_again':
            answer = text.lower()

            if answer in ('oui', 'yes', 'o', 'y', '1'):
                channel_name = state.get('channel_name', state.get('channel_id', '?'))
                state['step'] = 'wait_date'
                _ds_save(uid, state)
                await update.message.reply_text(
                    f"📅 <b>Nouvelle recherche</b>\n"
                    f"📡 Canal conservé : <b>{html.escape(channel_name)}</b>\n\n"
                    "Quelle date voulez-vous analyser ?\n"
                    "<code>10/03/2026</code>  ou  <code>2026-03-10</code>",
                    parse_mode='HTML'
                )
                return

            if answer in ('non', 'no', 'n', '0', 'fin', 'terminer'):
                all_results = state.get('results', [])
                _ds_clear(uid)

                if not all_results:
                    await update.message.reply_text("ℹ️ Aucun résultat à exporter.")
                    return

                # Génération du fichier texte
                import io as _io
                lines = [f"{num}:{cos}" for num, cos in all_results]
                content = '\n'.join(lines)
                buf = _io.BytesIO(content.encode('utf-8'))
                buf.name = 'resultats_recherche.txt'
                buf.seek(0)

                caption = (
                    f"📄 <b>Résultats de la recherche</b>\n"
                    f"🔢 {len(all_results)} résultat(s) au total\n"
                    f"Format : <code>numero:costume</code>"
                )
                await update.message.reply_document(
                    document=buf,
                    filename='resultats_recherche.txt',
                    caption=caption,
                    parse_mode='HTML'
                )
                return

            # Réponse non reconnue
            await update.message.reply_text(
                "❓ Tapez <b>oui</b> pour continuer ou <b>non</b> pour recevoir le fichier.",
                parse_mode='HTML'
            )

    # ── Fin recherche publique ────────────────────────────────────────────────

    async def clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._perm(update, 'clear'):
            return
        clear_all()
        await update.message.reply_text("🗑️ Effacé !")

handlers = Handlers()

async def _reset_state_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Efface tous les états d'attente dès qu'une commande est reçue.
    Enregistré en groupe -1 pour s'exécuter avant tous les autres handlers."""
    if update.effective_user:
        _clear_waits(update.effective_user.id)


def setup_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    # Priorité haute : efface tout état d'attente à chaque nouvelle commande
    app.add_handler(
        MessageHandler(filters.COMMAND, _reset_state_on_command),
        group=-1
    )

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("recherche", handlers.recherche))
    app.add_handler(CommandHandler("menu", handlers.menu))
    app.add_handler(CallbackQueryHandler(handlers.handle_menu_callback, pattern=r"^menu:"))
    app.add_handler(CommandHandler("connect", handlers.connect))
    app.add_handler(CommandHandler("code", handlers.code))
    app.add_handler(CommandHandler("disconnect", handlers.disconnect))
    app.add_handler(CommandHandler("sync", handlers.sync))
    app.add_handler(CommandHandler("fullsync", handlers.fullsync))
    app.add_handler(CommandHandler("report", handlers.report))
    app.add_handler(CommandHandler("filter", handlers.filter_cmd))
    app.add_handler(CommandHandler("stats", handlers.stats))
    app.add_handler(CommandHandler("search", handlers.search))
    app.add_handler(CommandHandler("searchcard", handlers.searchcard))
    app.add_handler(CommandHandler("clear", handlers.clear))
    app.add_handler(CommandHandler("addchannel", handlers.addchannel))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("cancel", handlers.cancel))
    app.add_handler(CommandHandler("channels", handlers.channels))
    app.add_handler(CommandHandler("usechannel", handlers.usechannel))
    app.add_handler(CommandHandler("removechannel", handlers.removechannel))
    app.add_handler(CommandHandler("helpcl", handlers.helpcl))
    app.add_handler(CommandHandler("hsearch", handlers.hsearch))
    app.add_handler(CommandHandler("documentation", handlers.documentation))
    app.add_handler(CommandHandler("predictsetup", handlers.predictsetup))
    app.add_handler(CommandHandler("gpredictload", handlers.gpredictload))
    app.add_handler(CommandHandler("gtop", handlers.gtop))
    app.add_handler(CommandHandler("gpredict", handlers.gpredict))
    app.add_handler(CommandHandler("addadmin", handlers.addadmin))
    app.add_handler(CommandHandler("setperm", handlers.setperm))
    app.add_handler(CommandHandler("removeadmin", handlers.removeadmin))
    app.add_handler(CommandHandler("admins", handlers.listadmins))
    app.add_handler(CommandHandler("myid", handlers.myid))
    app.add_handler(CommandHandler("ganalyze", handlers.ganalyze))
    app.add_handler(CommandHandler("gload", handlers.gload))
    app.add_handler(CommandHandler("gstats", handlers.gstats))
    app.add_handler(CommandHandler("gvictoire", handlers.gvictoire))
    app.add_handler(CommandHandler("gparite", handlers.gparite))
    app.add_handler(CommandHandler("gstructure", handlers.gstructure))
    app.add_handler(CommandHandler("gplusmoins", handlers.gplusmoins))
    app.add_handler(CommandHandler("gcostume", handlers.gcostume))
    app.add_handler(CommandHandler("gvaleur", handlers.gvaleur))
    app.add_handler(CommandHandler("gcycle", handlers.gcycle))
    app.add_handler(CommandHandler("gcycleauto", handlers.gcycleauto))
    app.add_handler(CommandHandler("gecartmax", handlers.gecartmax))
    app.add_handler(CommandHandler("gclear", handlers.gclear))
    app.add_handler(MessageHandler(filters.Document.PDF, handlers.handle_pdf))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handlers.handle_text_input
    ))

    return app
