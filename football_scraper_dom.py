# football_scraper_dom.py
import json
import os
import re

import requests


# ─────────────────────────────────────────────────────────────────────────────
# Event-type classifier
# Each URL contains a unique base-64 substring in its filename that reliably
# identifies the event type. Checked against lowercase URL.
#
#   goal             →  "cp-_"
#   penalty_goal     →  "tqm6" or "kbao"
#   penalty_missed   →  "emgp"   e.g. ChNLklyPLpuAEmgpAAAJOegKiJs321.png
#   own_goal         →  "oamr"
#   assist           →  "qwrv"
#   yellow_card      →  "l8te"
#   red_card         →  "widj"
#   substitution_in  →  "ualv"
#   substitution_out →  "ekur"
#   half_time        →  filename == "ht.png"
#   var              →  "var"
# ─────────────────────────────────────────────────────────────────────────────
def parse_event_type(img_url: str):
    if not img_url:
        return "unknown", "❓"

    filename_lower = img_url.split("/")[-1].lower()

    if filename_lower == "ht.png":
        return "half_time", "⏰"
    if "var" in filename_lower:
        return "var", "🖥️"

    lurl = img_url.lower()

    # Substitutions — check before goal-family
    if "ualv" in lurl:
        return "substitution_in",  "⬆️"
    if "ekur" in lurl:
        return "substitution_out", "⬇️"

    # Cards
    if "l8te" in lurl:
        return "yellow_card", "🟡"
    if "widj" in lurl:
        return "red_card",    "🔴"

    # Goal family — most specific first
    if "qwrv" in lurl:
        return "assist",          "🅰️"
    if "oamr" in lurl:
        return "own_goal",        "⚽ OG"
    if "tqm6" in lurl or "kbao" in lurl:
        return "penalty_goal",    "⚽ P"
    if "emgp" in lurl:
        return "penalty_missed",  "❌"
    if "cp-_" in lurl:
        return "goal",            "⚽"

    return "other", "🏳️"


# ─────────────────────────────────────────────────────────────────────────────
def get_match_data(url: str) -> dict | None:
    """
    Scrapes AllFootball match page and returns a structured dict.
    Returns None if the page cannot be fetched or parsed.
    Also saves the result to data/<match_id>-<home>-vs-<away>.json.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    # ── 1. Fetch ──────────────────────────────────────────────────────────────
    try:
        response = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"[scraper] Network error for {url}: {e}")
        return None

    if response.status_code != 200:
        print(f"[scraper] HTTP {response.status_code} for {url}")
        return None

    response.encoding = "utf-8"

    # ── 2. Extract embedded state ─────────────────────────────────────────────
    m = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(.*?)\s*</script>",
        response.text,
        re.DOTALL,
    )
    if not m:
        print(f"[scraper] Could not find __INITIAL_STATE__ in page: {url}")
        return None

    try:
        state = json.loads(m.group(1).strip().rstrip(";"))
    except json.JSONDecodeError as e:
        print(f"[scraper] JSON parse error: {e}")
        return None

    mds          = state["matchDetailStore"]
    match_sample = mds["matchDetailFormation"]["matchSample"]
    home_team    = match_sample["team_A_name"]
    away_team    = match_sample["team_B_name"]
    status       = mds["matchOverviewFormation"]["status"]

    # ── Safe accessor ─────────────────────────────────────────────────────────
    def _safe(d, *keys, fallback="No data available"):
        try:
            val = d
            for k in keys:
                val = val[k]
            return val if val not in (None, "", [], {}) else fallback
        except (KeyError, TypeError, IndexError):
            return fallback

    def _drop_name(d):
        if not isinstance(d, dict):
            return "No data available"
        return {k: v for k, v in d.items() if k != "name"}

    if status == "Fixture":
        formatted_events = "Match has not started yet"
        formatted_stats  = "Match has not started yet"
    else:
        # ── 3. Parse events ───────────────────────────────────────────────────
        formatted_events = []

        for item in mds["matchOverviewFormation"].get("events", []):
            minute = f"{item['minute']}'"

            for team_name, raw_team_events in [
                (home_team, item.get("teamAEvents", [])),
                (away_team, item.get("teamBEvents", [])),
            ]:
                ins, outs, goals, assists, others = [], [], [], [], []

                for ev in raw_team_events:
                    player   = ev.get("person", "").strip()
                    pid      = ev.get("person_id", "")
                    ev_type, ev_icon = parse_event_type(ev.get("event_pic", ""))

                    if ev_type == "substitution_in":
                        ins.append({"player": player, "player_id": pid})

                    elif ev_type == "substitution_out":
                        outs.append({"player": player, "player_id": pid})

                    elif ev_type in ("goal", "penalty_goal", "own_goal"):
                        goals.append({
                            "player": player, "player_id": pid,
                            "type": ev_type, "icon": ev_icon,
                        })

                    elif ev_type == "assist":
                        assists.append({"player": player, "player_id": pid})

                    elif ev_type == "penalty_missed":
                        others.append({
                            "minute":    minute,
                            "type":      "penalty_missed",
                            "icon":      "❌",
                            "player":    player or "N/A",
                            "player_id": pid,
                            "team":      team_name,
                        })

                    elif player or ev_type not in ("unknown", "other"):
                        others.append({
                            "minute":    minute,
                            "type":      ev_type,
                            "icon":      ev_icon,
                            "player":    player or "N/A",
                            "player_id": pid,
                            "team":      None if ev_type == "half_time" else team_name,
                        })

                # ── Goals merged with their assist ────────────────────────────
                for i, g in enumerate(goals):
                    assr = assists[i] if i < len(assists) else None
                    formatted_events.append({
                        "minute":      minute,
                        "type":        g["type"],
                        "icon":        g["icon"],
                        "player":      g["player"],
                        "player_id":   g["player_id"],
                        "team":        team_name,
                        "assister":    assr["player"]    if assr else None,
                        "assister_id": assr["player_id"] if assr else None,
                    })

                # Orphaned assists
                for i in range(len(goals), len(assists)):
                    a = assists[i]
                    formatted_events.append({
                        "minute":    minute,
                        "type":      "assist",
                        "icon":      "🅰️",
                        "player":    a["player"],
                        "player_id": a["player_id"],
                        "team":      team_name,
                    })

                # ── Cards / VAR / HT / penalty_missed ────────────────────────
                formatted_events.extend(others)

                # ── Substitutions ─────────────────────────────────────────────
                if not ins and not outs:
                    pass
                elif len(ins) == len(outs):
                    for i in range(len(ins)):
                        formatted_events.append({
                            "minute":        minute,
                            "type":          "substitution",
                            "icon":          "🔄",
                            "player_in":     ins[i]["player"],
                            "player_in_id":  ins[i]["player_id"],
                            "player_out":    outs[i]["player"],
                            "player_out_id": outs[i]["player_id"],
                            "team":          team_name,
                        })
                else:
                    for p in ins:
                        formatted_events.append({
                            "minute":    minute,
                            "type":      "substitution_in",
                            "icon":      "⬆️",
                            "player":    p["player"],
                            "player_id": p["player_id"],
                            "team":      team_name,
                        })
                    for p in outs:
                        formatted_events.append({
                            "minute":    minute,
                            "type":      "substitution_out",
                            "icon":      "⬇️",
                            "player":    p["player"],
                            "player_id": p["player_id"],
                            "team":      team_name,
                        })

        # ── 4. Parse statistics ───────────────────────────────────────────────
        raw_stats       = mds["matchOverviewFormation"]["statistics"].get("list", [])
        formatted_stats = {}

        for stat in raw_stats:
            stat_name = stat.get("type", "")
            val_home  = stat.get("team_A", {}).get("value", "0")
            val_away  = stat.get("team_B", {}).get("value", "0")

            pct_keywords = ("possession", "accuracy", "rate")
            if any(kw in stat_name.lower() for kw in pct_keywords):
                home_v = f"{val_home}%" if "%" not in str(val_home) else str(val_home)
                away_v = f"{val_away}%" if "%" not in str(val_away) else str(val_away)
            else:
                try:
                    home_v = int(val_home)
                    away_v = int(val_away)
                except ValueError:
                    home_v = val_home
                    away_v = val_away

            formatted_stats[stat_name] = {"home": home_v, "away": away_v}

    # ── 5. Build output object ────────────────────────────────────────────────
    maf          = mds.get("matchAnalysisFormation", {})
    ov_stats_raw = mds["matchOverviewFormation"]["statistics"]
 
    if not isinstance(ov_stats_raw, dict):
        ov_stats_raw = {}
 
    # For fixtures, statistics and events are plain strings, not dicts
    if status == "Fixture":
        output_statistics = "Match has not started yet"
    else:
        output_statistics = {
            "team_A": _safe(ov_stats_raw, "team_A"),
            "team_B": _safe(ov_stats_raw, "team_B"),
            "list":   formatted_stats,
        }
 
    output_data = {
        "matchSample": match_sample,
        "matchSK_url": _safe(mds, "matchDetailFormation", "matchSK", "url"),
        "matchFormation": _safe(mds, "matchDetailFormation", "matchFormation"),
        "status": status,
        "events": formatted_events,
        "statistics": output_statistics,
        "matchAnalysis": {
            "team_A":         _safe(maf, "team_A"),
            "team_B":         _safe(maf, "team_B"),
            "battle_history": _drop_name(maf.get("battle_history", {})),
            "cup_table":      _safe(maf, "cup_table"),
            "recent_record":  _drop_name(maf.get("recent_record", {})),
        },
    }

    # ── 6. Save to data/ ──────────────────────────────────────────────────────
    os.makedirs("data", exist_ok=True)
    match_id  = url.rstrip("/").split("/")[-1]
    home_slug = home_team.replace(" ", "-")
    away_slug = away_team.replace(" ", "-")
    filename  = f"{match_id}-{home_slug}-vs-{away_slug}.json"
    filepath  = os.path.join("data", filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=4, ensure_ascii=False)

    # ── 7. Console summary ────────────────────────────────────────────────────
    sep = "═" * 56
    print(f"\n{sep}")
    print(
        f"  {home_team:20s}  {match_sample.get('fs_A', '?')} – "
        f"{match_sample.get('fs_B', '?')}  {away_team}"
    )
    print(f"  Status: {status}")
    print(f"{sep}")
    print(f"\n✅  Saved → {filepath}")

    event_count = len(formatted_events) if isinstance(formatted_events, list) else "–"
    print(f"\n── Timeline ({event_count} events) ──────────────────")
    print(json.dumps(formatted_events, indent=4, ensure_ascii=False))

    return output_data



# =========================================================================================

# Normal goal: 
# http://img-sd.allfootballapp.com/fastdfs4/M00/C8/DF/ChMf8FyPLpyACp-_AAAIJm3aFSg881.png

# penalty goal:
# http://img-sd.allfootballapp.com/fastdfs4/M00/C8/DD/ChNLklyPLpuATqM6AAAJjsueBPk904.png
# http://img-sd.allfootballapp.com/fastdfs4/M00/C8/7A/ChNLklx1KiiAKBAOAAAHCpKLVGk952.png

# own goal:
# http://img-sd.allfootballapp.com/fastdfs4/M00/C8/DF/ChMf8FyPLpuAOAmRAAAI9PoZUXw387.png

# penalty missed:
# http://img-sd.allfootballapp.com/fastdfs4/M00/C8/DD/ChNLklyPLpuAEmgpAAAJOegKiJs321.png

# assist url: 
# http://img-sd.allfootballapp.com/fastdfs4/M00/C8/DF/ChMf8FyPUUyAQWrVAAAHWNnGGx8134.png

# yellow card:
# http://img-sd.allfootballapp.com/fastdfs3/M00/B9/3E/ChOxM1xGg02AL8TeAAAE1p6R8m4139.png

# red card:
# http://img-sd.allfootballapp.com/fastdfs3/M00/B9/3E/ChOxM1xGg02AWiDjAAAE3Y8ENZg267.png

# Half time:
# http://img-static.allfootballapp.com/soccer/data/logo/event/HT.png

# Substitute in:
# http://img-sd.allfootballapp.com/fastdfs4/M00/DD/74/ChNLkl11sD2AUALVAAAFm4yChAM178.png

# Substitute out:
# http://img-sd.allfootballapp.com/fastdfs4/M00/DD/73/ChMf8F11sD2AeKurAAAFymECorw405.png





# "matchDetailStore":{
#       "matchDetailFormation":{
#           "matchSample":{
#               whole object
#            },
#           "matchSK":{
#                 "url":"https://m-api.allfootballapp.com/m/data/event/match/54329975?plat=m&language=en"  //the page to get all the statistics and event data (just for reference later, no need to do anything with this right now)
#            },
#           "matchFormation":{
#               whole object //It has both team lineups
#            },
#        },
#       "matchOverviewFormation":{
#            "status":"Played", // very important to know if the game has ended or not, PLayed means its finished, Fixture means its scheduled but not started. Playing means in progress.
#            "events":{
#               whole object // it has details of the events that too place in that match.
#            },
#            "statistics":{
#               whole object // has info about who is team A and team B and has the statistics of entire match
#            },
#        },
#        "matchAnalysisFormation":{
#             "team_A": ,
#             "team_B": ,
#             "battle_history":{
                # keep everything except name key
                # it has head-2-head record of these teams
#             },
#             "cup_table":{
#                 "name":"World Cup Group Stage Group A",
#                 "list":{
                    #   It has the updated points table (what table looks like after this match) of the group in which the teams are. 
#                  },
#             },
#             "recent_record":{ 
#                  record of last 5 games, include everything except name key.
#             },
#        },
# },

# -------------------------------------------------------------
# "matchSample has the whole overview of the fixture, team A is Home team, team B is Away team, fs_A is full time score of Team A, hts_A is half time score of team A, ets_A is extra time score of team A, ps_A is penalty score of team A and same for team B"
#  "status":"Played" is important to denote the game has ended
#          "matchSample":{
#               "competition_name":"World Cup"
#               "group_name":"Group A",
#               "gameweek":"2",
#               "round_name":"R",
#               "status":"Played",
#               "minute":"90",
#               "suretime":true,
#               "date_utc":"2026-06-18",
#               "time_utc":"16:00:00",
#               "start_play":"2026-06-18 16:00:00",
#               "minute_extra":"None",
#               "minute_period":"None",
#               "team_A_id":"50000453",
#               "team_A_name":"Czech",
#               "team_A_logo":"https://sd.qunliao.info/fastdfs3/M00/B5/74/ChOxM1xC2EuAYSDaAAACk9Rvueg747.png",
#               "team_B_id":"50001753",
#               "team_B_name":"South Africa",
#               "team_B_logo":"https://sd.qunliao.info/fastdfs3/M00/B5/7E/ChOxM1xC2RuADToCAAAEmtXhz7o022.png",
#               "fs_A":"1",
#               "fs_B":"1",
#               "hts_A":"1",
#               "hts_B":"0",
#               "ets_A":"",
#               "ets_B":"",
#               "ps_A":"",
#               "ps_B":"",
#           },