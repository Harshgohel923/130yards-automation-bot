# hashtags.py — deterministic Instagram hashtags for a match.
"""
Five tags, always in this order:

    1. competition   — #UCL, #LaLiga, #PremierLeague, …
    2. home team     — #Liverpool
    3. away team     — #LeedsUnited
    4. home extra    — nickname, or a name the club is known by (#TheReds)
    5. away extra    — same for the away side

These are built in code rather than left to Gemini: the model invents
plausible-looking but wrong or match-abbreviation tags, and hashtags are the
one part of the caption that must be stable across posts.

Names are canonicalised through logo_fetch's NICKNAMES table, so whatever
spelling the scraper or matches.json uses lands on the same tag.
"""

from logo_fetch import (COMPETITION_DISPLAY, NICKNAMES, _slugify,
                        display_name, resolve_competition)

# Competition tag by canonical key. Short forms where the football internet
# uses them (#UCL, not #UEFAChampionsLeague); official names otherwise.
COMPETITION_TAGS = {
    'premier-league':         'PremierLeague',
    'efl-championship':       'EFLChampionship',
    'fa-cup':                 'FACup',
    'efl-cup':                'CarabaoCup',
    'community-shield':       'CommunityShield',
    'la-liga':                'LaLiga',
    'copa-del-rey':           'CopaDelRey',
    'supercopa-de-espana':    'Supercopa',
    'serie-a':                'SerieA',
    'coppa-italia':           'CoppaItalia',
    'bundesliga':             'Bundesliga',
    'dfb-pokal':              'DFBPokal',
    'ligue-1':                'Ligue1',
    'uefa-champions-league':  'UCL',
    'uefa-europa-league':     'UEL',
    'uefa-conference-league': 'UECL',
    'uefa-super-cup':         'UEFASuperCup',
    'fifa-club-world-cup':    'ClubWorldCup',
    'fifa-world-cup':         'FIFAWorldCup',
    'friendly':               'PreSeason',
}

# Tag 4/5 by canonical team slug — the club's nickname where it has a famous
# one, otherwise the name the fanbase actually uses.
TEAM_TAGS = {
    # England
    'liverpool':                 'TheReds',
    'manchester-united':         'RedDevils',
    'manchester-city':           'Cityzens',
    'arsenal':                   'Gunners',
    'chelsea':                   'TheBlues',
    'tottenham-hotspur':         'COYS',
    'newcastle-united':          'Magpies',
    'aston-villa':               'Villans',
    'west-ham-united':           'Hammers',
    'everton':                   'Toffees',
    'wolverhampton-wanderers':   'Wolves',
    'brighton-and-hove-albion':  'Seagulls',
    'crystal-palace':            'Eagles',
    'leeds-united':              'MarchingOnTogether',
    'nottingham-forest':         'TrickyTrees',
    'fulham':                    'Cottagers',
    'brentford':                 'Bees',
    'bournemouth':               'Cherries',
    'burnley':                   'Clarets',
    'sunderland':                'BlackCats',
    'leicester-city':            'Foxes',
    'southampton':               'SaintsFC',
    'newport-county':            'TheExiles',
    # Spain
    'real-madrid':               'HalaMadrid',
    'barcelona':                 'ForçaBarça',
    'atletico-madrid':           'Atleti',
    'sevilla':                   'Sevillistas',
    'real-betis':                'Béticos',
    'villarreal':                'YellowSubmarine',
    'athletic-club':             'LosLeones',
    'real-sociedad':             'LaReal',
    'valencia':                  'LosChés',
    'celta-vigo':                'Celtismo',
    'girona':                    'Gironistas',
    # Italy
    'juventus':                  'FinoAllaFine',
    'inter-milan':               'Nerazzurri',
    'ac-milan':                  'Rossoneri',
    'napoli':                    'Partenopei',
    'roma':                      'Giallorossi',
    'lazio':                     'Biancocelesti',
    'atalanta':                  'LaDea',
    'fiorentina':                'ForzaViola',
    'torino':                    'Granata',
    'bologna':                   'Rossoblù',
    # Germany
    'bayern-munich':             'MiaSanMia',
    'borussia-dortmund':         'BVB',
    'rb-leipzig':                'DieRotenBullen',
    'bayer-leverkusen':          'Werkself',
    'eintracht-frankfurt':       'SGE',
    'borussia-monchengladbach':  'DieFohlen',
    'vfb-stuttgart':             'VfB',
    'werder-bremen':             'Werder',
    'wolfsburg':                 'DieWölfe',
    # France
    'paris-saint-germain':       'ICICESTPARIS',
    'marseille':                 'DroitAuBut',
    'lyon':                      'TeamOL',
    'as-monaco':                 'DaghePrincipatu',
    'lille':                     'LOSC',
    'nice':                      'OGCN',
    'rennes':                    'SRFC',
    # Rest of Europe
    'benfica':                   'Glorioso',
    'fc-porto':                  'Dragões',
    'sporting-cp':               'LeõesDeAlvalade',
    'ajax':                      'DeGodenzonen',
    'psv':                       'PSVFans',
    'feyenoord':                 'GeenWoordenMaarDaden',
    'celtic':                    'TheHoops',
    'rangers':                   'TheGers',
    'galatasaray':               'CimBom',
    'fenerbahce':                'Kanarya',
    'besiktas':                  'KaraKartal',
    # Asia / friendlies opposition
    'urawa-red-diamonds':        'WeAreReds',
    'al-hilal':                  'AlZaeem',
    'al-nassr':                  'AlAlami',
    # National teams
    'england':                   'ThreeLions',
    'france':                    'LesBleus',
    'brazil':                    'Seleção',
    'argentina':                 'LaAlbiceleste',
    'spain':                     'LaRoja',
    'germany':                   'DieMannschaft',
    'portugal':                  'SeleçãoDasQuinas',
    'netherlands':               'Oranje',
    'italy':                     'Azzurri',
    'belgium':                   'RedDevilsBE',
    'croatia':                   'Vatreni',
    'united-states':             'USMNT',
    'mexico':                    'ElTri',
    'uruguay':                   'LaCeleste',
    'colombia':                  'LosCafeteros',
    'japan':                     'SamuraiBlue',
    'south-korea':               'TaegeukWarriors',
    'morocco':                   'AtlasLions',
    'nigeria':                   'SuperEagles',
    'senegal':                   'LionsOfTeranga',
    'ghana':                     'BlackStars',
    'egypt':                     'Pharaohs',
    'cameroon':                  'IndomitableLions',
    'australia':                 'Socceroos',
    'canada':                    'CanMNT',
    'denmark':                   'DanishDynamite',
    'sweden':                    'Blågult',
}


def _canonical(name: str) -> str:
    """Canonical slug for a team name — same resolution crest lookup uses."""
    slug = _slugify(name or '')
    return NICKNAMES.get(slug, slug)


def _tagify(text: str) -> str:
    """'Leeds United' -> 'LeedsUnited'; drops anything not alphanumeric."""
    return ''.join(ch for ch in (text or '') if ch.isalnum())


def competition_tag(competition: str | None) -> str:
    """#-less competition tag. Unknown competitions fall back to their name."""
    key = resolve_competition(competition)
    # Club and international friendlies share one canonical key but want
    # different tags — split them on the original wording.
    if key == 'friendly' and 'international' in _slugify(competition or ''):
        return 'InternationalFriendly'
    if key and key in COMPETITION_TAGS:
        return COMPETITION_TAGS[key]
    return _tagify(competition) or 'Football'


def team_tag(name: str) -> str:
    """Tag 2/3 — the team's own name."""
    return _tagify(name)


def team_extra_tag(name: str) -> str:
    """Tag 4/5 — nickname where we know one, otherwise a fan tag."""
    canonical = _canonical(name)
    if canonical in TEAM_TAGS:
        return TEAM_TAGS[canonical]
    return _tagify(display_name(canonical)) + 'Fans'


def build_hashtags(home: str, away: str, competition: str | None = None) -> str:
    """The full five-tag line, ready to append to a caption."""
    tags = [
        competition_tag(competition),
        team_tag(home),
        team_tag(away),
        team_extra_tag(home),
        team_extra_tag(away),
    ]
    # Duplicates read as sloppy — e.g. a club whose nickname is its own name.
    seen, unique = set(), []
    for t in tags:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)
    return ' '.join(f'#{t}' for t in unique)


def competition_label(competition: str | None) -> str:
    """Human-readable competition name for prompt copy."""
    key = resolve_competition(competition)
    if key:
        return COMPETITION_DISPLAY.get(key, competition or '')
    return competition or 'football'


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 3:
        print(build_hashtags(sys.argv[1], sys.argv[2],
                             sys.argv[3] if len(sys.argv) > 3 else None))
    else:
        print('usage: python hashtags.py "Home Team" "Away Team" [competition]')
