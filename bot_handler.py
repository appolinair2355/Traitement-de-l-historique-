import os
import asyncio
import logging
import html
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
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
                     ALL_COMMANDS)
from game_analyzer import (parse_game, format_analysis, build_category_stats,
                           format_ecarts, normalize_suit, SUIT_EMOJI)
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
    """Sépare les mots-clés des options limit: et from:/depuis:.

    Retourne (keywords, limit, from_date).
    Options reconnues :
      limit:500              → analyser 500 derniers messages
      from:2024-01-15        → depuis cette date
      from:2024-01-15 10:30  → date + heure (espace accepté)
      from:2024-01-15T10:30  → date + heure (T accepté)
      depuis:2024-01-15      → alias de from:
    """
    import re as _re
    keywords = []
    limit = None
    from_date = None
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
            # Si l'arg suivant ressemble à une heure HH:MM, on l'inclut dans la date
            if i + 1 < len(args) and _re.match(r'^\d{1,2}:\d{2}$', args[i + 1]):
                date_val += ' ' + args[i + 1]
                i += 1
            from_date = parse_date(date_val)
        else:
            keywords.append(arg)
        i += 1
    return keywords, limit, from_date

# État de la conversation : attend un ID de canal de l'admin
_waiting_for_channel = {}
# État : attend un enregistrement de jeu pour analyse
_waiting_for_game = {}
# Flags d'annulation par utilisateur pour les recherches en cours
_search_cancel: dict[int, bool] = {}
# État : attend la sélection de commandes pour un nouvel admin
# {main_admin_uid: {'target_uid': int, 'action': 'add'|'update'}}
_waiting_for_perm: dict[int, dict] = {}

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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return

        connected = "✅ Connecté" if auth_manager.is_connected() else "❌ Non connecté"
        channels = get_channels()
        active = get_active_channel()

        if channels:
            ch_lines = []
            for ch in channels:
                mark = "▶️" if ch.get('active') else "  "
                name = ch.get('name') or ch['id']
                ch_lines.append(f"{mark} {name} (`{ch['id']}`)")
            ch_info = "\n".join(ch_lines)
        else:
            ch_info = "Aucun canal ajouté"

        await update.message.reply_text(
            f"🎯 *Bot VIP KOUAMÉ & JOKER*\n\n"
            f"Status: {connected}\n"
            f"Numéro: `{USER_PHONE}`\n\n"
            f"📡 *Canaux configurés :*\n{ch_info}\n\n"
            f"Tapez /help pour voir toutes les commandes organisées par domaine.",
            parse_mode='Markdown'
        )

        # Proposer d'ajouter un canal si aucun n'est configuré
        if not channels:
            await update.message.reply_text(
                "👆 Vous n'avez aucun canal de recherche configuré.\n\n"
                "Envoyez l'ID du canal à analyser (ex: `-1001234567890`).\n"
                "Ou tapez /addchannel pour commencer."
            )
            _waiting_for_channel[update.effective_user.id] = True
    
    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/help — Liste toutes les commandes par domaine."""
        uid = update.effective_user.id
        if not is_admin(uid):
            return

        main = is_main_admin(uid)

        # Pour un sous-admin : afficher uniquement ses commandes autorisées
        if not main:
            perms = get_admin_permissions(uid)
            cmd_lines = '\n'.join(f'• /{c}' for c in perms) if perms else '_Aucune commande accordée._'
            await update.message.reply_text(
                f"📖 <b>VOS COMMANDES AUTORISÉES</b>\n\n{cmd_lines}\n\n"
                f"<i>Contactez l'administrateur principal pour modifier vos accès.</i>",
                parse_mode='HTML'
            )
            return

        sections = []

        # ── Général ──────────────────────────────────────────────────────────
        sections.append(
            "📋 <b>GÉNÉRAL</b>\n"
            "/start — Statut du bot et canaux actifs\n"
            "/help — Cette liste de commandes\n"
            "/myid — Voir votre Telegram ID\n"
            "/cancel — Annuler une recherche en cours"
        )

        # ── Connexion Telegram ────────────────────────────────────────────────
        if main:
            sections.append(
                "🔐 <b>CONNEXION TELEGRAM</b>\n"
                "/connect — Envoyer le code SMS d'authentification\n"
                "/code aa12345 — Entrer le code reçu par SMS\n"
                "/disconnect — Supprimer la session active"
            )

        # ── Données locales (canal principal) ─────────────────────────────────
        sections.append(
            "💾 <b>DONNÉES LOCALES — CANAL PRINCIPAL</b>\n"
            "/sync — Synchroniser les messages récents\n"
            "/fullsync — Tout l'historique du canal principal\n"
            "/stats — Statistiques des prédictions synchronisées\n"
            "/report — Générer un PDF complet des prédictions\n"
            "/search mot1 mot2 — Recherche locale (PDF)\n"
            "/filter — Filtrer par couleur ou statut\n"
            "📎 <i>Envoyer un PDF → extraire les numéros prédits</i>"
        )

        # ── Canaux de recherche ───────────────────────────────────────────────
        sections.append(
            "📡 <b>CANAUX DE RECHERCHE</b>\n"
            "/addchannel — Ajouter un canal à la liste\n"
            "/channels — Voir et gérer les canaux\n"
            "/usechannel -100XXX — Activer un canal\n"
            "/removechannel -100XXX — Supprimer un canal\n"
            "/hsearch mot1 mot2 — Rechercher dans l'historique\n"
            "  Options : <code>limit:500</code>  <code>from:2024-06-01</code>\n"
            "  Ex : <code>/hsearch GAGNÉ Cœur limit:1000</code>"
        )

        # ── Analyse de jeux Baccarat ──────────────────────────────────────────
        sections.append(
            "🎴 <b>ANALYSE DE JEUX BACCARAT</b>\n"
            "/gload from:AAAA-MM-JJ [HH:MM] — Charger les jeux depuis une date\n"
            "/gload limit:N — Charger les N derniers messages\n"
            "  Options : <code>limit:N</code>  <code>from:AAAA-MM-JJ</code>\n"
            "/ganalyze — Analyser un enregistrement (copier-coller)\n"
            "/gstats — Statistiques de tous les jeux chargés\n"
            "/gclear — Effacer les jeux analysés\n"
            "\n"
            "<b>Recherche par catégorie :</b>\n"
            "/gvictoire joueur|banquier|nul — Numéros et écarts\n"
            "/gparite pair|impair — Numéros et écarts\n"
            "/gstructure 2/2|2/3|3/2|3/3 — Structure des cartes\n"
            "/gplusmoins j|b plus|moins — Plus/Moins par joueur\n"
            "/gcostume ♠|♥|♦|♣ j|b — Costumes manquants\n"
            "/gecartmax — Paires de numéros formant l'écart max (toutes catégories)"
        )

        # ── Administration ────────────────────────────────────────────────────
        if main:
            sections.append(
                "👥 <b>ADMINISTRATION</b>\n"
                "/addadmin USER_ID [cmd1 cmd2 ...] — Ajouter un admin avec permissions\n"
                "/setperm USER_ID cmd1 cmd2 ... — Modifier les permissions d'un admin\n"
                "/removeadmin USER_ID — Supprimer un administrateur\n"
                "/admins — Liste des admins avec leurs permissions\n"
                "/clear — Effacer toutes les données locales"
            )

        header = "📖 <b>AIDE — TOUTES LES COMMANDES</b>\n\n"
        footer = "\n\n💡 <i>Tapez /cancel à tout moment pour arrêter une recherche en cours.</i>"

        full_text = header + "\n\n".join(sections) + footer
        await update.message.reply_text(full_text, parse_mode='HTML')

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
        if not await self._perm(update, 'addchannel'):
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

    async def removechannel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/removechannel <id> — Supprime un canal de la liste."""
        if not await self._perm(update, 'removechannel'):
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
                "Usage: `/hsearch mot1 mot2 [limit:N] [from:AAAA-MM-JJ]`\n\n"
                "Exemples :\n"
                "`/hsearch GAGNÉ Cœur`\n"
                "`/hsearch GAGNÉ limit:500`\n"
                "`/hsearch GAGNÉ from:2024-06-01`\n\n"
                "Tapez /cancel pour arrêter et voir les résultats partiels.",
                parse_mode='Markdown'
            )
            return

        keywords, limit, from_date = parse_search_options(list(context.args))
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
        elif from_date:
            scope_desc = f" | 📅 depuis {from_date.strftime('%d/%m/%Y %H:%M')}"

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

        _, limit, from_date = parse_search_options(list(context.args)) if context.args else ([], None, None)

        if not limit and not from_date:
            await update.message.reply_text(
                "⚠️ <b>Paramètre requis</b>\n\n"
                "Vous devez préciser une date ou une limite pour éviter de charger tout l'historique.\n\n"
                "<b>Exemples :</b>\n"
                "<code>/gload from:2026-02-01</code>\n"
                "<code>/gload from:2026-02-01 10:30</code>\n"
                "<code>/gload from:2026-02-01T10:30</code>\n"
                "<code>/gload limit:500</code>",
                parse_mode='HTML'
            )
            return

        channel_id = active['id']
        channel_name = active.get('name') or channel_id

        scope_desc = ''
        if limit:
            scope_desc = f" | 🔢 {limit} derniers messages"
        elif from_date:
            scope_desc = f" | 📅 depuis {from_date.strftime('%d/%m/%Y %H:%M')}"

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
                    progress_callback=progress,
                    cancel_check=lambda: _search_cancel.get(uid, False)
                )

                if not records:
                    await msg.edit_text("❌ Aucun enregistrement de jeu trouvé dans ce canal.")
                    return

                games = []
                for text in records:
                    g = parse_game(text)
                    if g:
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
        """Routeur de texte : canal, permissions ou analyse de jeu selon l'état d'attente."""
        uid = update.effective_user.id
        if _waiting_for_perm.get(uid):
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

    async def clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._perm(update, 'clear'):
            return
        clear_all()
        await update.message.reply_text("🗑️ Effacé !")

handlers = Handlers()

def setup_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("connect", handlers.connect))
    app.add_handler(CommandHandler("code", handlers.code))
    app.add_handler(CommandHandler("disconnect", handlers.disconnect))
    app.add_handler(CommandHandler("sync", handlers.sync))
    app.add_handler(CommandHandler("fullsync", handlers.fullsync))
    app.add_handler(CommandHandler("report", handlers.report))
    app.add_handler(CommandHandler("filter", handlers.filter_cmd))
    app.add_handler(CommandHandler("stats", handlers.stats))
    app.add_handler(CommandHandler("search", handlers.search))
    app.add_handler(CommandHandler("clear", handlers.clear))
    app.add_handler(CommandHandler("addchannel", handlers.addchannel))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("cancel", handlers.cancel))
    app.add_handler(CommandHandler("channels", handlers.channels))
    app.add_handler(CommandHandler("usechannel", handlers.usechannel))
    app.add_handler(CommandHandler("removechannel", handlers.removechannel))
    app.add_handler(CommandHandler("hsearch", handlers.hsearch))
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
