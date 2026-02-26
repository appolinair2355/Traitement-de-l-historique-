from datetime import datetime
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4

def generate_pdf(predictions, filters=None):
    """Génère le PDF des prédictions"""
    filename = f"/tmp/rapport_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    doc = SimpleDocTemplate(filename, pagesize="A4")
    styles = getSampleStyleSheet()
    elements = []
    
    # Titre
    title_style = ParagraphStyle(
        'Title', parent=styles['Heading1'],
        fontSize=18, textColor=colors.HexColor('#1a5276'),
        alignment=TA_CENTER
    )
    elements.append(Paragraph("RAPPORT DES PRÉDICTIONS VIP", title_style))
    elements.append(Spacer(1, 20))
    
    # Stats
    total = len(predictions)
    gagnes = len([p for p in predictions if 'gagn' in p['statut'].lower()])
    perdus = len([p for p in predictions if 'perd' in p['statut'].lower()])
    
    stats = f"Total: {total} | Gagnés: {gagnes} | Perdus: {perdus}"
    elements.append(Paragraph(stats, styles['Normal']))
    elements.append(Spacer(1, 20))
    
    # Tableau
    if predictions:
        data = [['#', 'Numéro', 'Couleur', 'Statut', 'Date']]
        
        for i, p in enumerate(predictions[:1000], 1):
            date_str = p['date'][:10] if isinstance(p['date'], str) else str(p['date'])[:10]
            
            # Couleur du statut
            statut = p['statut']
            if 'gagn' in statut.lower():
                statut_html = f"<font color='green'>{statut}</font>"
            elif 'perd' in statut.lower():
                statut_html = f"<font color='red'>{statut}</font>"
            else:
                statut_html = f"<font color='orange'>{statut}</font>"
            
            data.append([
                str(i),
                f"#{p['numero']}",
                p['couleur'],
                Paragraph(statut_html, styles['Normal']),
                date_str
            ])
        
        table = Table(data, colWidths=[40, 80, 120, 150, 80])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#2874a6')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
            ('ALIGN', (0,0), (-1,-1), 'CENTER'),
            ('GRID', (0,0), (-1,-1), 1, colors.grey),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        elements.append(table)
    
    doc.build(elements)
    return filename


def generate_search_pdf(results, keywords):
    """Génère un PDF avec les résultats de recherche par mots-clés"""
    filename = f"/tmp/recherche_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    doc = SimpleDocTemplate(filename, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle(
        'Title', parent=styles['Heading1'],
        fontSize=16, textColor=colors.HexColor('#1a5276'),
        alignment=TA_CENTER
    )
    subtitle_style = ParagraphStyle(
        'Subtitle', parent=styles['Normal'],
        fontSize=11, textColor=colors.HexColor('#555555'),
        alignment=TA_CENTER
    )
    body_style = ParagraphStyle(
        'Body', parent=styles['Normal'],
        fontSize=9, leading=12
    )

    elements.append(Paragraph("RECHERCHE DANS LES MESSAGES", title_style))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(f"Mots-clés: {', '.join(keywords)}", subtitle_style))
    elements.append(Paragraph(f"Résultats trouvés: {len(results)}", subtitle_style))
    elements.append(Spacer(1, 16))

    if not results:
        elements.append(Paragraph("Aucun résultat trouvé.", styles['Normal']))
    else:
        for i, r in enumerate(results, 1):
            date_str = r.get('date', '')[:10] if r.get('date') else ''
            header = f"<b>#{i} — Message #{r.get('numero', '?')} | {r.get('couleur', '')} | {r.get('statut', '')} | {date_str}</b>"
            elements.append(Paragraph(header, body_style))
            raw = r.get('raw_text', '').replace('\n', '<br/>')
            elements.append(Paragraph(raw, body_style))
            elements.append(Spacer(1, 8))

    doc.build(elements)
    return filename


def generate_channel_search_pdf(messages, keywords, channel_title=''):
    """Génère un PDF avec les messages bruts trouvés directement dans le canal"""
    filename = f"/tmp/canal_recherche_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    doc = SimpleDocTemplate(filename, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle(
        'Title', parent=styles['Heading1'],
        fontSize=16, textColor=colors.HexColor('#1a5276'),
        alignment=TA_CENTER
    )
    subtitle_style = ParagraphStyle(
        'Subtitle', parent=styles['Normal'],
        fontSize=11, textColor=colors.HexColor('#555555'),
        alignment=TA_CENTER
    )
    body_style = ParagraphStyle(
        'Body', parent=styles['Normal'],
        fontSize=9, leading=12
    )

    header_title = f"RECHERCHE — {channel_title}" if channel_title else "RECHERCHE DANS LE CANAL TELEGRAM"
    elements.append(Paragraph(header_title, title_style))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(f"Mots-clés: {', '.join(keywords)}", subtitle_style))
    elements.append(Paragraph(f"Résultats trouvés: {len(messages)}", subtitle_style))
    elements.append(Spacer(1, 16))

    if not messages:
        elements.append(Paragraph("Aucun résultat trouvé.", styles['Normal']))
    else:
        for i, m in enumerate(messages, 1):
            date_str = str(m.get('date', ''))[:16]
            header = f"<b>#{i} — ID: {m.get('id', '?')} | {date_str}</b>"
            elements.append(Paragraph(header, body_style))
            raw = (m.get('text', '') or '').replace('\n', '<br/>')
            elements.append(Paragraph(raw, body_style))
            elements.append(Spacer(1, 8))

    doc.build(elements)
    return filename


def generate_documentation_pdf(is_main_admin=True):
    filename = f"/tmp/documentation_vip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    doc = SimpleDocTemplate(filename, pagesize=A4, topMargin=30, bottomMargin=30,
                            leftMargin=40, rightMargin=40)
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'DocTitle', parent=styles['Heading1'],
        fontSize=20, textColor=colors.HexColor('#1a5276'),
        alignment=TA_CENTER, spaceAfter=6
    )
    section_style = ParagraphStyle(
        'Section', parent=styles['Heading2'],
        fontSize=14, textColor=colors.HexColor('#2874a6'),
        spaceBefore=14, spaceAfter=6,
        borderWidth=1, borderColor=colors.HexColor('#2874a6'),
        borderPadding=4
    )
    cmd_style = ParagraphStyle(
        'Cmd', parent=styles['Normal'],
        fontSize=10, leading=13, spaceBefore=6, spaceAfter=2,
        textColor=colors.HexColor('#1a1a1a')
    )
    example_style = ParagraphStyle(
        'Example', parent=styles['Normal'],
        fontSize=9, leading=12, leftIndent=20,
        textColor=colors.HexColor('#444444'), spaceAfter=2
    )
    note_style = ParagraphStyle(
        'Note', parent=styles['Normal'],
        fontSize=9, leading=12, leftIndent=10,
        textColor=colors.HexColor('#666666'),
        spaceBefore=4, spaceAfter=6,
        backColor=colors.HexColor('#f5f5f5')
    )
    subtitle_style = ParagraphStyle(
        'DocSubtitle', parent=styles['Normal'],
        fontSize=11, textColor=colors.HexColor('#555555'),
        alignment=TA_CENTER, spaceAfter=20
    )

    el = []
    el.append(Paragraph("DOCUMENTATION COMPL\u00c8TE", title_style))
    el.append(Paragraph("BOT VIP KOUAM\u00c9", title_style))
    el.append(Spacer(1, 6))
    el.append(Paragraph(f"Guide d\u00e9taill\u00e9 de toutes les commandes &mdash; {datetime.now().strftime('%d/%m/%Y')}", subtitle_style))
    el.append(Spacer(1, 10))

    def add_section(title):
        el.append(Paragraph(title, section_style))

    def add_cmd(name, desc):
        el.append(Paragraph(f"<b>{name}</b> &mdash; {desc}", cmd_style))

    def add_ex(*examples):
        for ex in examples:
            el.append(Paragraph(f"<font face='Courier' color='#2874a6'>{ex}</font>", example_style))

    def add_note(text):
        el.append(Paragraph(f"<i>{text}</i>", note_style))

    def add_text(text):
        el.append(Paragraph(text, cmd_style))

    add_section("SOMMAIRE")
    sommaire = [
        "1. Canaux &mdash; Gestion des canaux Telegram",
        "2. Recherche &mdash; Recherche dans l'historique",
        "3. Donn\u00e9es locales &mdash; Synchronisation et export",
        "4. Analyse Baccarat &mdash; Chargement et statistiques",
        "5. Cat\u00e9gories d'analyse &mdash; Victoire, Parit\u00e9, Structure, Costumes, Valeurs",
        "6. Cycles de costumes &mdash; Correction et recherche automatique",
        "7. Pr\u00e9diction &mdash; Syst\u00e8me de pr\u00e9diction par \u00e9carts",
    ]
    if is_main_admin:
        sommaire.append("8. Administration &mdash; Gestion des admins et permissions")
    sommaire.append("9. Astuces et informations g\u00e9n\u00e9rales")
    for s in sommaire:
        el.append(Paragraph(f"  {s}", example_style))
    el.append(Spacer(1, 10))

    add_section("1. GESTION DES CANAUX")
    add_cmd("/helpcl", "Menu interactif pour choisir le canal d'analyse")
    add_note("Le bot affiche une liste num\u00e9rot\u00e9e. Tapez le num\u00e9ro pour s\u00e9lectionner. Tapez 'sortir' pour quitter.")
    add_cmd("/addchannel", "Ajouter un nouveau canal")
    add_ex("/addchannel", "Puis entrez : -1001234567890 ou @moncanal")
    add_cmd("/channels", "Voir tous les canaux enregistr\u00e9s avec leur statut")
    add_cmd("/usechannel", "Activer un canal directement par son ID")
    add_ex("/usechannel -1001234567890")
    add_cmd("/removechannel", "Supprimer un canal de la liste")
    add_ex("/removechannel -1001234567890")
    add_note("Apr\u00e8s /addchannel, utilisez /gload pour charger les jeux du canal actif.")

    add_section("2. RECHERCHE")
    add_cmd("/hsearch", "Rechercher des mots-cl\u00e9s dans l'historique du canal actif")
    add_ex("/hsearch GAGN\u00c9 Coeur",
           "/hsearch PERDU limit:500",
           "/hsearch Pr\u00e9diction from:2026-02-20",
           "/hsearch GAGN\u00c9 from:2026-02-20 to:2026-02-23")
    add_note("Options combinables : limit:N, from:AAAA-MM-JJ, to:AAAA-MM-JJ HH:MM. Le r\u00e9sultat s'exporte en PDF.")
    add_cmd("/searchcard", "Recherche par valeur de carte (A, K, Q, J)")
    add_ex("/searchcard K joueur  &mdash; Tous les K c\u00f4t\u00e9 Joueur",
           "/searchcard A banquier  &mdash; Tous les As c\u00f4t\u00e9 Banquier",
           "/searchcard K Q joueur  &mdash; K ou Q c\u00f4t\u00e9 Joueur",
           "/searchcard K from:2026-02-20 to:2026-02-23")
    add_note("Valeurs : A, K, Q, J. C\u00f4t\u00e9s : joueur, banquier, tous")
    add_cmd("/search", "Recherche dans les donn\u00e9es locales (pr\u00e9dictions stock\u00e9es)")
    add_ex("/search rouge gagn\u00e9", "/search Coeur limit:100")

    add_section("3. DONN\u00c9ES LOCALES")
    add_cmd("/sync", "R\u00e9cup\u00e9rer les nouveaux messages depuis la derni\u00e8re synchronisation")
    add_cmd("/fullsync", "R\u00e9cup\u00e9rer tout l'historique du canal (peut \u00eatre long)")
    add_cmd("/stats", "Nombre de pr\u00e9dictions stock\u00e9es en local")
    add_cmd("/report", "G\u00e9n\u00e9rer un PDF complet de toutes les pr\u00e9dictions")
    add_cmd("/filter", "Filtrer par couleur ou statut")
    add_cmd("/clear", "Effacer toutes les donn\u00e9es locales")
    add_text("<b>Envoi de PDF</b> : Envoyez un fichier PDF au bot, il en extrait automatiquement les num\u00e9ros et costumes.")
    add_note("Le PDF affiche les num\u00e9ros au format : 1436 [costume]. Les num\u00e9ros apparaissant 4+ fois sont affich\u00e9s.")

    el.append(PageBreak())
    add_section("4. ANALYSE BACCARAT &mdash; CHARGEMENT")
    add_cmd("/gload", "Charger les jeux depuis le canal actif")
    add_ex("/gload from:2026-02-01  &mdash; Depuis le 1er f\u00e9vrier 2026",
           "/gload from:2026-02-10 08:00  &mdash; Depuis le 10 f\u00e9v. \u00e0 8h",
           "/gload limit:200  &mdash; Les 200 derniers jeux")
    add_note("Une date ou une limite est OBLIGATOIRE pour \u00e9viter de scanner tout l'historique.")
    add_cmd("/gstats", "R\u00e9sum\u00e9 statistique complet des jeux charg\u00e9s")
    add_note("Affiche : nombre de jeux, victoires J/B/N, parit\u00e9, structures, costumes manquants, valeurs sp\u00e9ciales, \u00e9carts max par cat\u00e9gorie.")
    add_cmd("/ganalyze", "Analyser un enregistrement manuellement (copier-coller)")
    add_ex("Format attendu : #N794. V3(K*4*9*) - 1(J*10*A*) #T4")
    add_cmd("/gclear", "Effacer les jeux charg\u00e9s en m\u00e9moire")

    add_section("5. CAT\u00c9GORIES D'ANALYSE")

    add_text("<b>5.1 Victoire</b>")
    add_cmd("/gvictoire", "\u00c9carts par r\u00e9sultat de victoire")
    add_ex("/gvictoire joueur  &mdash; Victoires Joueur uniquement",
           "/gvictoire banquier  &mdash; Victoires Banquier uniquement",
           "/gvictoire nul  &mdash; Matchs nuls uniquement",
           "/gvictoire  &mdash; Tous les r\u00e9sultats")
    add_note("Affiche les num\u00e9ros de jeux, les \u00e9carts entre apparitions, et l'\u00e9cart maximum.")

    add_text("<b>5.2 Parit\u00e9</b>")
    add_cmd("/gparite", "\u00c9carts par parit\u00e9 du total (score joueur + banquier)")
    add_ex("/gparite pair  &mdash; Totaux pairs (0, 2, 4, 6, 8)",
           "/gparite impair  &mdash; Totaux impairs (1, 3, 5, 7, 9)",
           "/gparite  &mdash; Les deux")

    add_text("<b>5.3 Structure</b>")
    add_cmd("/gstructure", "\u00c9carts par structure de cartes distribu\u00e9es")
    add_ex("/gstructure 2/2  &mdash; Joueur 2K / Banquier 2K",
           "/gstructure 2/3  &mdash; Joueur 2K / Banquier 3K",
           "/gstructure 3/2  &mdash; Joueur 3K / Banquier 2K",
           "/gstructure 3/3  &mdash; Joueur 3K / Banquier 3K",
           "/gstructure  &mdash; Toutes les structures")
    add_note("Le bilan montre aussi : Banquier 2K (2/2 + 3/2) et Banquier 3K (2/3 + 3/3).")

    add_text("<b>5.4 Plus/Moins</b>")
    add_cmd("/gplusmoins", "\u00c9carts pour Plus de 6,5 / Moins de 4,5 / Neutre")
    add_ex("/gplusmoins j plus  &mdash; Joueur Plus de 6,5",
           "/gplusmoins b moins  &mdash; Banquier Moins de 4,5",
           "/gplusmoins j neutre  &mdash; Joueur Neutre (entre 4,5 et 6,5)",
           "/gplusmoins  &mdash; Tous les cas")

    add_text("<b>5.5 Costumes manquants</b>")
    add_cmd("/gcostume", "Costumes absents dans la main d'un c\u00f4t\u00e9")
    add_ex("/gcostume pique j  &mdash; Pique manquant chez Joueur",
           "/gcostume coeur b  &mdash; Coeur manquant chez Banquier",
           "/gcostume carreau  &mdash; Carreau manquant des deux c\u00f4t\u00e9s",
           "/gcostume  &mdash; Tous les costumes")
    add_note("Costumes accept\u00e9s : pique, coeur, carreau, tr\u00e8fle, ou les symboles directement.")

    add_text("<b>5.6 Valeurs sp\u00e9ciales (Face cards)</b>")
    add_cmd("/gvaleur", "Analyse des cartes de valeur (A, K, Q, J) par costume et par c\u00f4t\u00e9")
    add_ex("/gvaleur A  &mdash; Tous les As par costume des deux c\u00f4t\u00e9s",
           "/gvaleur K joueur  &mdash; Roi par costume c\u00f4t\u00e9 Joueur",
           "/gvaleur Q banquier  &mdash; Dame par costume c\u00f4t\u00e9 Banquier",
           "/gvaleur J  &mdash; Valet par costume des deux c\u00f4t\u00e9s",
           "/gvaleur  &mdash; Toutes les valeurs (A, K, Q, J)")
    add_note("32 combinaisons analys\u00e9es : 4 valeurs (A, K, Q, J) x 4 costumes (Pique, Coeur, Carreau, Tr\u00e8fle) x 2 c\u00f4t\u00e9s (Joueur, Banquier). Chaque combinaison est une cat\u00e9gorie du pr\u00e9dicteur.")
    add_text("Combinaisons des valeurs sp\u00e9ciales :")
    val_data = [
        ['Valeur', 'Joueur', 'Banquier'],
        ['As (A)', 'A Pique J, A Coeur J, A Carreau J, A Tr\u00e8fle J', 'A Pique B, A Coeur B, A Carreau B, A Tr\u00e8fle B'],
        ['Roi (K)', 'K Pique J, K Coeur J, K Carreau J, K Tr\u00e8fle J', 'K Pique B, K Coeur B, K Carreau B, K Tr\u00e8fle B'],
        ['Dame (Q)', 'Q Pique J, Q Coeur J, Q Carreau J, Q Tr\u00e8fle J', 'Q Pique B, Q Coeur B, Q Carreau B, Q Tr\u00e8fle B'],
        ['Valet (J)', 'J Pique J, J Coeur J, J Carreau J, J Tr\u00e8fle J', 'J Pique B, J Coeur B, J Carreau B, J Tr\u00e8fle B'],
    ]
    val_table = Table(val_data, colWidths=[70, 190, 190])
    val_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2874a6')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    el.append(val_table)
    el.append(Spacer(1, 6))

    add_text("<b>5.7 \u00c9cart maximum</b>")
    add_cmd("/gecartmax", "Trouve l'\u00e9cart maximum dans TOUTES les 67 cat\u00e9gories")
    add_note("Affiche les paires de num\u00e9ros formant l'\u00e9cart le plus grand pour chaque cat\u00e9gorie.")

    el.append(PageBreak())
    add_section("6. CORRECTION DE CYCLES DE COSTUMES")
    add_text("Les cycles de costumes permettent de v\u00e9rifier si les costumes manquants suivent un sch\u00e9ma r\u00e9p\u00e9titif. La correction g\u00e9n\u00e8re une liste compl\u00e8te num\u00e9ro [costume] pour chaque jeu qualifiant.")

    add_cmd("/gcycle", "Tester un cycle de costumes pr\u00e9d\u00e9fini")
    add_ex("/gcycle pair  &mdash; Cycle pairs (sauf multiples de 10)",
           "/gcycle impair  &mdash; Cycle impairs + multiples de 10",
           "/gcycle pair j  &mdash; C\u00f4t\u00e9 Joueur seulement",
           "/gcycle impair b 6-1436  &mdash; Banquier, plage sp\u00e9cifique")
    add_note("Cycle PAIR (7 \u00e9l\u00e9ments) : Coeur, Carreau, Tr\u00e8fle, Pique, Carreau, Coeur, Pique. Cycle IMPAIR (8 \u00e9l\u00e9ments) : Coeur, Carreau, Tr\u00e8fle, Pique, Carreau, Coeur, Pique, Tr\u00e8fle.")
    add_text("<b>R\u00e9sultat de /gcycle :</b>")
    add_text("1) D\u00e9tails des \u00e9carts (messages temporaires 15s)")
    add_text("2) Bilan : taux de correspondance + cycle corrig\u00e9 sugg\u00e9r\u00e9")
    add_text("3) Liste compl\u00e8te de correction au format num\u00e9ro [costume] :")
    el.append(Paragraph("<font face='Courier'>6 [Coeur]<br/>8 [Carreau]<br/>12 [Tr\u00e8fle]<br/>14 [Pique]<br/>16 [Carreau]<br/>...</font>", example_style))
    el.append(Spacer(1, 4))

    add_cmd("/gcycleauto", "Recherche automatique du meilleur cycle + filtre")
    add_ex("/gcycleauto  &mdash; Recherche compl\u00e8te (tous c\u00f4t\u00e9s)",
           "/gcycleauto j  &mdash; C\u00f4t\u00e9 Joueur seulement",
           "/gcycleauto b 6-1436  &mdash; Banquier, plage sp\u00e9cifique")
    add_note("Teste 12 filtres de num\u00e9ros x 8 longueurs de cycles (5 \u00e0 12) automatiquement. Filtres test\u00e9s : tous, pairs sauf x10, impairs+x10, pairs, impairs, sauf x10, sauf x5, terminaison 2/8, terminaison 4/6, terminaison 1/3/7/9, sauf x3, multiples de 3 sauf x10.")
    add_text("<b>R\u00e9sultat de /gcycleauto :</b>")
    add_text("1) Top 5 des meilleures combinaisons (filtre + cycle + taux)")
    add_text("2) Le meilleur cycle trouv\u00e9 avec son filtre id\u00e9al")
    add_text("3) D\u00e9tails des \u00e9carts (message temporaire 20s)")
    add_text("4) Liste compl\u00e8te de correction num\u00e9ro [costume]")

    add_section("7. SYST\u00c8ME DE PR\u00c9DICTION")
    add_cmd("/predictsetup", "Configurer les canaux de pr\u00e9diction")
    add_ex("/predictsetup",
           "Puis : 1=S 2=S 3=P",
           "S = canal Statistiques (r\u00e9sultats #N), P = canal Pr\u00e9dicteur")
    add_cmd("/gpredictload", "Charger les jeux depuis les canaux statistiques")
    add_note("Charge automatiquement depuis tous les canaux marqu\u00e9s S.")
    add_cmd("/gpredict", "G\u00e9n\u00e9rer des pr\u00e9dictions par cat\u00e9gorie")
    add_ex("/gpredict 30  &mdash; Les 30 prochains jeux",
           "/gpredict 900 950  &mdash; Du jeu #900 au #950",
           "/gpredict 30 from:2026-02-20 to:2026-02-23")
    add_note("67 cat\u00e9gories analys\u00e9es par l'algorithme d'\u00e9carts. Chaque num\u00e9ro n'appara\u00eet que dans la cat\u00e9gorie la plus confiante. Maximum 4 pr\u00e9dictions par cat\u00e9gorie.")

    if is_main_admin:
        add_section("8. ADMINISTRATION")
        add_cmd("/addadmin 123456789", "Ajouter un administrateur")
        add_note("Le bot affiche la liste num\u00e9rot\u00e9e des commandes. Tapez : 1,3,5 ou 1-8,13")
        add_cmd("/setperm 123456789", "Modifier les permissions d'un admin existant")
        add_cmd("/removeadmin 123456789", "Supprimer un admin")
        add_cmd("/admins", "Voir tous les admins et leurs permissions")
        add_cmd("/connect", "Connexion Telegram (code SMS)")
        add_cmd("/code aa12345", "Valider le code SMS (pr\u00e9fixer avec 'aa')")
        add_cmd("/disconnect", "Supprimer la session Telegram")
        add_cmd("/myid", "Afficher votre Telegram ID")

    sect_num = "9" if is_main_admin else "8"
    add_section(f"{sect_num}. ASTUCES ET INFORMATIONS")
    tips = [
        "<b>/cancel</b> &mdash; Annule n'importe quelle op\u00e9ration en cours",
        "Apr\u00e8s /gload, les commandes d'analyse travaillent sur les jeux charg\u00e9s",
        "Les listes d\u00e9taill\u00e9es s'effacent apr\u00e8s 10-15 secondes",
        "Les bilans restent en permanence",
        "/helpcl est le moyen le plus rapide de changer de canal",
        "Format des dates : from:AAAA-MM-JJ ou from:AAAA-MM-JJ HH:MM",
    ]
    for tip in tips:
        el.append(Paragraph(f"  * {tip}", cmd_style))

    el.append(Spacer(1, 20))
    add_section("TABLEAU DES 67 CAT\u00c9GORIES")
    cat_data = [
        ['Groupe', 'Cat\u00e9gories', 'Nb'],
        ['Victoire', 'Joueur, Banquier, Nul', '3'],
        ['Parit\u00e9', 'Pair, Impair', '2'],
        ['Structure', '2/2, 2/3, 3/2, 3/3', '4'],
        ['2K/3K', 'Joueur 2K, Joueur 3K, Banquier 2K, Banquier 3K', '4'],
        ['Plus/Moins', 'J Plus 6.5, J Moins 4.5, J Neutre, B Plus 6.5, B Moins 4.5, B Neutre', '6'],
        ['Costumes', 'Pique/Coeur/Carreau/Tr\u00e8fle manquant Joueur + Banquier', '8'],
        ['Valeurs', 'A/K/Q/J Joueur + Banquier', '8'],
        ['Valeurs+Costume', 'A/K/Q/J x Pique/Coeur/Carreau/Tr\u00e8fle x Joueur/Banquier', '32'],
        ['', 'TOTAL', '67'],
    ]
    cat_table = Table(cat_data, colWidths=[90, 290, 40])
    cat_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2874a6')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (2, 0), (2, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#d5e8f0')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
    ]))
    el.append(cat_table)

    doc.build(el)
    return filename
