# All assets ids are from cloudinary here:

import os

from dotenv import load_dotenv

load_dotenv()
CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')

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
    "HT": "Half_time_template_ujqiub",
    "FT": "Full_time_template_hckszk",
}

CLOUDINARY_TEAM_CRESTS = {
    "Panama": "panama_panama-national-team_3000x3000.football-logos.cc_ourbmw",
    "Ghana": "ghana_ghana-national-team_3000x3000.football-logos.cc_zcm0kp",
    "Croatia": "croatia_croatia-national-team_3000x3000.football-logos.cc_yl5gep",
    "England": "england_england-national-team_3000x3000.football-logos.cc_hatcaq",
    "Colombia": "colombia_colombia-national-team_3000x3000.football-logos.cc_ck4fsc",
    "Uzbekistan": "uzbekistan_uzbekistan-national-team_3000x3000.football-logos.cc_pcv5du",
    "DR Congo": "congo-dr_congo-dr-national-team_3000x3000.football-logos.cc_b7hqui",
    "Jordan": "jordan_jordan-national-team_3000x3000.football-logos.cc_yvpnbs",
    "Portugal": "portugal_portuguese-football-federation_3000x3000.football-logos.cc_lblejx",
    "Austria": "austria_austria-national-team_3000x3000.football-logos.cc_b8ayln",
    "Algeria": "algeria_algeria-national-team_3000x3000.football-logos.cc_hjt92q",
    "Argentina": "argentina_argentina-national-team_3000x3000.football-logos.cc_ldq7sn",
    "Norway": "norway_norway-national-team_3000x3000.football-logos.cc_mm8h9x",
    "Senegal": "senegal_senegal-national-team_3000x3000.football-logos.cc_lqpltg",
    "Iraq": "iraq_iraq-national-team_3000x3000.football-logos.cc_ex7n4l",
    "France": "france_france-national-team_3000x3000.football-logos.cc_wm8som",
    "Uruguay": "uruguay_uruguay-national-team_3000x3000.football-logos.cc_re8tex",
    "Saudi Arabia": "saudi-arabia_saudi-arabia-national-team_3000x3000.football-logos.cc_hpq5ar",
    "Cabo Verde": "cabo-verde_cabo-verde-national-team_3000x3000.football-logos.cc_njawpl",
    "Spain": "spain_spain-national-team_3000x3000.football-logos.cc_hv4cpv",
    "Iran": "iran_iran-national-team_3000x3000.football-logos.cc_yz2viv",
    "New Zealand": "new-zealand_new-zealand-national-team_3000x3000.football-logos.cc_vsz9b1",
    "Egypt": "egypt_egypt-national-team_3000x3000.football-logos.cc_uyduwu",
    "Belgium": "belgium_belgium-national-team_3000x3000.football-logos.cc_cqqmjb",
    "Tunisia": "tunisia_tunisia-national-team_3000x3000.football-logos.cc_rjzpgq",
    "Sweden": "sweden_sweden-national-team_3000x3000.football-logos.cc_qqylk1",
    "Netherlands": "netherlands_dutch-national-team_3000x3000.football-logos.cc_ugdo1d",
    "Japan": "japan_japan-national-team_3000x3000.football-logos.cc_k5uqkg",
    "Ecuador": "ecuador_ecuador-national-team_3000x3000.football-logos.cc_ntejgz",
    "Côte d'Ivoire": "cote-d-ivoire_cote-d-ivoire-national-team_3000x3000.football-logos.cc_nwhpeu",
    "Curaçao": "curacao_curacao-national-team_3000x3000.football-logos.cc_sglxvo",
    "United States": "usa_usa-national-team_3000x3000.football-logos.cc_oswazx",
    "Germany": "germany_germany-national-team_3000x3000.football-logos.cc_xenjn3",
    "Türkiye": "turkey_turkey-national-team_3000x3000.football-logos.cc_si672q",
    "Australia": "australia_australia-national-team_3000x3000.football-logos.cc_flk7hw",
    "Paraguay": "paraguay_paraguay-national-team_3000x3000.football-logos.cc_bzkion",
    "Scotland": "scotland_scotland-national-team_3000x3000.football-logos.cc_vuyixh",
    "Haiti": "haiti_haiti-national-team_3000x3000.football-logos.cc_rfbfoq",
    "Morocco": "morocco_morocco-national-team_3000x3000.football-logos.cc_iorleu",
    "Brazil": "brazil_brazil-national-team_3000x3000.football-logos.cc_bj5oix",
    "Switzerland": "switzerland_switzerland-national-team_3000x3000.football-logos.cc_b27tsy",
    "Qatar": "qatar_qatar-national-team_3000x3000.football-logos.cc_ufsigk",
    "Bosnia and Herzegovina": "bosnia-and-herzegovina_bosnia-and-herzegovina-national-team_3000x3000.football-logos.cc_p9a5aa",
    "Canada": "canada_canada-national-team_3000x3000.football-logos.cc_mrkpus",
    "South Africa": "south-africa_south-africa-national-team_3000x3000.football-logos.cc_skcf4r",
    "South Korea": "south-korea_south-korea-national-team_3000x3000.football-logos.cc_pf5y5m",
    "Czechia": "czech-republic_czech-republic-national-team_3000x3000.football-logos.cc_sfp6lw",
    "Mexico": "mexico_mexico-national-team_3000x3000.football-logos.cc_hwm38l",
}

# Local event symbols (relative to scorecard.py)
LOCAL_SYMBOLS = {
    "normal_goal":    "assets/symbols/normal_goal.png",
    "red_card":       "assets/symbols/red_card.jpg",
    "yellow_card":    "assets/symbols/yellow_card.png",
    "penalty_goal":   "assets/symbols/penalty_goal.png",
    "own_goal":       "assets/symbols/own_goal.png",
    "penalty_missed": "assets/symbols/penalty_missed.png",
}

CLOUDINARY_TOURNAMENT_LOGO = {
    "World Cup": "tournaments_fifa-world-cup-2026--white_3000x3000.football-logos.cc_fgfkp8",
}

BRAND_LOGO = {
    "130 Yards": "130yardslogo-transparent_p3rjtr"
}


def get_crest_url(team_name):
    """
    Fetch crest from Cloudinary, handling team name variations.
    """
    # Normalize the incoming name
    normalized = TEAM_NAME_ALIASES.get(team_name, team_name)
    
    # Look up in config
    crest_id = CLOUDINARY_TEAM_CRESTS.get(normalized)
    
    if not crest_id:
        print(f"[warning] No crest found for '{team_name}' (normalized: '{normalized}')")
        return None
    
    return f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{crest_id}"