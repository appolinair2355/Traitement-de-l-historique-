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
    return {
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
        '📈 J.Plus 6.5':         cats['plusmoins_j']['Plus de 6,5'],
        '📉 J.Moins 4.5':        cats['plusmoins_j']['Moins de 4,5'],
        '↔️ J.Neutre':           cats['plusmoins_j']['Neutre'],
        '📈 B.Plus 6.5':         cats['plusmoins_b']['Plus de 6,5'],
        '📉 B.Moins 4.5':        cats['plusmoins_b']['Moins de 4,5'],
        '↔️ B.Neutre':           cats['plusmoins_b']['Neutre'],
        '♠ Manque J':            cats['missing_j']['♠'],
        '♥ Manque J':            cats['missing_j']['♥'],
        '♦ Manque J':            cats['missing_j']['♦'],
        '♣ Manque J':            cats['missing_j']['♣'],
        '♠ Manque B':            cats['missing_b']['♠'],
        '♥ Manque B':            cats['missing_b']['♥'],
        '♦ Manque B':            cats['missing_b']['♦'],
        '♣ Manque B':            cats['missing_b']['♣'],
    }


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
    Génère une liste de prédictions PAR CATÉGORIE.

    Règle d'exclusivité : chaque numéro de jeu n'apparaît que dans UNE seule
    catégorie — celle pour laquelle il a la confiance la plus haute.

    Retourne un dict ordonné :
      {
        cat_name: {
          'nums': [game_numbers...],
          'conf_avg': float,   # confiance moyenne de la catégorie
          'emoji': str,
        }
      }
    Seules les catégories avec au moins une prédiction sont incluses.
    Le dict est trié par confiance moyenne décroissante.
    """
    if not games:
        return {}

    pd = build_predict_data(games)
    if not pd:
        return {}

    all_nums = [int(g['numero']) for g in games]
    last_known = max(all_nums)

    # Étape 1 : pour chaque futur numéro, calculer la confiance par catégorie
    assignments: dict[int, tuple[str, int]] = {}   # {numero: (best_cat, best_conf)}

    for target in range(from_num, to_num + 1):
        delta = max(0, target - last_known)
        best_cat = None
        best_conf = -1

        for cat_name, data in pd.items():
            freq = data['freq']
            stats = data['stats']
            if freq == 0 or stats['count'] == 0:
                continue
            base_conf = freq * 100
            conf = _confidence(stats, freq, delta)
            # On ne garde que les catégories réellement "en avance sur leur cycle"
            # Seuil : confiance absolue >= min_confidence ET > fréquence de base
            if conf >= min_confidence and conf > base_conf:
                if conf > best_conf:
                    best_conf = conf
                    best_cat = cat_name

        if best_cat is not None:
            assignments[target] = (best_cat, best_conf)

    # Étape 2 : regrouper par catégorie
    cat_groups: dict[str, list] = {}
    cat_conf_sum: dict[str, float] = {}
    cat_conf_cnt: dict[str, int] = {}

    for num, (cat, conf) in sorted(assignments.items()):
        if cat not in cat_groups:
            cat_groups[cat] = []
            cat_conf_sum[cat] = 0
            cat_conf_cnt[cat] = 0
        cat_groups[cat].append(num)
        cat_conf_sum[cat] += conf
        cat_conf_cnt[cat] += 1

    # Emoji mapping pour les catégories
    EMOJI_MAP = {
        '🏆 Victoire Joueur':   '🏆',
        '🏆 Victoire Banquier': '🏆',
        '🤝 Match Nul':         '🤝',
        '📊 Pair':              '📊',
        '📊 Impair':            '📊',
        '🎴 2/2':               '🎴',
        '🎴 2/3':               '🎴',
        '🎴 3/2':               '🎴',
        '🎴 3/3':               '🎴',
        '👤 Joueur 2K':         '👤',
        '👤 Joueur 3K':         '👤',
        '🏦 Banquier 2K':       '🏦',
        '🏦 Banquier 3K':       '🏦',
        '📈 J.Plus 6.5':        '📈',
        '📉 J.Moins 4.5':       '📉',
        '↔️ J.Neutre':          '↔️',
        '📈 B.Plus 6.5':        '📈',
        '📉 B.Moins 4.5':       '📉',
        '↔️ B.Neutre':          '↔️',
        '♠ Manque J':           '♠️',
        '♥ Manque J':           '♥️',
        '♦ Manque J':           '♦️',
        '♣ Manque J':           '♣️',
        '♠ Manque B':           '♠️',
        '♥ Manque B':           '♥️',
        '♦ Manque B':           '♦️',
        '♣ Manque B':           '♣️',
    }

    # Notation courte pour chaque catégorie (affichée sur chaque ligne de prédiction)
    NOTATION_MAP = {
        '🏆 Victoire Joueur':   'V1',
        '🏆 Victoire Banquier': 'V2',
        '🤝 Match Nul':         'X',
        '📊 Pair':              'Pa',
        '📊 Impair':            'I',
        '🎴 2/2':               '2/2',
        '🎴 2/3':               '2/3',
        '🎴 3/2':               '3/2',
        '🎴 3/3':               '3/3',
        '👤 Joueur 2K':         'J2K',
        '👤 Joueur 3K':         'J3K',
        '🏦 Banquier 2K':       'B2K',
        '🏦 Banquier 3K':       'B3K',
        '📈 J.Plus 6.5':        'J+',
        '📉 J.Moins 4.5':       'J-',
        '↔️ J.Neutre':          'J=',
        '📈 B.Plus 6.5':        'B+',
        '📉 B.Moins 4.5':       'B-',
        '↔️ B.Neutre':          'B=',
        '♠ Manque J':           '♠J',
        '♥ Manque J':           '❤J',
        '♦ Manque J':           '♦J',
        '♣ Manque J':           '♣J',
        '♠ Manque B':           '♠B',
        '♥ Manque B':           '❤B',
        '♦ Manque B':           '♦B',
        '♣ Manque B':           '♣B',
    }

    # Étape 3 : construire le résultat trié par confiance moyenne décroissante
    result = {}
    sorted_cats = sorted(
        cat_groups.keys(),
        key=lambda c: -(cat_conf_sum[c] / max(cat_conf_cnt[c], 1))
    )
    for cat in sorted_cats:
        conf_avg = cat_conf_sum[cat] / max(cat_conf_cnt[cat], 1)
        result[cat] = {
            'nums': sorted(cat_groups[cat]),
            'conf_avg': round(conf_avg, 1),
            'emoji': EMOJI_MAP.get(cat, '🎯'),
            'notation': NOTATION_MAP.get(cat, cat.split()[-1]),
        }

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

        # Noms affichés complets (probabilité d'apparition, pas "manquant")
        _DISPLAY_NAMES = {
            '🏆 Victoire Joueur':   'Victoire Joueur',
            '🏆 Victoire Banquier': 'Victoire Banquier',
            '🤝 Match Nul':         'Match Nul',
            '📊 Pair':              'Pair',
            '📊 Impair':            'Impair',
            '🎴 2/2':               'Structure 2/2',
            '🎴 2/3':               'Structure 2/3',
            '🎴 3/2':               'Structure 3/2',
            '🎴 3/3':               'Structure 3/3',
            '👤 Joueur 2K':         'Joueur 2 cartes',
            '👤 Joueur 3K':         'Joueur 3 cartes',
            '🏦 Banquier 2K':       'Banquier 2 cartes',
            '🏦 Banquier 3K':       'Banquier 3 cartes',
            '📈 J.Plus 6.5':        'Joueur Plus 6.5',
            '📉 J.Moins 4.5':       'Joueur Moins 4.5',
            '↔️ J.Neutre':          'Joueur Neutre',
            '📈 B.Plus 6.5':        'Banquier Plus 6.5',
            '📉 B.Moins 4.5':       'Banquier Moins 4.5',
            '↔️ B.Neutre':          'Banquier Neutre',
            '♠ Manque J':           'Prob ♠ Joueur',
            '♥ Manque J':           'Prob ❤ Joueur',
            '♦ Manque J':           'Prob ♦ Joueur',
            '♣ Manque J':           'Prob ♣ Joueur',
            '♠ Manque B':           'Prob ♠ Banquier',
            '♥ Manque B':           'Prob ❤ Banquier',
            '♦ Manque B':           'Prob ♦ Banquier',
            '♣ Manque B':           'Prob ♣ Banquier',
        }
        clean_name = _DISPLAY_NAMES.get(cat_name,
                     cat_name.lstrip('🏆📊🎴👤🏦📈📉↔️♠️♥️♦️♣️🤝 '))

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
        f"🎲 Basé sur {total_games} jeux analysés",
        f"📐 Plage : #N{from_num} → #N{to_num}",
        f"🎯 {total_preds} prédiction(s) en {nb_cats} catégorie(s)",
        "",
    ]
    for cat_name, data in cat_results.items():
        notation = data['notation']
        nums_str = ', '.join(f'#{n}' for n in data['nums'][:6])
        if len(data['nums']) > 6:
            nums_str += f' … (+{len(data["nums"])-6})'
        summary_lines.append(
            f"{data['emoji']} <b>{notation}</b> ({data['conf_avg']:.0f}%) : {nums_str}"
        )
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
