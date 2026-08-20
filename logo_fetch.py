# logo_fetch.py
"""
Team logo fetcher.

Downloads 3000x3000 PNG logos from football-logos.cc and uploads them to
Cloudinary under:
    assets/national/<slug>   ← national teams
    assets/club/<slug>       ← clubs

The source site is a static Astro site whose asset URLs embed a per-size
content hash, so URLs cannot be constructed blindly — the hash has to be read
off the team page first:

    1. GET https://football-logos.cc/<country>/<slug>/
    2. scrape  <option value="3000::<hash>">  from the download dropdown
    3. GET https://images.football-logos.cc/<country>/3000/<slug>.<hash>.png

Both a browser User-Agent and a Referer header are required on the image host
(a missing UA returns 403, a missing Referer returns 404).

Team lookup uses the site's image sitemap (~3.6k teams), cached locally at
data/logo_index.json and refreshed automatically once it is older than
INDEX_MAX_AGE_DAYS.

Usage:
    from logo_fetch import fetch_logo

    fetch_logo('Argentina')              # -> assets/national/argentina
    fetch_logo('Liverpool')              # -> assets/club/liverpool
    fetch_logo('england/liverpool')      # explicit, skips name resolution

CLI:
    python logo_fetch.py Argentina Egypt
    python logo_fetch.py --club Arsenal
"""

import difflib
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET

import cloudinary
import cloudinary.api
import cloudinary.uploader
import requests
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)

SITE            = 'https://football-logos.cc'
IMAGE_HOST      = 'https://images.football-logos.cc'
IMAGE_SITEMAP   = f'{SITE}/image-sitemap.xml.gz'
NATIONAL_SUFFIX = '-national-team'

# The image host rejects non-browser clients (403) and requests without a
# referer (404) — both headers are mandatory, not cosmetic.
BROWSER_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0 Safari/537.36'
)
HEADERS = {'User-Agent': BROWSER_UA, 'Referer': f'{SITE}/'}

INDEX_PATH         = 'data/logo_index.json'
INDEX_VERSION      = 2
INDEX_MAX_AGE_DAYS = 30
LOCAL_CACHE_DIR    = 'assets/logos'

# ~18 slugs exist in more than one country (liverpool: england + uruguay,
# everton: england + chile, river-plate: argentina + uruguay, …). When a bare
# name is ambiguous, prefer the bigger league; pass 'country/slug' to override.
COUNTRY_PRIORITY = [
    'england', 'spain', 'italy', 'germany', 'france', 'portugal',
    'netherlands', 'brazil', 'argentina', 'usa', 'mexico', 'saudi-arabia',
]

NATIONAL_FOLDER    = 'assets/national'
CLUB_FOLDER        = 'assets/club'
COMPETITION_FOLDER = 'assets/competition'

# ── name normalization ────────────────────────────────────────────────────────
#
# Canonical slugs mirror the official FIFA display names used in config.py
# ("United States" → 'united-states', "Türkiye" → 'turkiye'), so Cloudinary
# public_ids always match the bot's team names — never the source site's
# spelling quirks. Two tables drive this:
#
#   NICKNAMES       every known variant (slugified) → canonical slug
#   SITE_OVERRIDES  canonical slug → where it lives on football-logos.cc,
#                   for the teams the site spells differently

# variant → canonical. Keys and values are slugified; accents are already
# stripped by _slugify, so "Côte d'Ivoire" arrives as 'cote-d-ivoire'.
NICKNAMES = {
    # ── national teams ──
    'usa': 'united-states',
    'us': 'united-states',
    'united-states-of-america': 'united-states',
    'holland': 'netherlands',
    'czech': 'czechia',
    'czech-republic': 'czechia',
    'turkey': 'turkiye',
    'korea': 'south-korea',
    'korea-republic': 'south-korea',
    'republic-of-korea': 'south-korea',
    'korea-dpr': 'north-korea',
    'dpr-korea': 'north-korea',
    'ivory-coast': 'cote-d-ivoire',
    'cote-divoire': 'cote-d-ivoire',
    'congo-dr': 'dr-congo',
    'drc': 'dr-congo',
    'congo-kinshasa': 'dr-congo',
    'democratic-republic-of-the-congo': 'dr-congo',
    'cape-verde': 'cabo-verde',
    'ir-iran': 'iran',
    'bosnia': 'bosnia-and-herzegovina',
    'bosnia-herzegovina': 'bosnia-and-herzegovina',
    'united-arab-emirates': 'uae',
    'emirates': 'uae',
    'columbia': 'colombia',
    'macedonia': 'north-macedonia',
    # ── clubs: England ──
    'man-utd': 'manchester-united',
    'man-united': 'manchester-united',
    'manchester-utd': 'manchester-united',
    'mufc': 'manchester-united',
    'man-city': 'manchester-city',
    'mcfc': 'manchester-city',
    'spurs': 'tottenham-hotspur',
    'tottenham': 'tottenham-hotspur',
    'wolves': 'wolverhampton-wanderers',
    'wolverhampton': 'wolverhampton-wanderers',
    'newcastle': 'newcastle-united',
    'nufc': 'newcastle-united',
    'west-ham': 'west-ham-united',
    'forest': 'nottingham-forest',
    'leeds': 'leeds-united',
    'brighton-hove-albion': 'brighton',
    # ── clubs: Spain ──
    'barca': 'barcelona',
    'fc-barcelona': 'barcelona',
    'real-madrid-cf': 'real-madrid',
    'atletico': 'atletico-madrid',
    'atleti': 'atletico-madrid',
    'atletico-de-madrid': 'atletico-madrid',
    'athletic-bilbao': 'athletic-club',
    'bilbao': 'athletic-club',
    'betis': 'real-betis',
    'sociedad': 'real-sociedad',
    'vallecano': 'rayo-vallecano',      # the scraper's own spelling
    'rayo': 'rayo-vallecano',
    'rcd-mallorca': 'mallorca',
    'rcd-espanyol': 'espanyol',
    'deportivo-alaves': 'deportivo',
    'alaves': 'deportivo',
    # ── clubs: Mexico / MLS ──
    'atletico-san-luis': 'atletico-de-san-luis',
    'inter-miami-cf': 'inter-miami',
    # ── clubs: Italy ──
    'inter': 'inter-milan',
    'internazionale': 'inter-milan',
    'milan': 'ac-milan',
    'juve': 'juventus',
    'as-roma': 'roma',
    'ss-lazio': 'lazio',
    'ssc-napoli': 'napoli',
    # ── clubs: Germany ──
    'bayern': 'bayern-munich',
    'fc-bayern': 'bayern-munich',
    'bayern-munchen': 'bayern-munich',
    'fc-bayern-munich': 'bayern-munich',
    'dortmund': 'borussia-dortmund',
    'bvb': 'borussia-dortmund',
    'leverkusen': 'bayer-leverkusen',
    'leipzig': 'rb-leipzig',
    'gladbach': 'borussia-monchengladbach',
    # ── clubs: France ──
    'psg': 'paris-saint-germain',
    'paris-sg': 'paris-saint-germain',
    'olympique-marseille': 'marseille',
    'olympique-de-marseille': 'marseille',
    'olympique-lyonnais': 'lyon',
    'monaco': 'as-monaco',
    # ── clubs: Portugal / Netherlands ──
    'porto': 'fc-porto',
    'sporting': 'sporting-cp',
    'sporting-lisbon': 'sporting-cp',
    'sl-benfica': 'benfica',
    'psv-eindhoven': 'psv',
    'afc-ajax': 'ajax',
    # ── clubs: rest of world ──
    # (inter-miami-cf is listed with the MLS clubs above)
    'boca': 'boca-juniors',
    'alnassr': 'al-nassr',
    'al-nassr-fc': 'al-nassr',          # the scraper's own spelling
    'alhilal': 'al-hilal',
    'jeju-united': 'jeju-sk',
    'jeju-sk-fc': 'jeju-sk',
    'newport': 'newport-county',
    'newport-county-afc': 'newport-county',
}

# canonical slug → official display name, for teams whose display form isn't
# just the slug title-cased ('manchester-united' → 'Manchester United' needs
# no entry; 'ac-milan' → 'AC Milan' does).
DISPLAY_NAMES = {
    'cote-d-ivoire': "Côte d'Ivoire",
    'turkiye': 'Türkiye',
    'curacao': 'Curaçao',
    'dr-congo': 'DR Congo',
    'uae': 'UAE',
    'bosnia-and-herzegovina': 'Bosnia and Herzegovina',
    'ac-milan': 'AC Milan',
    'as-monaco': 'AS Monaco',
    'atletico-de-san-luis': 'Atlético de San Luis',
    'psv': 'PSV',
    'rb-leipzig': 'RB Leipzig',
    'fc-porto': 'FC Porto',
    'sporting-cp': 'Sporting CP',
    'paris-saint-germain': 'Paris Saint-Germain',
    'borussia-monchengladbach': 'Borussia Mönchengladbach',
    'atletico-madrid': 'Atlético Madrid',
    'fenerbahce': 'Fenerbahçe',
    'besiktas': 'Beşiktaş',
    'al-nassr': 'Al-Nassr',
    'al-hilal': 'Al-Hilal',
    'jeju-sk': 'Jeju SK',
    'club-america': 'Club América',
}

# canonical slug → (country, site_slug, is_national) — only for teams the
# source site spells differently from their canonical name.
SITE_OVERRIDES = {
    'united-states':          ('usa', 'usa-national-team', True),
    'netherlands':            ('netherlands', 'dutch-national-team', True),
    'czechia':                ('czech-republic', 'czech-republic-national-team', True),
    'turkiye':                ('turkey', 'turkey-national-team', True),
    'dr-congo':               ('congo-dr', 'congo-dr-national-team', True),
    'portugal':               ('portugal', 'portuguese-football-federation', True),
    'tottenham-hotspur':      ('england', 'tottenham', False),
    'wolverhampton-wanderers': ('england', 'wolves', False),
    'newcastle-united':       ('england', 'newcastle', False),
    'west-ham-united':        ('england', 'west-ham', False),
    'bayern-munich':          ('germany', 'bayern-munchen', False),
    'ac-milan':               ('italy', 'milan', False),
    'inter-milan':            ('italy', 'inter', False),
    'inter-miami':            ('usa', 'inter-miami-cf', False),
    'jeju-sk':                ('south-korea', 'jeju-sk-fc', False),
    'newport-county':         ('england', 'newport', False),
}


def _slugify(name: str) -> str:
    """'Manchester United' -> 'manchester-united'; strips accents (Türkiye -> turkiye)."""
    slug = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    slug = slug.strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')


# ── team index ────────────────────────────────────────────────────────────────

def _download_index() -> dict:
    """Build {slug: [countries]} for every team page listed in the image sitemap."""
    response = requests.get(IMAGE_SITEMAP, headers=HEADERS, timeout=30)
    response.raise_for_status()

    xml  = gzip.GzipFile(fileobj=io.BytesIO(response.content)).read()
    root = ET.fromstring(xml)
    ns   = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

    teams: dict[str, list[str]] = {}
    for loc in root.findall('.//sm:url/sm:loc', ns):
        path = (loc.text or '').removeprefix(SITE).strip('/')
        parts = path.split('/')
        # Team pages are exactly <country>/<slug>; deeper paths are logo
        # variants (…/logo-history/, …/white/) and are not downloadable pages.
        if len(parts) == 2 and all(parts):
            countries = teams.setdefault(parts[1], [])
            if parts[0] not in countries:
                countries.append(parts[0])

    return {'version': INDEX_VERSION, 'built_at': time.time(), 'teams': teams}


def _pick_country(countries: list[str], slug: str) -> str:
    """Choose one country for a slug that exists in several."""
    if len(countries) == 1:
        return countries[0]

    for country in COUNTRY_PRIORITY:
        if country in countries:
            others = [c for c in countries if c != country]
            print(f"[logos] '{slug}' exists in {', '.join(sorted(countries))} — "
                  f"using {country}. Pass '<country>/{slug}' for {', '.join(sorted(others))}.")
            return country

    return sorted(countries)[0]


def _load_index(force_refresh: bool = False) -> dict:
    """Return the cached team index, rebuilding it if missing or stale."""
    if not force_refresh and os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH) as f:
                index = json.load(f)
            age_days = (time.time() - index.get('built_at', 0)) / 86400
            if (index.get('version') == INDEX_VERSION
                    and age_days < INDEX_MAX_AGE_DAYS
                    and index.get('teams')):
                return index
        except (json.JSONDecodeError, OSError):
            pass  # corrupt cache — fall through and rebuild

    print('[logos] Building team index from sitemap…')
    index = _download_index()
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, 'w') as f:
        json.dump(index, f)
    print(f"[logos] Indexed {len(index['teams'])} teams → {INDEX_PATH}")
    return index


def resolve_team(name: str, kind: str | None = None) -> tuple[str, str, bool, str]:
    """
    Resolve a team name to (country, site_slug, is_national, canonical).

    name: 'Argentina', 'Spurs', or an explicit 'england/liverpool'.
    kind: 'national' or 'club' to disambiguate; None auto-detects, preferring
          a national team when both exist (e.g. 'Argentina').

    canonical is the slug used for the Cloudinary public_id — derived from the
    team's official name, not from how the source site spells it.

    Raises LookupError if the team cannot be found.
    """
    teams = _load_index()['teams']

    # Explicit 'country/slug' bypasses resolution entirely. A slug shared by
    # clubs in several countries keeps its bare canonical only for the
    # priority-league club; the others get a country-suffixed public_id so
    # e.g. uruguay/nacional can never overwrite Paraguay's Nacional crest.
    if '/' in name:
        country, slug = name.strip('/').split('/', 1)
        is_national = slug.endswith(NATIONAL_SUFFIX)
        canonical = slug.removesuffix(NATIONAL_SUFFIX)
        countries = teams.get(slug, [])
        if len(countries) > 1:
            primary = next((c for c in COUNTRY_PRIORITY if c in countries),
                           sorted(countries)[0])
            if country != primary:
                canonical = f'{canonical}-{country}'
        return country, slug, is_national, canonical

    canonical = _slugify(name)
    canonical = NICKNAMES.get(canonical, canonical)

    if canonical in SITE_OVERRIDES:
        country, site_slug, is_national = SITE_OVERRIDES[canonical]
        return country, site_slug, is_national, canonical

    candidates = []
    if kind != 'club':
        candidates.append(canonical if canonical.endswith(NATIONAL_SUFFIX)
                          else canonical + NATIONAL_SUFFIX)
    if kind != 'national':
        candidates.append(canonical)

    for candidate in candidates:
        if candidate in teams:
            country = _pick_country(teams[candidate], candidate)
            return (country, candidate, candidate.endswith(NATIONAL_SUFFIX),
                    canonical.removesuffix(NATIONAL_SUFFIX))

    # Nothing exact — offer the closest slugs so the caller can correct it.
    pool = [s for s in teams if kind != 'club' or not s.endswith(NATIONAL_SUFFIX)]
    close = difflib.get_close_matches(canonical, pool, n=5, cutoff=0.6)
    hint = f" Did you mean: {', '.join(close)}?" if close else ''
    raise LookupError(f"No logo found for '{name}' (tried: {', '.join(candidates)}).{hint}")


# ── competitions ──────────────────────────────────────────────────────────────
#
# Same recipe as team names: canonical key → where the logo lives on the
# source site, plus an alias table absorbing every spelling TheSportsDB, the
# scraper, or the user might produce. A value of None means "no logo on the
# site" — the card falls back to the 130 Yards brand logo (friendlies).

# (country, site_slug, variant). The scorecard templates are all dark stadium
# photos, so the white artwork variant is preferred wherever the site has one
# — a navy UCL starball is invisible on a navy background.
COMPETITIONS = {
    'premier-league':         ('england', 'english-premier-league', None),
    'efl-championship':       ('england', 'efl-championship', None),
    'fa-cup':                 ('england', 'emirates-fa-cup', None),
    'efl-cup':                ('england', 'efl-cup', None),
    'community-shield':       ('england', 'fa-community-shield', None),
    'la-liga':                ('spain', 'la-liga', None),
    'copa-del-rey':           ('spain', 'copa-del-rey', None),
    'supercopa-de-espana':    ('spain', 'supercopa-de-espana', None),
    'serie-a':                ('italy', 'serie-a', None),
    'coppa-italia':           ('italy', 'coppa-italia', None),
    'bundesliga':             ('germany', 'bundesliga', None),
    'dfb-pokal':              ('germany', 'dfb-pokal', None),
    'ligue-1':                ('france', 'ligue-1', 'white'),
    'uefa-champions-league':  ('tournaments', 'uefa-champions-league', 'no-text-white'),
    'uefa-europa-league':     ('tournaments', 'uefa-europa-league', None),
    'uefa-conference-league': ('tournaments', 'uefa-conference-league', None),
    'uefa-super-cup':         ('tournaments', 'uefa-super-cup', 'white'),
    'fifa-club-world-cup':    ('tournaments', 'fifa-club-world-cup', None),
    'fifa-world-cup':         ('tournaments', 'fifa-world-cup-2026', 'white'),
    'concacaf-champions-cup': ('tournaments', 'concacaf-champions-cup', None),
    'mls':                    ('usa', 'mls', None),
    'saudi-pro-league':       ('saudi-arabia', 'saudi-professional-league', None),
    # Not on football-logos.cc — the logo has to be uploaded by hand:
    #   python logo_fetch.py --local <file.png> --competition "Leagues Cup"
    'leagues-cup':            None,
    'friendly':               None,
}

COMPETITION_ALIASES = {
    'english-premier-league': 'premier-league',
    'epl': 'premier-league',
    'pl': 'premier-league',
    'championship': 'efl-championship',
    'english-league-championship': 'efl-championship',
    'emirates-fa-cup': 'fa-cup',
    'english-fa-cup': 'fa-cup',
    'carabao-cup': 'efl-cup',
    'league-cup': 'efl-cup',
    'english-league-cup': 'efl-cup',
    'fa-community-shield': 'community-shield',
    'laliga': 'la-liga',
    'la-liga-ea-sports': 'la-liga',
    'spanish-la-liga': 'la-liga',
    'primera-division': 'la-liga',
    'spanish-copa-del-rey': 'copa-del-rey',
    'spanish-super-cup': 'supercopa-de-espana',
    'italian-serie-a': 'serie-a',
    'italian-coppa-italia': 'coppa-italia',
    'german-bundesliga': 'bundesliga',
    '1-bundesliga': 'bundesliga',
    'german-dfb-pokal': 'dfb-pokal',
    'french-ligue-1': 'ligue-1',
    'ligue1': 'ligue-1',
    'ligue-1-mcdonald-s': 'ligue-1',
    'champions-league': 'uefa-champions-league',
    'ucl': 'uefa-champions-league',
    'europa-league': 'uefa-europa-league',
    'uel': 'uefa-europa-league',
    'conference-league': 'uefa-conference-league',
    'uecl': 'uefa-conference-league',
    'europa-conference-league': 'uefa-conference-league',
    'uefa-europa-conference-league': 'uefa-conference-league',
    'super-cup': 'uefa-super-cup',
    'club-world-cup': 'fifa-club-world-cup',
    'cwc': 'fifa-club-world-cup',
    'world-cup': 'fifa-world-cup',
    'fifa-world-cup-2026': 'fifa-world-cup',
    'leagues-cup-2026': 'leagues-cup',
    'concacaf-champions-league': 'concacaf-champions-cup',
    'ccc': 'concacaf-champions-cup',
    'major-league-soccer': 'mls',
    'usa-mls': 'mls',
    # The site files it as "saudi-professional-league"; everyone else, and the
    # scraper, calls it something shorter.
    'saudi-professional-league': 'saudi-pro-league',
    'saudi-pl': 'saudi-pro-league',
    'saudi-league': 'saudi-pro-league',
    'roshn-saudi-league': 'saudi-pro-league',
    'spl': 'saudi-pro-league',
    'friendlies': 'friendly',
    'club-friendly': 'friendly',
    'club-friendlies': 'friendly',
    'club-friendly-games': 'friendly',
    'pre-season': 'friendly',
    'pre-season-friendly': 'friendly',
    'preseason-friendly': 'friendly',
    'international-friendly': 'friendly',
}

COMPETITION_DISPLAY = {
    'premier-league': 'Premier League',
    'efl-championship': 'EFL Championship',
    'fa-cup': 'FA Cup',
    'efl-cup': 'EFL Cup',
    'community-shield': 'Community Shield',
    'la-liga': 'La Liga',
    'copa-del-rey': 'Copa del Rey',
    'supercopa-de-espana': 'Supercopa de España',
    'serie-a': 'Serie A',
    'coppa-italia': 'Coppa Italia',
    'bundesliga': 'Bundesliga',
    'dfb-pokal': 'DFB-Pokal',
    'ligue-1': 'Ligue 1',
    'uefa-champions-league': 'UEFA Champions League',
    'uefa-europa-league': 'UEFA Europa League',
    'uefa-conference-league': 'UEFA Conference League',
    'uefa-super-cup': 'UEFA Super Cup',
    'fifa-club-world-cup': 'FIFA Club World Cup',
    'fifa-world-cup': 'FIFA World Cup',
    'concacaf-champions-cup': 'CONCACAF Champions Cup',
    'mls': 'MLS',
    'saudi-pro-league': 'Saudi Pro League',
    'leagues-cup': 'Leagues Cup',
    'friendly': 'Club Friendly',
}


def resolve_competition(name: str | None) -> str | None:
    """Canonical competition key for any spelling, or None if unknown."""
    slug = _slugify(name or '')
    slug = COMPETITION_ALIASES.get(slug, slug)
    return slug if slug in COMPETITIONS else None


def fetch_competition_logo(name: str, size: int = 3000, force: bool = False) -> str | None:
    """
    Fetch a competition logo to Cloudinary (assets/competition/<key>).

    Returns the secure URL, or None for competitions that deliberately have
    no logo (friendlies — the card uses the brand logo instead).
    Raises LookupError for competitions not in COMPETITIONS.
    """
    key = resolve_competition(name)
    if key is None:
        known = ', '.join(sorted(COMPETITIONS))
        raise LookupError(f"Unknown competition '{name}'. Known: {known}")

    site = COMPETITIONS[key]
    if site is None:
        print(f"[logos] '{name}' is a friendly — the card uses the 130 Yards brand logo.")
        return None

    public_id = f'{COMPETITION_FOLDER}/{key}'
    if not force:
        try:
            existing = cloudinary.api.resource(public_id)
            print(f'[logos] Already on Cloudinary: {public_id}')
            return existing['secure_url']
        except cloudinary.api.NotFound:
            pass

    country, site_slug, variant = site
    local_path = download_logo(country, site_slug, size, variant=variant)
    result = cloudinary.uploader.upload(
        local_path,
        public_id=public_id,
        overwrite=True,
        invalidate=True,
    )
    print(f'[logos] Uploaded {key} → {public_id}')
    return result['secure_url']


def crest_candidates(name: str) -> list[str]:
    """
    Possible Cloudinary public_ids for a team name, most likely first.

    Pure string work — no site index, no network — so the render pipeline can
    call it mid-match without new failure modes. When the name alone can't
    tell national from club, both folders are returned and the caller checks
    which one actually exists on Cloudinary.
    """
    canonical = _slugify(name)
    canonical = NICKNAMES.get(canonical, canonical)
    if canonical in SITE_OVERRIDES:
        return [logo_public_id(canonical, SITE_OVERRIDES[canonical][2])]
    return [f'{NATIONAL_FOLDER}/{canonical}', f'{CLUB_FOLDER}/{canonical}']


# Slug tokens that are acronyms and must stay uppercase in display names.
_ACRONYMS = {'fc', 'cf', 'sc', 'ac', 'cd', 'sd', 'ca', 'cp', 'fk', 'nk',
             'bk', 'sk', 'afc', 'cfr', 'aek', 'ogc', 'az', 'psv', 'rkc', 'nec'}


def display_name(canonical: str) -> str:
    """'manchester-united' -> 'Manchester United', 'fc-ryukyu' -> 'FC Ryukyu'."""
    if canonical in DISPLAY_NAMES:
        return DISPLAY_NAMES[canonical]
    return ' '.join(w.upper() if w in _ACRONYMS else w.capitalize()
                    for w in canonical.split('-'))


def normalize_team_name(name: str, kind: str | None = None) -> str:
    """
    Official display name for any spelling a data source might use:
    'Man Utd' -> 'Manchester United', 'Internazionale' -> 'Inter Milan',
    'Korea Republic' -> 'South Korea'.

    Raises LookupError if the team is unknown (with did-you-mean hints).
    """
    _, _, _, canonical = resolve_team(name, kind)
    return display_name(canonical)


# ── download ──────────────────────────────────────────────────────────────────

def _scrape_hash(country: str, slug: str, size: int = 3000,
                 variant: str | None = None) -> str:
    """
    Read the content hash for the requested size off the team page.

    variant: an alternate artwork sub-page ('white', 'no-text-white') — these
    live at /<country>/<slug>/<variant>/ and have their own hashes.
    """
    url = f'{SITE}/{country}/{slug}/' + (f'{variant}/' if variant else '')
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()

    match = re.search(rf'value="{size}::([a-z0-9]+)"', response.text)
    if not match:
        available = sorted(set(re.findall(r'value="(\d+)::', response.text)), key=int)
        raise RuntimeError(
            f"No {size}px logo for {country}/{slug}"
            + (f" (available: {', '.join(available)})" if available else ' (page has no downloads)')
        )
    return match.group(1)


def download_logo(country: str, slug: str, size: int = 3000,
                  dest_dir: str = LOCAL_CACHE_DIR, variant: str | None = None) -> str:
    """Download a logo PNG to dest_dir and return the local path."""
    os.makedirs(dest_dir, exist_ok=True)
    # Country is part of the cache key: ~18 slugs exist in several countries
    # (england/liverpool vs uruguay/liverpool would otherwise collide).
    suffix = f'--{variant}' if variant else ''
    local_path = os.path.join(dest_dir, f'{country}--{slug}{suffix}.png')

    if os.path.exists(local_path):
        return local_path

    logo_hash = _scrape_hash(country, slug, size, variant)
    url = f'{IMAGE_HOST}/{country}/{size}/{slug}{suffix}.{logo_hash}.png'

    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    content_type = response.headers.get('content-type', '')
    if not content_type.startswith('image/'):
        raise RuntimeError(f'Expected an image from {url}, got {content_type!r}')

    with open(local_path, 'wb') as f:
        f.write(response.content)

    print(f'[logos] Downloaded {slug} ({size}px, {len(response.content) // 1024} KB)')
    return local_path


# ── cloudinary ────────────────────────────────────────────────────────────────

def logo_public_id(canonical: str, is_national: bool) -> str:
    """
    Deterministic Cloudinary public_id from the canonical team slug:
    assets/national/united-states, assets/club/tottenham-hotspur — always the
    team's own name, never the source site's spelling.
    """
    folder = NATIONAL_FOLDER if is_national else CLUB_FOLDER
    return f'{folder}/{canonical}'


def fetch_logo(name: str, kind: str | None = None, size: int = 3000, force: bool = False) -> str:
    """
    Fetch a team logo and upload it to Cloudinary.

    name:  'Argentina', 'Liverpool', or explicit 'england/liverpool'
    kind:  'national' | 'club' | None (auto-detect, prefers national)
    force: re-download and overwrite even if it already exists on Cloudinary

    Returns the Cloudinary secure URL.
    """
    country, slug, is_national, canonical = resolve_team(name, kind)
    public_id = logo_public_id(canonical, is_national)

    if not force:
        try:
            existing = cloudinary.api.resource(public_id)
            print(f'[logos] Already on Cloudinary: {public_id}')
            return existing['secure_url']
        except cloudinary.api.NotFound:
            pass

    local_path = download_logo(country, slug, size)

    result = cloudinary.uploader.upload(
        local_path,
        public_id=public_id,
        overwrite=True,
        invalidate=True,
    )
    url = result['secure_url']
    print(f'[logos] Uploaded {slug} → {public_id}')
    return url


def upload_local_crest(path: str, name: str, kind: str | None = None,
                       force: bool = False) -> str:
    """
    Upload a crest image from disk for a team the source site doesn't have.

    The file lands at the same deterministic public_id fetch_logo would use
    ('FC Ryukyu' → assets/club/fc-ryukyu), so get_crest_url finds it with no
    other changes. Pass kind='national' for a national side; default is club.

    Refuses to replace a crest that already exists unless force=True — a name
    that happens to be a nickname of a known club ('Inter') would otherwise
    silently overwrite the real crest.

    Returns the Cloudinary secure URL.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f'No such file: {path}')

    if kind == 'competition':
        canonical = resolve_competition(name) or _slugify(name)
        public_id = f'{COMPETITION_FOLDER}/{canonical}'
    else:
        canonical = _slugify(name)
        canonical = NICKNAMES.get(canonical, canonical)
        if canonical in SITE_OVERRIDES:
            is_national = SITE_OVERRIDES[canonical][2]
        else:
            is_national = (kind == 'national' or canonical.endswith(NATIONAL_SUFFIX))
        public_id = logo_public_id(canonical.removesuffix(NATIONAL_SUFFIX), is_national)

    if not force:
        try:
            cloudinary.api.resource(public_id)
        except cloudinary.api.NotFound:
            pass
        else:
            raise RuntimeError(
                f"A crest already exists at {public_id} — '{name}' resolves to "
                f"{canonical!r}. Pass --force to replace it, or use a more "
                f"specific team name."
            )

    result = cloudinary.uploader.upload(
        path,
        public_id=public_id,
        overwrite=True,
        invalidate=True,
    )
    url = result['secure_url']
    print(f"[logos] Uploaded local file {path} → {public_id} "
          f"(any team name that slugifies to {canonical!r} will find it)")
    return url


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Fetch team logos to Cloudinary.')
    parser.add_argument('teams', nargs='+', help="team names, or 'country/slug'")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--national', action='store_const', const='national', dest='kind',
                       help='treat names as national teams')
    group.add_argument('--club', action='store_const', const='club', dest='kind',
                       help='treat names as clubs')
    group.add_argument('--competition', action='store_const', const='competition', dest='kind',
                       help='treat names as competitions (Premier League, UCL, …)')
    parser.add_argument('--size', type=int, default=3000, help='logo size (default: 3000)')
    parser.add_argument('--force', action='store_true', help='re-upload even if it exists')
    parser.add_argument('--local', metavar='FILE',
                        help='upload FILE from disk instead of fetching from the site '
                             '(for teams football-logos.cc does not have); '
                             'takes exactly one team name')
    args = parser.parse_args()

    if args.local:
        if len(args.teams) != 1:
            parser.error('--local takes exactly one team name')
        try:
            print(upload_local_crest(args.local, args.teams[0], kind=args.kind,
                                     force=args.force))
        except (FileNotFoundError, RuntimeError, cloudinary.exceptions.Error) as e:
            print(f'[logos] {args.teams[0]}: {e}')
            sys.exit(1)
        sys.exit(0)

    for team in args.teams:
        try:
            if args.kind == 'competition':
                print(fetch_competition_logo(team, size=args.size, force=args.force))
            else:
                print(fetch_logo(team, kind=args.kind, size=args.size, force=args.force))
        except (LookupError, RuntimeError, requests.RequestException) as e:
            print(f'[logos] {team}: {e}')
