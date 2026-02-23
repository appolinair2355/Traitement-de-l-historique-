from datetime import datetime
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
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


def generate_channel_search_pdf(messages, keywords):
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

    elements.append(Paragraph("RECHERCHE DANS LE CANAL TELEGRAM", title_style))
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
