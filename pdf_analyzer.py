import re
import pdfplumber
import logging

logger = logging.getLogger(__name__)

# Pattern pour les messages bruts du canal
RAW_PATTERN = re.compile(
    r'PR[EÉ]DICTION\s*#?(\d+).*?'
    r'(?:Couleur|Costume|Suit)\s*[:\-]\s*([^\n\r]+)'
    r'(?:.*?Statut\s*[:\-]\s*([^\n\r]+))?',
    re.IGNORECASE | re.DOTALL
)

# Mapping couleur → emoji
COLOR_EMOJI_MAP = {
    '♣': '♣️',
    '♣️': '♣️',
    'trèfle': '♣️',
    'trefle': '♣️',
    '❤': '❤️',
    '❤️': '❤️',
    'cœur': '❤️',
    'coeur': '❤️',
    '♠': '♠️',
    '♠️': '♠️',
    'pique': '♠️',
    '♦': '♦️',
    '♦️': '♦️',
    'carreau': '♦️',
}


def get_color_emoji(couleur_str: str) -> str:
    """Extrait l'emoji de couleur depuis la chaîne brute."""
    s = couleur_str.strip().lower()
    for key, emoji in COLOR_EMOJI_MAP.items():
        if key.lower() in s:
            return emoji
    # Si aucun mapping, retourner la chaîne brute tronquée
    return couleur_str.strip()[:20]


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extrait tout le texte d'un PDF page par page."""
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
            for table in page.extract_tables():
                for row in table:
                    if row:
                        text_parts.append(' | '.join(str(c) for c in row if c))
    return '\n'.join(text_parts)


def analyze_pdf(pdf_path: str):
    """
    Analyse un PDF et extrait la liste des numéros prédits avec leur emoji de couleur.
    Déduplique : si un numéro apparaît plusieurs fois, on garde une seule occurrence.

    Retourne:
        list of dict: [{'numero': '1', 'couleur_emoji': '♣️', 'statut': '...', 'count': 2}, ...]
        str: texte extrait brut (pour debug si aucun résultat)
    """
    text = extract_text_from_pdf(pdf_path)

    predictions = {}  # numero -> dict

    # --- Méthode 1 : pattern brut PRÉDICTION #X ... Couleur: Y ---
    for match in RAW_PATTERN.finditer(text):
        numero = match.group(1).strip()
        couleur_raw = match.group(2).strip()
        statut_raw = match.group(3).strip() if match.group(3) else ''

        couleur_emoji = get_color_emoji(couleur_raw)

        if numero not in predictions:
            predictions[numero] = {
                'numero': numero,
                'couleur_emoji': couleur_emoji,
                'statut': statut_raw[:60],
                'count': 1
            }
        else:
            predictions[numero]['count'] += 1

    # --- Méthode 2 : lecture ligne par ligne des tableaux ---
    if not predictions:
        lines = text.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue

            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 3:
                num_match = re.search(r'#?(\d+)', parts[1]) if len(parts) > 1 else None
                if num_match:
                    numero = num_match.group(1)
                    couleur_raw = parts[2] if len(parts) > 2 else ''
                    statut = parts[3] if len(parts) > 3 else ''
                    couleur_emoji = get_color_emoji(couleur_raw)
                    if numero not in predictions:
                        predictions[numero] = {
                            'numero': numero,
                            'couleur_emoji': couleur_emoji,
                            'statut': statut.strip()[:60],
                            'count': 1
                        }
                    else:
                        predictions[numero]['count'] += 1
                    continue

    # Trier par numéro
    result = sorted(predictions.values(), key=lambda x: int(x['numero']))
    return result, text[:500] if not result else ''
