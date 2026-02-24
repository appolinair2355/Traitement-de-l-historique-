import os
import asyncio
import logging
import html
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
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
                       build_predict_data, format_global_summary)
from scraper import scraper
from auth_manager import auth_manager
from pdf_generator import generate_pdf, generate_search_pdf, generate_channel_search_pdf
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


def _clear_waits(uid: int):
    """Efface tous les états d'attente d'un utilisateur.
    Appelé automatiquement dès qu'une nouvelle commande est reçue,
    pour éviter qu'un ancien état bloque le nouveau flux."""
    _waiting_for_channel.pop(uid, None)
    _waiting_for_game.pop(uid, None)
    _waiting_for_perm.pop(uid, None)
    _waiting_for_helpcl.pop(uid, None)
    _waiting_for_predict.pop(uid, None)

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
        [InlineKeyboardButton("🔍 Recherche",      callback_data="menu:recherche"),
         InlineKeyboardButton("🔮 Prédiction",     callback_data="menu:prediction")],
        [InlineKeyboardButton("📊 Statistiques",   callback_data="menu:statistiques"),
         InlineKeyboardButton("📡 Canaux",          callback_data="menu:canaux")],
        [InlineKeyboardButton("📚 Documentation",  callback_data="menu:doc")],
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
        "<b>Autres :</b>\n"
        "  <code>/gpredictload</code> — Charger depuis canaux de stats\n"
        "  <code>/ganalyze</code> — Analyser un enregistrement (copier-coller)\n"
        "  <code>/predictsetup</code> — Configurer les canaux de prédiction\n\n"
        "💡 <i>Chaque prédiction analyse les manquements par catégorie :\n"
        "V1/V2, Pa/I, costumes ♠♥♦♣, valeurs A/K/Q/Valet, structures 2K/3K</i>"
    ),
    "statistiques": (
        "📊 <b>STATISTIQUES</b>\n\n"
        "<b>/gstats</b> — Résumé complet des jeux chargés\n\n"
        "<b>/gvictoire</b> — Victoires par résultat\n"
        "  <code>/gvictoire joueur</code>  <code>/gvictoire banquier</code>  <code>/gvictoire nul</code>\n\n"
        "<b>/gparite</b> — Parité du total\n"
        "  <code>/gparite pair</code>  <code>/gparite impair</code>\n\n"
        "<b>/gstructure</b> — Structure des cartes (2/2, 2/3, 3/2, 3/3)\n"
        "  <code>/gstructure 2/3</code>\n\n"
        "<b>/gplusmoins</b> — Plus/Moins de 6,5 ou 4,5\n"
        "  <code>/gplusmoins j plus</code>  <code>/gplusmoins b moins</code>\n\n"
        "<b>/gcostume</b> — Costumes manquants par main\n"
        "  <code>/gcostume ♠ j</code>  <code>/gcostume ♥ b</code>\n\n"
        "<b>/gecartmax</b> — Écart maximum dans toutes les catégories\n\n"
        "<b>/gclear</b> — Effacer les jeux chargés"
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
    "doc": (
        "📚 <b>DOCUMENTATION</b>\n\n"
        "Tapez <b>/documentation</b> pour recevoir le guide complet\n"
        "avec des exemples détaillés pour chaque commande.\n\n"
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
        'gecartmax':    'Paires ayant l\'écart maximum par catégorie',
        'predictsetup': 'Configurer les canaux de prédiction',
        'gpredictload': 'Charger les jeux depuis les canaux de stats',
        'gpredict':     'Générer des prédictions par catégorie (N1 → N2)',
        'searchcard':   'Rechercher les jeux par valeur de carte (A, K, Q, J)',
        'documentation':'Guide complet avec exemples d\'utilisation',
    }

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
        main = is_main_admin(uid)

        if section == "accueil":
            channels = get_channels()
            ch_lines = []
            for ch in channels:
                mark = "▶️" if ch.get('active') else "○"
                name = ch.get('name') or str(ch['id'])
                ch_lines.append(f"  {mark} <b>{name}</b>")
            ch_block = ("\n".join(ch_lines)) if ch_lines else "  <i>Aucun canal configuré</i>"
            text = (
                "🎯 <b>Bot VIP KOUAMÉ &amp; JOKER</b>\n\n"
                f"📡 <b>Canaux :</b>\n{ch_block}\n\n"
                "Choisissez une section :"
            )
            await query.edit_message_text(text, parse_mode='HTML',
                                          reply_markup=_main_menu_keyboard(main))
            return

        if section not in _MENU_SECTIONS:
            await query.answer("Section inconnue.")
            return

        # Filtrer le contenu admin pour les sous-admins
        if section == "admin" and not main:
            await query.answer("❌ Réservé à l'administrateur principal.")
            return

        text = _MENU_SECTIONS[section]
        await query.edit_message_text(text, parse_mode='HTML',
                                      reply_markup=_back_keyboard())

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
        if not is_admin(uid):
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
            "🎴 <b>ANALYSE BACCARAT</b>\n"
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
            "  /gcostume ♠|♥|♦|♣ j|b — Probabilité costume par main\n"
            "  /gecartmax — Paires avec l'écart maximum (toutes catégories)"
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
        """/documentation — Guide complet avec exemples pour chaque commande."""
        uid = update.effective_user.id
        if not is_admin(uid):
            return

        main = is_main_admin(uid)
        perms = list(ALL_COMMANDS) if main else get_admin_permissions(uid)

        parts = []

        parts.append(
            "📚 <b>DOCUMENTATION — GUIDE D'UTILISATION</b>\n"
            "Exemples concrets pour chaque commande disponible.\n"
        )

        # ── Canaux ──────────────────────────────────────────────────────────
        if any(c in perms for c in ['helpcl', 'addchannel', 'channels', 'usechannel']):
            parts.append(
                "📡 <b>GESTION DES CANAUX</b>\n\n"
                "<b>/helpcl</b> — Menu interactif pour choisir le canal d'analyse\n"
                "  → Le bot affiche une liste numérotée\n"
                "  → Tapez <code>1</code> pour sélectionner le premier canal\n"
                "  → Tapez <code>sortir</code> pour quitter sans changer\n\n"
                "<b>/addchannel</b> — Ajouter un canal\n"
                "  → Le bot vous demande l'ID ou @username\n"
                "  → Ex : <code>-1001234567890</code> ou <code>@moncanal</code>\n\n"
                "<b>/channels</b> — Voir tous les canaux enregistrés\n\n"
                "<b>/usechannel -1001234567890</b> — Activer un canal par son ID\n\n"
                "<b>/removechannel -1001234567890</b> — Supprimer un canal"
            )

        # ── Recherche historique ──────────────────────────────────────────────
        if 'hsearch' in perms:
            parts.append(
                "🔍 <b>RECHERCHE DANS L'HISTORIQUE</b>\n\n"
                "<b>/hsearch</b> <code>mot1 mot2</code> — Chercher des mots dans le canal actif\n"
                "  Ex : <code>/hsearch GAGNÉ Cœur</code>\n"
                "  Ex : <code>/hsearch PERDU limit:500</code>\n"
                "  Ex : <code>/hsearch Prédiction from:2024-12-01</code>\n"
                "  Ex : <code>/hsearch Numéro from:2025-01-15 10:00 limit:200</code>\n\n"
                "  Options combinables :\n"
                "  • <code>limit:N</code> — limiter à N messages analysés\n"
                "  • <code>from:AAAA-MM-JJ</code> ou <code>from:AAAA-MM-JJ HH:MM</code>\n\n"
                "  Le résultat s'exporte automatiquement en PDF."
            )

        # ── Synchronisation ───────────────────────────────────────────────────
        if any(c in perms for c in ['sync', 'fullsync', 'search', 'report']):
            parts.append(
                "💾 <b>SYNCHRONISATION ET DONNÉES LOCALES</b>\n\n"
                "<b>/sync</b> — Récupérer les nouveaux messages depuis la dernière synchro\n\n"
                "<b>/fullsync</b> — Récupérer tout l'historique (peut être long)\n\n"
                "<b>/stats</b> — Nombre de prédictions stockées\n\n"
                "<b>/report</b> — Générer un PDF de toutes les prédictions\n\n"
                "<b>/search</b> <code>Cœur GAGNÉ</code> — Chercher et exporter en PDF\n"
                "  Options : <code>limit:N</code>  <code>from:AAAA-MM-JJ</code>\n\n"
                "<b>📎 Envoyer un PDF au bot</b> — Il en extrait tous les numéros\n"
                "  automatiquement et affiche la liste des prédictions trouvées."
            )

        # ── Analyse Baccarat ──────────────────────────────────────────────────
        if any(c in perms for c in ['gload', 'gstats', 'gvictoire', 'gstructure']):
            parts.append(
                "🎴 <b>ANALYSE BACCARAT — CHARGEMENT</b>\n\n"
                "<b>/gload from:2025-01-01</b> — Charger les jeux depuis le 1er janvier 2025\n"
                "<b>/gload from:2025-02-10 08:00</b> — Depuis le 10 fév. à 8h\n"
                "<b>/gload limit:200</b> — Charger les 200 derniers jeux\n\n"
                "⚠️ <i>Une date ou une limite est obligatoire pour éviter\n"
                "de scanner tout l'historique du canal.</i>\n\n"
                "<b>/gstats</b> — Résumé statistique des jeux chargés\n"
                "<b>/gclear</b> — Effacer les jeux chargés en mémoire\n"
                "<b>/ganalyze</b> — Coller un enregistrement pour analyse instantanée\n"
                "  Ex de format : <code>#N794. ✅3(K♦️4♦️9♦️) - 1(J♦️10♥️A♠️) #T4</code>"
            )

        if any(c in perms for c in ['gvictoire', 'gparite', 'gstructure', 'gplusmoins', 'gcostume', 'gecartmax']):
            parts.append(
                "🎴 <b>ANALYSE BACCARAT — CATÉGORIES</b>\n\n"
                "<b>/gvictoire</b> — Tous les résultats (Joueur / Banquier / Nul)\n"
                "<b>/gvictoire joueur</b> — Uniquement les victoires Joueur\n"
                "<b>/gvictoire banquier</b> — Uniquement les victoires Banquier\n"
                "<b>/gvictoire nul</b> — Uniquement les matchs nuls\n\n"
                "<b>/gparite</b> — Résultats pair et impair\n"
                "<b>/gparite pair</b> — Uniquement les totaux pairs\n\n"
                "<b>/gstructure</b> — Structures 2/2, 2/3, 3/2, 3/3 + bilan Banquier 2K/3K\n"
                "<b>/gstructure 2/3</b> — Uniquement la structure 2/3\n"
                "  ↳ Le bilan montre aussi :\n"
                "     • Banquier 2K = jeux où Banquier avait 2 cartes (2/2 + 3/2)\n"
                "     • Banquier 3K = jeux où Banquier avait 3 cartes (2/3 + 3/3)\n\n"
                "<b>/gplusmoins</b> — Plus/Moins pour Joueur et Banquier\n"
                "<b>/gplusmoins j plus</b> — Joueur Plus de 6,5\n"
                "<b>/gplusmoins b moins</b> — Banquier Moins de 4,5\n\n"
                "<b>/gcostume</b> — Costumes manquants (toutes mains)\n"
                "<b>/gcostume ♠ j</b> — Pique manquant chez le Joueur\n"
                "<b>/gcostume ♥ b</b> — Cœur manquant chez le Banquier\n\n"
                "<b>/gecartmax</b> — Paires de numéros formant l'écart le plus grand\n"
                "  dans chacune des 23 catégories + bilan global permanent"
            )

        # ── Administration ────────────────────────────────────────────────────
        if main:
            parts.append(
                "👥 <b>ADMINISTRATION</b>\n\n"
                "<b>/addadmin 123456789</b> — Ajouter un admin\n"
                "  → Le bot affiche la liste numérotée des commandes\n"
                "  → Tapez ex : <code>1,3,5</code> ou <code>1-8,13</code>\n"
                "  → L'admin ne verra et ne pourra utiliser que ces commandes\n\n"
                "<b>/setperm 123456789</b> — Modifier les permissions d'un admin existant\n"
                "  → Même menu numéroté que /addadmin\n\n"
                "<b>/removeadmin 123456789</b> — Supprimer définitivement un admin\n\n"
                "<b>/admins</b> — Voir tous les admins et leurs commandes autorisées\n\n"
                "<b>/myid</b> — Afficher votre propre Telegram ID\n"
                "  → Utile pour communiquer votre ID à l'admin principal"
            )

        # ── Astuces générales ─────────────────────────────────────────────────
        parts.append(
            "💡 <b>ASTUCES</b>\n\n"
            "• /cancel — Annule n'importe quelle opération en cours\n"
            "• Après /gload, les commandes /gvictoire, /gstructure etc. travaillent\n"
            "  sur les jeux chargés jusqu'au prochain /gclear ou /gload\n"
            "• Les listes de numéros (détail) s'effacent après 10 secondes\n"
            "• Les bilans restent en permanence pour référence\n"
            "• /helpcl est le moyen le plus rapide de changer de canal"
        )

        for i, part in enumerate(parts):
            await update.message.reply_text(part, parse_mode='HTML')
            if i < len(parts) - 1:
                import asyncio as _asyncio
                await _asyncio.sleep(0.3)

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
        
        msg = await update.message.reply_text("📄 Génération PDF...")
        
        try:
            pdf_path = generate_pdf(predictions, context.user_data.get('filters'))
            
            with open(pdf_path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=ADMIN_ID,
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
                                chat_id=ADMIN_ID,
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
                        chat_id=ADMIN_ID,
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

        async def _do_analyze():
            tmp_path = f"/tmp/analyse_{doc.file_id}.pdf"
            try:
                # Télécharger le PDF
                file = await context.bot.get_file(doc.file_id)
                await file.download_to_drive(tmp_path)

                await msg.edit_text("🔍 Extraction des données du PDF...")

                results, raw_sample = analyze_pdf(tmp_path)

                if not results:
                    await msg.edit_text(
                        "❌ Aucun numéro prédit trouvé dans ce PDF.\n\n"
                        "Assurez-vous que le PDF contient des prédictions au format:\n"
                        "`PRÉDICTION #X` et `Couleur: Y`",
                        parse_mode='Markdown'
                    )
                    return

                # Compter les doublons
                duplicates = [r for r in results if r['count'] > 1]
                unique_count = len(results)
                total_count = sum(r['count'] for r in results)

                # Filtrer : seulement les numéros qui apparaissent au moins 4 fois
                filtered = [r for r in results if r['count'] >= 4]

                # Construire la réponse au format demandé
                lines = ["Joueur 😉😌", ""]

                for r in filtered:
                    emoji = r.get('couleur_emoji', '?')
                    lines.append(f"{r['numero']} [{emoji}]")

                if not filtered:
                    lines.append("Aucun numéro n'apparaît 4 fois ou plus.")

                lines.append("")
                lines.append(f"Total : {len(filtered)} numéros (≥4 occurrences)")

                response = '\n'.join(lines)

                # Si trop long, envoyer en fichier texte
                if len(response) > 4000:
                    txt_path = f"/tmp/analyse_result_{doc.file_id}.txt"
                    with open(txt_path, 'w', encoding='utf-8') as f:
                        f.write(response)
                    with open(txt_path, 'rb') as f:
                        await context.bot.send_document(
                            chat_id=ADMIN_ID,
                            document=f,
                            caption=f"Joueur 😉😌 — {unique_count} numéros extraits",
                            filename="predictions.txt"
                        )
                    os.remove(txt_path)
                    await msg.delete()
                else:
                    await msg.edit_text(response)

            except Exception as e:
                logger.error(f"PDF analyze error: {e}")
                try:
                    await msg.edit_text(f"❌ Erreur lors de l'analyse: {str(e)[:300]}")
                except Exception:
                    pass
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

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

        lines = ["📡 *Canaux de recherche enregistrés :*\n"]
        for ch in channels:
            mark = "▶️ *ACTIF*" if ch.get('active') else "⬜"
            name = ch.get('name') or ch['id']
            lines.append(f"{mark} {html.escape(name)} — `{ch['id']}`")

        lines.append("\n*Pour changer de canal actif :*")
        lines.append("`/usechannel <ID>`  ex: /usechannel -1001234567890")
        lines.append("`/removechannel <ID>`  pour supprimer")

        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

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
        await update.message.reply_text(f"✅ Canal actif : *{html.escape(name)}* (`{channel_id}`)", parse_mode='Markdown')

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

        msg = await update.message.reply_text(f"🔄 Vérification du canal `{html.escape(text)}`...", parse_mode='Markdown')

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
                await msg.edit_text(
                    f"❌ Impossible d'accéder à ce canal : {str(e)[:200]}\n\n"
                    "Vérifiez que le compte Telegram est membre de ce canal.",
                    parse_mode='Markdown'
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
        ]

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

        def find_max_pair(nums):
            if len(nums) < 2:
                return None, 0
            s = sorted(int(n) for n in nums)
            max_diff, pair = 0, (s[0], s[1])
            for i in range(len(s) - 1):
                diff = s[i + 1] - s[i]
                if diff > max_diff:
                    max_diff, pair = diff, (s[i], s[i + 1])
            return pair, max_diff

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
        ]

        detail_lines = ["🔍 <b>PAIRES D'ÉCART MAXIMUM PAR CATÉGORIE</b>\n"]
        bilan_lines = []

        for label, nums in all_categories:
            if not nums:
                continue
            pair, diff = find_max_pair(nums)
            if diff == 0:
                continue
            detail_lines.append(f"<b>{label}</b>")
            detail_lines.append(f"  N° {pair[0]}  →  N° {pair[1]}  =  <b>{diff}</b>\n")
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
        """Routeur de texte : canal, helpcl, predict, permissions ou analyse de jeu."""
        uid = update.effective_user.id
        if _waiting_for_helpcl.get(uid):
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
    app.add_handler(CommandHandler("gecartmax", handlers.gecartmax))
    app.add_handler(CommandHandler("gclear", handlers.gclear))
    app.add_handler(MessageHandler(filters.Document.PDF, handlers.handle_pdf))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handlers.handle_text_input
    ))

    return app
