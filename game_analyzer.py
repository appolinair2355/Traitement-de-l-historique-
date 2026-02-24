import re

SUITS = ['♠', '♥', '♦', '♣']
SUIT_EMOJI = {'♠': '♠️', '♥': '♥️', '♦': '♦️', '♣': '♣️'}
FACE_CARDS = ['A', 'K', 'Q', 'J']

# Regex pour capturer les cartes de face : K♦️, A♠, J♣️, Q♥, etc.
_FACE_CARD_RE = re.compile(r'([AKQJ])(?:♠️|♥️|♦️|♣️|♠|♥|♦|♣)')

# Formats supportés :
#   ✅3(K♦️4♦️9♦️) - 1(J♦️10♥️A♠️)   → victoire joueur/banquier
#   🔰5(K♦️J♦️) - 5(Q♣️10♥️)          → nul (🔰 avant score joueur)
#   5(K♦️J♦️) 🔰 5(Q♣️10♥️)           → nul (🔰 comme séparateur)
#   🔰 avant ou après #N, ou n'importe où → nul détecté par 'in text'
GAME_PATTERN = re.compile(
    r'#N(\d+)[.\s]*'
    r'(✅|🔰|🟣)?\s*(\d+)\(([^)]+)\)\s*(?:-|🔰)\s*(✅|🔰|🟣)?\s*(\d+)\(([^)]+)\)\s*#T(\d+)',
    re.UNICODE
)

# Pattern de secours pour matchs nuls avec format différent (🔰 n'importe où)
NUL_FALLBACK_PATTERN = re.compile(
    r'#N(\d+)[.\s,]*'
    r'(\d+)\(([^)]+)\).*?(\d+)\(([^)]+)\).*?#T(\d+)',
    re.UNICODE | re.DOTALL
)


def extract_face_cards(cards_str: str) -> set:
    """Retourne les cartes de valeur (A, K, Q, J) présentes dans une main.
    Ex: 'K♦️4♦️9♦️' → {'K'}
        'J♦️10♥️A♠️' → {'J', 'A'}
    """
    return set(_FACE_CARD_RE.findall(cards_str))


def extract_suits_present(cards_str):
    """Retourne les costumes présents dans une main."""
    return {ch for ch in cards_str if ch in '♠♥♦♣'}


def count_cards(cards_str):
    """Compte le nombre de cartes via les symboles de costume."""
    return sum(1 for ch in cards_str if ch in '♠♥♦♣')


def get_plusmoins(score):
    score = int(score)
    if score >= 7:
        return 'Plus de 6,5'
    elif score <= 4:
        return 'Moins de 4,5'
    else:
        return 'Neutre'


def parse_game(text):
    """Parse un enregistrement de jeu. Retourne un dict ou None."""
    match = GAME_PATTERN.search(text)
    using_fallback = False

    if not match:
        # Tentative avec le pattern de secours uniquement si 🔰 est présent
        if '🔰' in text:
            match = NUL_FALLBACK_PATTERN.search(text)
            using_fallback = True
        if not match:
            return None

    numero = match.group(1)
    if using_fallback:
        # Groupes du NUL_FALLBACK_PATTERN : 1=N, 2=score_j, 3=cards_j, 4=score_b, 5=cards_b, 6=total
        marker_j = None
        score_j = int(match.group(2))
        cards_j_str = match.group(3)
        marker_b = None
        score_b = int(match.group(4))
        cards_b_str = match.group(5)
        total = int(match.group(6))
    else:
        marker_j = match.group(2)
        score_j = int(match.group(3))
        cards_j_str = match.group(4)
        marker_b = match.group(5)
        score_b = int(match.group(6))
        cards_b_str = match.group(7)
        total = int(match.group(8))

    # 🔰 n'importe où dans le message = match nul (quelle que soit sa position)
    if '🔰' in text:
        victoire = 'NUL'
    elif marker_j == '✅':
        victoire = 'JOUEUR'
    elif marker_b == '✅':
        victoire = 'BANQUIER'
    elif marker_j == '🟣' or marker_b == '🟣' or score_j == score_b:
        victoire = 'NUL'
    else:
        victoire = 'JOUEUR' if score_j > score_b else 'BANQUIER'

    cards_j = count_cards(cards_j_str)
    cards_b = count_cards(cards_b_str)
    suits_j = extract_suits_present(cards_j_str)
    suits_b = extract_suits_present(cards_b_str)
    face_j = extract_face_cards(cards_j_str)
    face_b = extract_face_cards(cards_b_str)
    missing_j = sorted({'♠', '♥', '♦', '♣'} - suits_j)
    missing_b = sorted({'♠', '♥', '♦', '♣'} - suits_b)
    parite = 'PAIR' if total % 2 == 0 else 'IMPAIR'

    return {
        'numero': numero,
        'victoire': victoire,
        'score_j': score_j,
        'score_b': score_b,
        'total': total,
        'parite': parite,
        'cards_j': cards_j,
        'cards_b': cards_b,
        'structure': f'{cards_j}/{cards_b}',
        'plusmoins_j': get_plusmoins(score_j),
        'plusmoins_b': get_plusmoins(score_b),
        'missing_j': missing_j,
        'missing_b': missing_b,
        'face_j': face_j,
        'face_b': face_b,
        'raw': match.group(0)
    }


def format_analysis(game):
    """Formate l'analyse d'un jeu pour affichage."""
    lines = [f"#N{game['numero']}\n"]
    lines.append(f"🏆 Victoire : {game['victoire']}")
    lines.append(f"🎯 Score : {game['score_j']} - {game['score_b']}")
    lines.append(f"📊 Total : {game['total']} ({game['parite']})\n")
    lines.append("🎴 Cartes :")
    lines.append(f"Joueur : {game['cards_j']}K")
    lines.append(f"Banquier : {game['cards_b']}K")
    lines.append(f"Structure : {game['structure']}\n")
    lines.append("🎯 Plus/Moins :")
    lines.append(f"Joueur : {game['plusmoins_j']}")
    lines.append(f"Banquier : {game['plusmoins_b']}\n")
    for suit in ['♠', '♥', '♦', '♣']:
        j_mark = '✅' if suit in game['missing_j'] else '❌'
        b_mark = '✅' if suit in game['missing_b'] else '❌'
        lines.append(f"{SUIT_EMOJI[suit]} Manquant : Joueur {j_mark}, Banquier {b_mark}")
    return '\n'.join(lines)


def calculate_ecarts(numbers):
    """Calcule les écarts entre numéros consécutifs."""
    nums = sorted([int(n) for n in numbers])
    if len(nums) < 2:
        return nums, []
    return nums, [nums[i + 1] - nums[i] for i in range(len(nums) - 1)]


def format_ecarts(numbers, label=''):
    """Formate la liste des numéros et leurs écarts."""
    nums, ecarts = calculate_ecarts(numbers)
    if not nums:
        return f"{label} : Aucun résultat"
    nums_str = ','.join(str(n) for n in nums)
    ecarts_str = ','.join(str(e) for e in ecarts) if ecarts else '-'
    max_ecart = max(ecarts) if ecarts else 0
    return (
        f"{label}\n"
        f"  Numéros : {nums_str}\n"
        f"  Écarts  : {ecarts_str}\n"
        f"  Écart max : {max_ecart} | Total : {len(nums)}"
    )


def build_category_stats(games):
    """Construit les statistiques par catégorie."""
    cats = {
        'victoire': {'JOUEUR': [], 'BANQUIER': [], 'NUL': []},
        'parite': {'PAIR': [], 'IMPAIR': []},
        'structure': {'2/2': [], '2/3': [], '3/2': [], '3/3': []},
        'plusmoins_j': {'Plus de 6,5': [], 'Moins de 4,5': [], 'Neutre': []},
        'plusmoins_b': {'Plus de 6,5': [], 'Moins de 4,5': [], 'Neutre': []},
        'missing_j': {'♠': [], '♥': [], '♦': [], '♣': []},
        'missing_b': {'♠': [], '♥': [], '♦': [], '♣': []},
        'face_j': {'A': [], 'K': [], 'Q': [], 'J': []},
        'face_b': {'A': [], 'K': [], 'Q': [], 'J': []},
    }
    for g in games:
        n = g['numero']
        if g['victoire'] in cats['victoire']:
            cats['victoire'][g['victoire']].append(n)
        if g['parite'] in cats['parite']:
            cats['parite'][g['parite']].append(n)
        if g['structure'] in cats['structure']:
            cats['structure'][g['structure']].append(n)
        if g['plusmoins_j'] in cats['plusmoins_j']:
            cats['plusmoins_j'][g['plusmoins_j']].append(n)
        if g['plusmoins_b'] in cats['plusmoins_b']:
            cats['plusmoins_b'][g['plusmoins_b']].append(n)
        for s in g['missing_j']:
            if s in cats['missing_j']:
                cats['missing_j'][s].append(n)
        for s in g['missing_b']:
            if s in cats['missing_b']:
                cats['missing_b'][s].append(n)
        for fc in g.get('face_j', set()):
            if fc in cats['face_j']:
                cats['face_j'][fc].append(n)
        for fc in g.get('face_b', set()):
            if fc in cats['face_b']:
                cats['face_b'][fc].append(n)
    return cats


def normalize_suit(s):
    """Normalise un symbole de costume saisi par l'utilisateur."""
    aliases = {
        'spade': '♠', 'pique': '♠', '♠': '♠', '♠️': '♠',
        'heart': '♥', 'coeur': '♥', 'cœur': '♥', '♥': '♥', '♥️': '♥',
        'diamond': '♦', 'carreau': '♦', '♦': '♦', '♦️': '♦',
        'club': '♣', 'trefle': '♣', 'trèfle': '♣', '♣': '♣', '♣️': '♣',
    }
    return aliases.get(s.lower().strip(), None)
