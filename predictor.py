"""
Moteur de prédiction Baccarat — analyse des écarts multi-catégories.

Algorithme :
  Pour chaque catégorie (victoire, parité, structure, costumes, etc.) :
  1. On calcule les écarts entre occurrences successives (historique)
  2. On détermine l'écart courant depuis la dernière apparition
  3. On compare au cycle moyen → plus le retard est grand, plus la probabilité monte
  4. On projette sur les N prochains jeux en simulant l'avancement

Timing :
  En plus de l'écart brut, on utilise un indice de cycle :
  cycle_idx = ecart_courant / avg_ecart
  Ce ratio gouverne la forme de la courbe de confiance.
"""

from game_analyzer import build_category_stats


def _ecart_stats(positions: list, last_known: int) -> dict:
    """Statistiques d'écart pour une liste de positions de jeu."""
    if not positions:
        return {
            'count': 0, 'last_pos': 0, 'avg_ecart': 0.0,
            'max_ecart': 0, 'current_ecart': 0, 'all_ecarts': [],
        }
    sp = sorted(int(p) for p in positions)
    count = len(sp)
    if count >= 2:
        ecarts = [sp[i + 1] - sp[i] for i in range(count - 1)]
        avg_ecart = sum(ecarts) / len(ecarts)
        max_ecart = max(ecarts)
    else:
        avg_ecart = float(last_known) / count if count else 0.0
        max_ecart = int(avg_ecart)
        ecarts = []
    last_pos = sp[-1]
    current_ecart = last_known - last_pos
    return {
        'count': count,
        'last_pos': last_pos,
        'avg_ecart': avg_ecart,
        'max_ecart': max_ecart,
        'current_ecart': current_ecart,
        'all_ecarts': ecarts,
    }


def _confidence(stats: dict, freq: float, delta: int) -> int:
    """
    Score de confiance 0-95 pour une catégorie à delta jeux dans le futur.

    Courbe logistique centrée sur avg_ecart :
      - En dessous de avg_ecart  → confiance modérée (< fréquence de base)
      - Autour de avg_ecart      → confiance = fréquence de base
      - Au-delà de avg_ecart     → confiance augmente progressivement
      - Au-delà de max_ecart     → confiance proche du plafond (95)
    """
    if stats['count'] == 0 or freq == 0:
        return 0
    avg = stats['avg_ecart']
    mx = stats['max_ecart']
    ecart = stats['current_ecart'] + delta
    base = freq * 100

    if avg == 0:
        return min(95, int(base))

    ratio = ecart / avg

    if ratio >= 2.5:
        conf = base + 45
    elif ratio >= 2.0:
        conf = base + 35 + (ratio - 2.0) * 20
    elif ratio >= 1.5:
        conf = base + 20 + (ratio - 1.5) * 30
    elif ratio >= 1.0:
        conf = base + (ratio - 1.0) * 40
    elif ratio >= 0.6:
        conf = base * (0.6 + ratio * 0.7)
    else:
        conf = base * ratio * 0.5

    # Plafond dynamique : si on dépasse le max historique → cap à 95
    if mx and ecart > mx:
        conf = min(95, conf)
    return min(95, max(3, int(conf)))


def _all_categories(cats: dict) -> dict:
    """Construit le dictionnaire complet des catégories à analyser."""
    j2k = cats['structure']['2/2'] + cats['structure']['2/3']
    j3k = cats['structure']['3/2'] + cats['structure']['3/3']
    b2k = cats['structure']['2/2'] + cats['structure']['3/2']
    b3k = cats['structure']['2/3'] + cats['structure']['3/3']
    d = {
        '🏆 Victoire Joueur':    cats['victoire']['JOUEUR'],
        '🏆 Victoire Banquier':  cats['victoire']['BANQUIER'],
        '🤝 Match Nul':          cats['victoire']['NUL'],
        '📊 Pair':               cats['parite']['PAIR'],
        '📊 Impair':             cats['parite']['IMPAIR'],
        '🎴 2/2':                cats['structure']['2/2'],
        '🎴 2/3':                cats['structure']['2/3'],
        '🎴 3/2':                cats['structure']['3/2'],
        '🎴 3/3':                cats['structure']['3/3'],
        '👤 Joueur 2K':          j2k,
        '👤 Joueur 3K':          j3k,
        '🏦 Banquier 2K':        b2k,
        '🏦 Banquier 3K':        b3k,
        '📈 Joueur Plus 6.5':    cats['plusmoins_j']['Plus de 6,5'],
        '📉 Joueur Moins 4.5':   cats['plusmoins_j']['Moins de 4,5'],
        '↔️ Joueur Neutre':      cats['plusmoins_j']['Neutre'],
        '📈 Banquier Plus 6.5':  cats['plusmoins_b']['Plus de 6,5'],
        '📉 Banquier Moins 4.5': cats['plusmoins_b']['Moins de 4,5'],
        '↔️ Banquier Neutre':    cats['plusmoins_b']['Neutre'],
        '♠ Manque Joueur':       cats['missing_j']['♠'],
        '♥ Manque Joueur':       cats['missing_j']['♥'],
        '♦ Manque Joueur':       cats['missing_j']['♦'],
        '♣ Manque Joueur':       cats['missing_j']['♣'],
        '♠ Manque Banquier':     cats['missing_b']['♠'],
        '♥ Manque Banquier':     cats['missing_b']['♥'],
        '♦ Manque Banquier':     cats['missing_b']['♦'],
        '♣ Manque Banquier':     cats['missing_b']['♣'],
        '🃏 A Joueur':           cats['face_j']['A'],
        '🃏 K Joueur':           cats['face_j']['K'],
        '🃏 Q Joueur':           cats['face_j']['Q'],
        '🃏 Valet Joueur':       cats['face_j']['J'],
        '🎴 A Banquier':         cats['face_b']['A'],
        '🎴 K Banquier':         cats['face_b']['K'],
        '🎴 Q Banquier':         cats['face_b']['Q'],
        '🎴 Valet Banquier':     cats['face_b']['J'],
    }
    fsj = cats.get('face_suit_j', {})
    fsb = cats.get('face_suit_b', {})
    face_labels = {'A': 'As', 'K': 'Roi', 'Q': 'Dame', 'J': 'Valet'}
    for fc in ['A', 'K', 'Q', 'J']:
        for s in ['♠', '♥', '♦', '♣']:
            key = f'{fc}{s}'
            lbl = face_labels[fc]
            suit_e = {'♠': '♠️', '♥': '♥️', '♦': '♦️', '♣': '♣️'}[s]
            d[f'🃏 {lbl}{suit_e} Joueur'] = fsj.get(key, [])
            d[f'🎴 {lbl}{suit_e} Banquier'] = fsb.get(key, [])
    return d


def build_predict_data(games: list) -> dict:
    """
    Construit les données de prédiction complètes pour une liste de jeux.
    Retourne {cat_name: {nums, stats, freq}}.
    """
    if not games:
        return {}
    cats = build_category_stats(games)
    total = len(games)
    all_nums = [int(g['numero']) for g in games]
    last_known = max(all_nums)

    result = {}
    for name, nums in _all_categories(cats).items():
        count = len(nums)
        freq = count / total
        stats = _ecart_stats(nums, last_known)
        result[name] = {'nums': nums, 'stats': stats, 'freq': freq}
    return result


def generate_predictions(games: list, from_num: int, to_num: int,
                          top_n: int = 6) -> list:
    """
    Génère des prédictions pour les jeux [from_num … to_num].
    Retourne liste de dicts :
      {numero, predictions: [{category, confidence, trend, ecart_info}]}
    """
    if not games:
        return []
    pd = build_predict_data(games)
    if not pd:
        return []
    all_nums = [int(g['numero']) for g in games]
    last_known = max(all_nums)

    results = []
    for target in range(from_num, to_num + 1):
        delta = max(0, target - last_known)
        preds = []
        for cat_name, data in pd.items():
            freq = data['freq']
            stats = data['stats']
            if freq == 0 or stats['count'] == 0:
                continue
            conf = _confidence(stats, freq, delta)
            conf_base = _confidence(stats, freq, 0)
            if conf > conf_base + 5:
                trend = '↗'
            elif conf < conf_base - 5:
                trend = '↘'
            else:
                trend = '→'
            ecart_now = stats['current_ecart'] + delta
            avg = stats['avg_ecart']
            preds.append({
                'category': cat_name,
                'confidence': conf,
                'trend': trend,
                'ecart_now': round(ecart_now, 1),
                'avg_ecart': round(avg, 1),
                'last_pos': stats['last_pos'],
            })
        preds.sort(key=lambda x: -x['confidence'])
        results.append({'numero': target, 'predictions': preds[:top_n]})
    return results


def generate_category_list(games: list, from_num: int, to_num: int,
                            min_confidence: int = 38) -> dict:
    """
    Génère une liste de prédictions PAR CATÉGORIE basée sur l'analyse des manquements.

    Algorithme :
      Pour chaque catégorie :
        1. On répertorie tous les écarts historiques entre occurrences (les "manquements")
        2. Chaque écart de longueur L donne une prédiction :
             predicted = last_occurrence + L
        3. On projette aussi les cycles suivants :
             predicted = last_occurrence + L + k * avg_ecart  (k = 1, 2, ...)
        4. La confiance dépend de : fréquence du gap historique × urgence courante
        5. On élimine les numéros consécutifs (spacing >= 2 obligatoire)
        6. Attribution exclusive : chaque numéro de jeu → UNE seule catégorie
           (la plus confiante gagne)

    Retourne un dict trié par confiance décroissante.
    """
    if not games:
        return {}

    pd = build_predict_data(games)
    if not pd:
        return {}

    all_nums = [int(g['numero']) for g in games]
    last_known = max(all_nums)

    EMOJI_MAP = {
        '🏆 Victoire Joueur':    '🏆',
        '🏆 Victoire Banquier':  '🏆',
        '🤝 Match Nul':          '🤝',
        '📊 Pair':               '📊',
        '📊 Impair':             '📊',
        '🎴 2/2':                '🎴',
        '🎴 2/3':                '🎴',
        '🎴 3/2':                '🎴',
        '🎴 3/3':                '🎴',
        '👤 Joueur 2K':          '👤',
        '👤 Joueur 3K':          '👤',
        '🏦 Banquier 2K':        '🏦',
        '🏦 Banquier 3K':        '🏦',
        '📈 Joueur Plus 6.5':    '📈',
        '📉 Joueur Moins 4.5':   '📉',
        '↔️ Joueur Neutre':      '↔️',
        '📈 Banquier Plus 6.5':  '📈',
        '📉 Banquier Moins 4.5': '📉',
        '↔️ Banquier Neutre':    '↔️',
        '♠ Manque Joueur':       '♠️',
        '♥ Manque Joueur':       '♥️',
        '♦ Manque Joueur':       '♦️',
        '♣ Manque Joueur':       '♣️',
        '♠ Manque Banquier':     '♠️',
        '♥ Manque Banquier':     '♥️',
        '♦ Manque Banquier':     '♦️',
        '♣ Manque Banquier':     '♣️',
        '🃏 A Joueur':           '🃏',
        '🃏 K Joueur':           '🃏',
        '🃏 Q Joueur':           '🃏',
        '🃏 Valet Joueur':       '🃏',
        '🎴 A Banquier':         '🎴',
        '🎴 K Banquier':         '🎴',
        '🎴 Q Banquier':         '🎴',
        '🎴 Valet Banquier':     '🎴',
    }
    _fl = {'A': 'As', 'K': 'Roi', 'Q': 'Dame', 'J': 'Valet'}
    _se = {'♠': '♠️', '♥': '♥️', '♦': '♦️', '♣': '♣️'}
    for _fc in ['A', 'K', 'Q', 'J']:
        for _s in ['♠', '♥', '♦', '♣']:
            EMOJI_MAP[f'🃏 {_fl[_fc]}{_se[_s]} Joueur'] = '🃏'
            EMOJI_MAP[f'🎴 {_fl[_fc]}{_se[_s]} Banquier'] = '🎴'

    NOTATION_MAP = {
        '🏆 Victoire Joueur':    'V1',
        '🏆 Victoire Banquier':  'V2',
        '🤝 Match Nul':          'X',
        '📊 Pair':               'Pa',
        '📊 Impair':             'I',
        '🎴 2/2':                '2/2',
        '🎴 2/3':                '2/3',
        '🎴 3/2':                '3/2',
        '🎴 3/3':                '3/3',
        '👤 Joueur 2K':          'Joueur 2K',
        '👤 Joueur 3K':          'Joueur 3K',
        '🏦 Banquier 2K':        'Banquier 2K',
        '🏦 Banquier 3K':        'Banquier 3K',
        '📈 Joueur Plus 6.5':    'Joueur+',
        '📉 Joueur Moins 4.5':   'Joueur-',
        '↔️ Joueur Neutre':      'Joueur=',
        '📈 Banquier Plus 6.5':  'Banquier+',
        '📉 Banquier Moins 4.5': 'Banquier-',
        '↔️ Banquier Neutre':    'Banquier=',
        '♠ Manque Joueur':       'Joueur ♠️',
        '♥ Manque Joueur':       'Joueur ❤️',
        '♦ Manque Joueur':       'Joueur ♦️',
        '♣ Manque Joueur':       'Joueur ♣️',
        '♠ Manque Banquier':     'Banquier ♠️',
        '♥ Manque Banquier':     'Banquier ❤️',
        '♦ Manque Banquier':     'Banquier ♦️',
        '♣ Manque Banquier':     'Banquier ♣️',
        '🃏 A Joueur':           'Joueur valeur A',
        '🃏 K Joueur':           'Joueur valeur K',
        '🃏 Q Joueur':           'Joueur valeur Q',
        '🃏 Valet Joueur':       'Joueur valeur Valet',
        '🎴 A Banquier':         'Banquier valeur A',
        '🎴 K Banquier':         'Banquier valeur K',
        '🎴 Q Banquier':         'Banquier valeur Q',
        '🎴 Valet Banquier':     'Banquier valeur Valet',
    }
    _fl2 = {'A': 'As', 'K': 'Roi', 'Q': 'Dame', 'J': 'Valet'}
    _se2 = {'♠': '♠️', '♥': '♥️', '♦': '♦️', '♣': '♣️'}
    for _fc2 in ['A', 'K', 'Q', 'J']:
        for _s2 in ['♠', '♥', '♦', '♣']:
            NOTATION_MAP[f'🃏 {_fl2[_fc2]}{_se2[_s2]} Joueur'] = f'Joueur {_fc2}{_se2[_s2]}'
            NOTATION_MAP[f'🎴 {_fl2[_fc2]}{_se2[_s2]} Banquier'] = f'Banquier {_fc2}{_se2[_s2]}'

    # Catégories exclues des prédictions (non pertinentes pour le joueur)
    EXCLUDED_CATS = {'↔️ Joueur Neutre', '↔️ Banquier Neutre'}

    # ─── Étape 1 : candidats par catégorie depuis analyse des manquements ───────
    # {cat_name: {game_num: confidence}}
    cat_candidates: dict[str, dict[int, int]] = {}

    for cat_name, data in pd.items():
        if cat_name in EXCLUDED_CATS:
            continue
        freq = data['freq']
        stats = data['stats']
        nums_raw = data['nums']

        if freq == 0 or stats['count'] < 2:
            continue

        nums = sorted(int(n) for n in nums_raw)
        ecarts = stats['all_ecarts']          # gaps historiques entre occurrences
        avg_ecart = stats['avg_ecart'] or 1
        max_ecart = stats['max_ecart'] or avg_ecart
        last_occ = stats['last_pos']
        current_ecart = stats['current_ecart']

        # Match Nul est rare : on prédit à partir de l'écart max historique
        cycle_ecart = max_ecart if cat_name == '🤝 Match Nul' else avg_ecart

        if not ecarts:
            continue

        # Urgence : catégorie en retard sur son cycle moyen
        overdue_ratio = current_ecart / avg_ecart if avg_ecart else 1.0
        overdue_bonus = min(25, int(max(0, overdue_ratio - 1.0) * 12))

        # Base de confiance de la catégorie
        base = freq * 100

        candidates: dict[int, int] = {}

        # Pondération des gaps : les gaps récents (derniers 30%) comptent double.
        # Cela permet au prédicateur de s'adapter aux changements de rythme récents.
        n_recent = max(1, len(ecarts) // 3)
        recent_ecarts = ecarts[-n_recent:]
        recent_avg = sum(recent_ecarts) / len(recent_ecarts) if recent_ecarts else avg_ecart
        # Cycle de prédiction = moyenne pondérée (70% historique + 30% récent)
        blended_ecart = int(avg_ecart * 0.7 + recent_avg * 0.3)
        if cat_name == '🤝 Match Nul':
            blended_ecart = max_ecart  # Match Nul toujours sur écart max

        # Bonus retard extrême : si la catégorie dépasse son propre écart max, priorité absolue
        extreme_overdue = current_ecart > max_ecart
        extreme_bonus = min(35, int((current_ecart - max_ecart) * 3)) if extreme_overdue else 0

        # Plancher de confiance : seulement pour les catégories avec peu de données
        # (évite que les catégories fréquentes dominent via floor artificiel)
        conf_floor = int(base * 0.72) if freq < 0.25 else int(base * 0.55)

        # Gaps pondérés : les gaps récents comptent 2×, les anciens 1×
        weighted_gaps = ecarts[:-n_recent] + recent_ecarts * 2
        total_weight = len(weighted_gaps)

        # Pour chaque gap historique unique, projeter dans la plage
        unique_gaps = sorted(set(ecarts))
        for gap in unique_gaps:
            # Fréquence pondérée du gap
            gap_weight = weighted_gaps.count(gap) / total_weight if total_weight else 0

            # Projeter ce gap sur 3 cycles à partir de la dernière occurrence
            for cycle in range(1, 4):
                projected = int(last_occ + gap + (cycle - 1) * blended_ecart)
                if from_num <= projected <= to_num:
                    cycle_decay = 0.9 ** (cycle - 1)
                    conf = int(base * gap_weight * 2.5 * cycle_decay
                               + base * 0.25
                               + overdue_bonus + extreme_bonus)
                    conf = max(conf, conf_floor)
                    conf = min(95, max(0, conf))
                    if conf >= min_confidence:
                        candidates[projected] = max(candidates.get(projected, 0), conf)

        # Prédictions sur le cycle pur (blended_ecart)
        for mult in range(1, 6):
            projected = int(last_occ + mult * blended_ecart)
            if from_num <= projected <= to_num:
                decay = 0.85 ** (mult - 1)
                conf = int(base * decay * min(2.0, overdue_ratio)
                           + overdue_bonus + extreme_bonus)
                conf = max(conf, conf_floor)
                conf = min(95, max(0, conf))
                if conf >= min_confidence:
                    candidates[projected] = max(candidates.get(projected, 0), conf)

        if not candidates:
            continue

        # ── Règle : pas de numéros consécutifs dans la même catégorie ──────────
        # Trier par confiance décroissante, puis garder seulement ceux espacés
        sorted_cands = sorted(candidates.items(), key=lambda x: -x[1])
        non_consec: list[tuple[int, int]] = []
        for g_num, conf in sorted_cands:
            if not any(abs(g_num - kept_num) <= 1 for kept_num, _ in non_consec):
                non_consec.append((g_num, conf))
            if len(non_consec) >= 15:
                break

        if non_consec:
            cat_candidates[cat_name] = {g: c for g, c in non_consec}

    # ─── Étape 2 : attribution exclusive (un numéro → une seule catégorie) ──────
    # Tri global : tous les (game_num, cat, conf) ensemble, meilleure conf d'abord.
    # On attribue chaque numéro à la catégorie la plus confiante,
    # et on limite à MAX_PER_CAT prédictions par catégorie pour forcer la diversité.
    MAX_PER_CAT = 4
    all_candidates: list[tuple[int, str, int]] = []
    for cat_name, cands in cat_candidates.items():
        for g_num, conf in cands.items():
            all_candidates.append((conf, g_num, cat_name))
    all_candidates.sort(reverse=True)  # meilleure confiance d'abord

    assignments: dict[int, tuple[str, int]] = {}  # game_num → (cat_name, conf)
    cat_counts: dict[str, int] = {}               # cat_name → nb attribués

    for conf, g_num, cat_name in all_candidates:
        if g_num in assignments:
            continue  # numéro déjà attribué
        if cat_counts.get(cat_name, 0) >= MAX_PER_CAT:
            continue  # catégorie déjà saturée
        assignments[g_num] = (cat_name, conf)
        cat_counts[cat_name] = cat_counts.get(cat_name, 0) + 1

    # ─── Étape 3 : regrouper par catégorie après attribution ────────────────────
    cat_groups: dict[str, list[tuple[int, int]]] = {}
    for g_num, (cat_name, conf) in sorted(assignments.items()):
        cat_groups.setdefault(cat_name, []).append((g_num, conf))

    # ─── Étape 4 : construire le résultat ───────────────────────────────────────
    result = {}
    for cat_name, preds in cat_groups.items():
        nums = [g for g, _ in preds]
        conf_avg = sum(c for _, c in preds) / len(preds)
        result[cat_name] = {
            'nums': sorted(nums),
            'conf_avg': round(conf_avg, 1),
            'emoji': EMOJI_MAP.get(cat_name, '🎯'),
            'notation': NOTATION_MAP.get(cat_name, cat_name.split()[-1]),
        }

    result = dict(sorted(result.items(), key=lambda x: -x[1]['conf_avg']))
    return result


def format_category_list(cat_results: dict, total_games: int,
                          from_num: int, to_num: int) -> list:
    """
    Formate les prédictions par catégorie en messages Telegram HTML.
    Chaque ligne de prédiction affiche la notation courte (V1, Pa, 2/3, J2K…)
    au lieu d'un simple compteur.
    """
    messages = []
    total_preds = sum(len(v['nums']) for v in cat_results.values())

    for cat_name, data in cat_results.items():
        nums = data['nums']
        conf_avg = data['conf_avg']
        notation = data['notation']
        if not nums:
            continue

        # Noms affichés complets
        _DISPLAY_NAMES = {
            '🏆 Victoire Joueur':    'Victoire Joueur',
            '🏆 Victoire Banquier':  'Victoire Banquier',
            '🤝 Match Nul':          'Match Nul',
            '📊 Pair':               'Pair',
            '📊 Impair':             'Impair',
            '🎴 2/2':                'Structure 2/2',
            '🎴 2/3':                'Structure 2/3',
            '🎴 3/2':                'Structure 3/2',
            '🎴 3/3':                'Structure 3/3',
            '👤 Joueur 2K':          'Joueur 2 cartes',
            '👤 Joueur 3K':          'Joueur 3 cartes',
            '🏦 Banquier 2K':        'Banquier 2 cartes',
            '🏦 Banquier 3K':        'Banquier 3 cartes',
            '📈 Joueur Plus 6.5':    'Joueur Plus 6.5',
            '📉 Joueur Moins 4.5':   'Joueur Moins 4.5',
            '↔️ Joueur Neutre':      'Joueur Neutre',
            '📈 Banquier Plus 6.5':  'Banquier Plus 6.5',
            '📉 Banquier Moins 4.5': 'Banquier Moins 4.5',
            '↔️ Banquier Neutre':    'Banquier Neutre',
            '♠ Manque Joueur':       'Prob ♠ Joueur',
            '♥ Manque Joueur':       'Prob ❤ Joueur',
            '♦ Manque Joueur':       'Prob ♦ Joueur',
            '♣ Manque Joueur':       'Prob ♣ Joueur',
            '♠ Manque Banquier':     'Prob ♠ Banquier',
            '♥ Manque Banquier':     'Prob ❤ Banquier',
            '♦ Manque Banquier':     'Prob ♦ Banquier',
            '♣ Manque Banquier':     'Prob ♣ Banquier',
            '🃏 A Joueur':           'As côté Joueur',
            '🃏 K Joueur':           'Roi côté Joueur',
            '🃏 Q Joueur':           'Dame côté Joueur',
            '🃏 Valet Joueur':       'Valet côté Joueur',
            '🎴 A Banquier':         'As côté Banquier',
            '🎴 K Banquier':         'Roi côté Banquier',
            '🎴 Q Banquier':         'Dame côté Banquier',
            '🎴 Valet Banquier':     'Valet côté Banquier',
        }
        clean_name = _DISPLAY_NAMES.get(cat_name,
                     cat_name.lstrip('🏆📊🎴👤🏦📈📉↔️♠️♥️♦️♣️🤝🃏 '))

        lines = [
            f"{data['emoji']} <b>{notation}</b> — {clean_name}",
            f"<i>Confiance : {conf_avg:.0f}%  |  {len(nums)} numéro(s)</i>",
            ""
        ]
        for num in nums:
            lines.append(f"#{num} — {notation} | ⏳")
        messages.append('\n'.join(lines))

    if not messages:
        return ["❌ Aucune prédiction générée pour cette plage.\n"
                "Essayez d'élargir la plage ou de charger plus de jeux."]

    # Résumé final
    nb_cats = len(cat_results)

    summary_lines = [
        f"📋 <b>RÉSUMÉ DES PRÉDICTIONS</b>",
        f"🎲 Basé sur {total_games} jeux  |  Plage #N{from_num} → #N{to_num}",
        f"🎯 {total_preds} prédiction(s) dans {nb_cats} catégorie(s)",
        "",
    ]

    # Liste chronologique simple : #numéro  notation
    all_entries = []
    for cat_name, data in cat_results.items():
        notation = data['notation']
        for num in data['nums']:
            all_entries.append((num, notation))

    all_entries.sort(key=lambda x: x[0])
    for num, notation in all_entries:
        summary_lines.append(f"#{num}  {notation}")

    messages.append('\n'.join(summary_lines))

    return messages


def conf_bar(conf: int) -> str:
    """Barre █ visuelle 10 cases."""
    filled = round(conf / 10)
    return '█' * filled + '░' * (10 - filled)


def format_global_summary(results: list) -> str:
    """Résumé global pour l'ancien format (compatibilité)."""
    from collections import Counter
    top3_counts = Counter()
    for r in results:
        for p in r['predictions'][:3]:
            top3_counts[p['category']] += 1
    total_games = len(results)
    lines = [f"📋 <b>RÉSUMÉ GLOBAL — {total_games} jeu(x) prédit(s)</b>\n"]
    for cat, cnt in top3_counts.most_common(8):
        pct = int(cnt / total_games * 100)
        lines.append(f"  {cat} : {cnt}× ({pct}%)  {conf_bar(pct)}")
    return '\n'.join(lines)
