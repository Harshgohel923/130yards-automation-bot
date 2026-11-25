# All assets ids are from cloudinary here:

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()
CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')

# Anything a person reads is shown in German local time — the fixture list, the
# match log, the dispatcher's housekeeping window. Everything *stored* stays
# UTC, which is what the scraper speaks and the only form that can't be
# misread. Using the IANA zone rather than a fixed offset is what makes the
# summer/winter switch automatic: CEST (UTC+2) and CET (UTC+1) are chosen per
# date, including on the changeover weekends.
LOCAL_TZ = ZoneInfo('Europe/Berlin')

# The same instant read a second way, for whoever works to Indian time. Shown
# beside the German one in the fixture list and written into matches.json as
# kickoff_ist; nothing is ever stored or scheduled in it.
IST_TZ = ZoneInfo('Asia/Kolkata')

# Normalize team names from scraper to config
# Normalize team names from scraper to official FIFA names
TEAM_NAME_ALIASES = {
    # Côte d'Ivoire
    "Côte d'Ivoire": "Côte d'Ivoire",
    "Cote d'Ivoire": "Côte d'Ivoire",
    "Cote D'Ivoire": "Côte d'Ivoire",
    "Ivory Coast": "Côte d'Ivoire",
    "Cote-d-ivoire": "Côte d'Ivoire",

    # DR Congo
    "DR Congo": "DR Congo",
    "DR-Congo": "DR Congo",
    "Congo DR": "DR Congo",
    "Congo-Kinshasa": "DR Congo",
    "Democratic Republic of the Congo": "DR Congo",

    # Czechia
    "Czech": "Czechia",
    "Czech Republic": "Czechia",
    "Czech-Republic": "Czechia",
    "Czechia": "Czechia",

    # South Korea
    "South Korea": "South Korea",
    "South-Korea": "South Korea",
    "Korea Republic": "South Korea",

    # South Africa
    "South Africa": "South Africa",
    "South-Africa": "South Africa",

    # Saudi Arabia
    "Saudi Arabia": "Saudi Arabia",
    "Saudi-Arabia": "Saudi Arabia",

    # New Zealand
    "New Zealand": "New Zealand",
    "New-Zealand": "New Zealand",

    # Bosnia and Herzegovina
    "Bosnia and Herzegovina": "Bosnia and Herzegovina",
    "Bosnia-And-Herzegovina": "Bosnia and Herzegovina",
    "Bosnia-Herzegovina": "Bosnia and Herzegovina",

    # United States
    "USA": "United States",
    "United States": "United States",
    "United States of America": "United States",

    # Türkiye
    "Turkey": "Türkiye",
    "Türkiye": "Türkiye",

    # Cabo Verde
    "Cape Verde": "Cabo Verde",
    "Cabo Verde": "Cabo Verde",
    "Cabo-Verde": "Cabo Verde",

    # Colombia
    "Colombia": "Colombia",
    "Columbia": "Colombia",

    # Curacao
    "Curacao": "Curaçao",
    "Curaçao": "Curaçao",
}

STADIUM_NAME_ALIASES = {

    # ── CANADA ───────────────────────────────────────────────────────────────

    # Toronto — BMO Field → Toronto Stadium
    "Toronto Stadium":          "Toronto Stadium",
    "BMO Field":                "Toronto Stadium",
    "BMO":                      "Toronto Stadium",

    # Vancouver — BC Place → BC Place Vancouver (only stadium keeping partial name)
    "BC Place Vancouver":       "BC Place Vancouver",
    "BC Place":                 "BC Place Vancouver",
    "Vancouver Stadium":        "BC Place Vancouver",  # common wrong assumption

    # ── MEXICO ───────────────────────────────────────────────────────────────

    # Mexico City — Estadio Banorte → Mexico City Stadium
    "Mexico City Stadium":      "Mexico City Stadium",
    "Estadio Banorte":          "Mexico City Stadium",
    "Estadio Azteca":           "Mexico City Stadium",
    "Azteca":                   "Mexico City Stadium",

    # Guadalajara — Estadio Akron → Guadalajara Stadium
    "Guadalajara Stadium":      "Guadalajara Stadium",
    "Estadio Akron":            "Guadalajara Stadium",
    "Akron Stadium":            "Guadalajara Stadium",

    # Monterrey — Estadio BBVA → Monterrey Stadium
    "Monterrey Stadium":        "Monterrey Stadium",
    "Estadio BBVA":             "Monterrey Stadium",
    "BBVA Stadium":             "Monterrey Stadium",
    "Estadio Bancomer":         "Monterrey Stadium",  # former name

    # ── UNITED STATES ────────────────────────────────────────────────────────

    # Atlanta — Mercedes-Benz Stadium → Atlanta Stadium
    "Atlanta Stadium":          "Atlanta Stadium",
    "Mercedes-Benz Stadium":    "Atlanta Stadium",
    "Mercedes Benz Stadium":    "Atlanta Stadium",

    # Boston — Gillette Stadium → Boston Stadium
    "Boston Stadium":           "Boston Stadium",
    "Gillette Stadium":         "Boston Stadium",
    "Gillette":                 "Boston Stadium",
    "Foxboro Stadium":          "Boston Stadium",     # former name, same site

    # Dallas — AT&T Stadium → Dallas Stadium
    "Dallas Stadium":           "Dallas Stadium",
    "AT&T Stadium":             "Dallas Stadium",
    "AT&T":                     "Dallas Stadium",
    "Cowboys Stadium":          "Dallas Stadium",     # former name

    # Houston — NRG Stadium → Houston Stadium
    "Houston Stadium":          "Houston Stadium",
    "NRG Stadium":              "Houston Stadium",
    "NRG":                      "Houston Stadium",
    "Reliant Stadium":          "Houston Stadium",    # former name

    # Kansas City — GEHA Field at Arrowhead Stadium → Kansas City Stadium
    "Kansas City Stadium":      "Kansas City Stadium",
    "GEHA Field at Arrowhead Stadium": "Kansas City Stadium",
    "Arrowhead Stadium":        "Kansas City Stadium",
    "Arrowhead":                "Kansas City Stadium",

    # Los Angeles — SoFi Stadium → Los Angeles Stadium
    "Los Angeles Stadium":      "Los Angeles Stadium",
    "SoFi Stadium":             "Los Angeles Stadium",
    "SoFi":                     "Los Angeles Stadium",

    # Miami — Hard Rock Stadium → Miami Stadium
    "Miami Stadium":            "Miami Stadium",
    "Hard Rock Stadium":        "Miami Stadium",
    "Hard Rock":                "Miami Stadium",
    "Sun Life Stadium":         "Miami Stadium",      # former name
    "Joe Robbie Stadium":       "Miami Stadium",      # former name

    # New York/New Jersey — MetLife Stadium → New York New Jersey Stadium
    "New York New Jersey Stadium": "New York New Jersey Stadium",
    "New York New Jersey":      "New York New Jersey Stadium",
    "NY NJ Stadium":            "New York New Jersey Stadium",
    "MetLife Stadium":          "New York New Jersey Stadium",
    "MetLife":                  "New York New Jersey Stadium",
    "Giants Stadium":           "New York New Jersey Stadium",  # former stadium, same site

    # Philadelphia — Lincoln Financial Field → Philadelphia Stadium
    "Philadelphia Stadium":     "Philadelphia Stadium",
    "Lincoln Financial Field":  "Philadelphia Stadium",
    "Lincoln Financial":        "Philadelphia Stadium",
    "The Linc":                 "Philadelphia Stadium",

    # San Francisco Bay Area — Levi's Stadium → San Francisco Bay Area Stadium
    "San Francisco Bay Area Stadium": "San Francisco Bay Area Stadium",
    "San Francisco Stadium":    "San Francisco Bay Area Stadium",
    "SF Bay Area Stadium":      "San Francisco Bay Area Stadium",
    "Levi's Stadium":           "San Francisco Bay Area Stadium",
    "Levis Stadium":            "San Francisco Bay Area Stadium",

    # Seattle — Lumen Field → Seattle Stadium
    "Seattle Stadium":          "Seattle Stadium",
    "Lumen Field":              "Seattle Stadium",
    "Lumen":                    "Seattle Stadium",
    "CenturyLink Field":        "Seattle Stadium",    # former name
    "Qwest Field":              "Seattle Stadium",    # former name
}


# Cloudinary resources (public IDs)
CLOUDINARY_TEMPLATES = {
    "UCL": "scorecard-template-UCL_lc32n3",
    "random-1": "scorecard-template-1_s4duju",
    "random-2": "scorecard-template-2_kpwltr",
    "random-3": "scorecard-template-3_mpir3j",
    "random-4": "scorecard-template-4_go9nva",
    "random-5": "scorecard-template-5_evmehr",
    "random-6": "scorecard-template-6_ixggep",
}

# Competitions that get their own dedicated template instead of a random one.
# Keys are canonical competition keys from logo_fetch.COMPETITIONS.
COMPETITION_TEMPLATES = {
    'uefa-champions-league': 'UCL',
}


def select_template_key(competition=None, match_id=None):
    """
    Pick which template a card uses.

    Champions League matches get the dedicated 'UCL' template; everything else
    gets one of the 'random-N' designs.

    The choice is *seeded by match_id*, so it is stable: the HT card, the FT
    card, and any corrected repost of the same match all land on the same
    design. Without the seed, a corrected repost would silently switch
    templates mid-match.
    """
    import random

    from logo_fetch import resolve_competition

    key = resolve_competition(competition)
    if key in COMPETITION_TEMPLATES:
        dedicated = COMPETITION_TEMPLATES[key]
        if dedicated in CLOUDINARY_TEMPLATES:
            return dedicated

    pool = sorted(k for k in CLOUDINARY_TEMPLATES if k.startswith('random-'))
    if not pool:
        # No random designs configured — fall back to any template at all.
        return next(iter(CLOUDINARY_TEMPLATES), None)

    return random.Random(str(match_id)).choice(pool) if match_id else random.choice(pool)


# Local event symbols (relative to scorecard.py)
LOCAL_SYMBOLS = {
    "normal_goal":    "assets/symbols/normal_goal.png",
    "red_card":       "assets/symbols/red_card.jpg",
    "yellow_card":    "assets/symbols/yellow_card.jpg",
    "penalty_goal":   "assets/symbols/penalty_goal.png",
    "own_goal":       "assets/symbols/own_goal.png",
    "penalty_missed": "assets/symbols/penalty_missed.png",
}

BRAND_LOGO = {
    "130 Yards": "130yardslogo-transparent_p3rjtr"
}


# Deterministic crest URLs are resolved once per team per run.
_CREST_URL_CACHE = {}
_COMP_URL_CACHE = {}


def get_brand_logo_url():
    return f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{BRAND_LOGO['130 Yards']}"


def get_competition_logo_url(competition, alert=True):
    """
    Cloudinary logo URL for a competition name from any source (matches.json,
    scraper, TheSportsDB). Friendlies use the dedicated friendly logo
    (assets/competition/friendly); unknown competitions and missing logos
    fall back to the 130 Yards brand logo — never an empty slot.
    """
    import requests
    from logo_fetch import (COMPETITIONS, COMPETITION_FOLDER, _slugify,
                            resolve_competition)
    from telegram_notify import send_alert

    name = (competition or '').strip()
    if not name:
        return get_brand_logo_url()

    # A competition the bot has no entry for is still worth looking up: an
    # uploaded badge lands at the slugified name, exactly as `--local
    # --competition` writes it. Checking here is what makes that command work
    # without a code change — the same deal unknown team crests already get.
    key = resolve_competition(name)
    known = key is not None
    if key is None:
        key = _slugify(name)
        if not key:
            return get_brand_logo_url()

    if key in _COMP_URL_CACHE:
        return _COMP_URL_CACHE[key]

    url = f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{COMPETITION_FOLDER}/{key}.png"
    for attempt in (1, 2):
        try:
            if requests.head(url, timeout=10).status_code == 200:
                _COMP_URL_CACHE[key] = url
                return url
            break   # definitive miss
        except requests.RequestException:
            if attempt == 2:
                return url   # Cloudinary unreachable — optimistic, uncached

    if alert:
        # Only a competition the bot knows *and* that the source site carries
        # can be fetched automatically. Everything else — friendlies, and any
        # competition with no entry at all — needs a badge supplied by hand.
        fetchable = known and COMPETITIONS[key]
        fix = (f'python logo_fetch.py --competition "{name}"' if fetchable
               else f'python logo_fetch.py --local <file.png> --competition "{name}"')
        send_alert(
            f"⚠️ We don't have a badge saved for '{name}', so its cards will "
            f"show the 130 Yards logo instead.\n\n"
            f"Posts still go out normally — this is cosmetic.\n\n"
            f"To add one: run '{fix}'"
            + ("" if fetchable else
               " — point it at a PNG of the badge you want") +
            f". It takes effect straight away, nothing needs pushing.",
            key=f'comp:{key}', cooldown=1800,
        )
    return get_brand_logo_url()


def get_crest_url(team_name, alert=True):
    """
    Cloudinary crest URL for any spelling of a team name.

    Every crest — club and national alike — lives at the deterministic path
    logo_fetch uploads to from football-logos.cc (assets/club/<name>,
    assets/national/<name>), which validate_matches.py guarantees exists
    before kickoff. There are no hand-mapped crest links.

    Only hits are cached — a miss is re-checked on every call, so a crest
    uploaded mid-match is picked up by the next render. alert=False lets a
    caller do its own reporting (the match worker's pre-kickoff check).
    """
    import requests
    from logo_fetch import crest_candidates
    from telegram_notify import send_alert

    normalized = TEAM_NAME_ALIASES.get(team_name, team_name)
    if team_name in _CREST_URL_CACHE:
        return _CREST_URL_CACHE[team_name]

    candidates = crest_candidates(team_name)
    network_error = False
    for public_id in candidates:
        url = f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{public_id}.png"
        for attempt in (1, 2):
            try:
                if requests.head(url, timeout=10).status_code == 200:
                    _CREST_URL_CACHE[team_name] = url
                    return url
                break   # definitive miss for this candidate — try the next
            except requests.RequestException:
                if attempt == 2:
                    network_error = True

    if network_error:
        # Cloudinary was unreachable, so "missing" can't be distinguished from
        # a blip. Assume the crest exists at the likeliest path instead of
        # false-alarming; if Cloudinary really is down, the image download
        # fails anyway. Not cached, so the next call re-checks properly.
        url = f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{candidates[-1]}.png"
        print(f"[warning] Cloudinary unreachable while checking crest for "
              f"'{team_name}' — assuming {candidates[-1]}")
        return url

    print(f"[warning] No crest found for '{team_name}' (normalized: '{normalized}')")
    if alert:
        send_alert(
            f"⚠️ We don't have a badge for '{team_name}', so its scorecard will "
            f"have a blank space where the logo goes.\n\n"
            f"Everything else posts normally.\n\n"
            f"To fix: check how the name is spelled in the fixture list, then "
            f"run 'python validate_matches.py'. If the spelling is right, try "
            f"'python logo_fetch.py \"{team_name}\"'.",
            key=f'crest:{team_name}', cooldown=1800,
        )
    return None