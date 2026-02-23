import os
import asyncio
import logging
import html
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from config import BOT_TOKEN, ADMIN_ID, CHANNEL_USERNAME, USER_PHONE

logger = logging.getLogger(__name__)
from storage import get_predictions, get_stats, clear_all, search_predictions
from scraper import scraper
from auth_manager import auth_manager
from pdf_generator import generate_pdf, generate_search_pdf, generate_channel_search_pdf
from pdf_analyzer import analyze_pdf

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

class Handlers:
    def __init__(self):
        self.syncing = False
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return
        
        connected = "✅ Connecté" if auth_manager.is_connected() else "❌ Non connecté"
        
        await update.message.reply_text(
            f"🎯 **Bot VIP KOUAMÉ & JOKER**\n\n"
            f"Status: {connected}\n"
            f"Votre numéro: `{USER_PHONE}`\n\n"
            f"Commandes auth:\n"
            f"/connect - Recevoir le code SMS (supprime l'ancienne session)\n"
            f"/code aaXXXXXX - Entrer le code (ex: /code aa43481)\n"
            f"/disconnect - Se déconnecter\n\n"
            f"Commandes données:\n"
            f"/sync - Synchroniser récent\n"
            f"/fullsync - Tout l'historique\n"
            f"/search mot1 mot2 - Rechercher dans les messages (PDF)\n"
            f"/report - Générer PDF complet\n"
            f"/stats - Statistiques\n"
            f"/filter - Filtrer par couleur/statut\n\n"
            f"📎 Analyse PDF:\n"
            f"Envoyez un fichier PDF directement → le bot extrait les numéros prédits et costumes",
            parse_mode='Markdown'
        )
    
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
        if not is_admin(update.effective_user.id):
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
        if not is_admin(update.effective_user.id):
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
        if not is_admin(update.effective_user.id):
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
        if not is_admin(update.effective_user.id):
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
        if not is_admin(update.effective_user.id):
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
        if not is_admin(update.effective_user.id):
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

    async def clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
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
    app.add_handler(MessageHandler(filters.Document.PDF, handlers.handle_pdf))
    
    return app
