# caption.py — Gemini caption generator
import json
import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",   # fallback if 2.5 is busy/overloaded
]


def _summarise_events(events: list) -> str:
    """Build a compact goals string from scraper events list."""
    if not isinstance(events, list):
        return "No goals"
    goal_types = ('goal', 'penalty_goal', 'own_goal')
    parts = []
    for ev in events:
        if ev.get('type') not in goal_types:
            continue
        player = ev.get('player', 'Unknown')
        minute = ev.get('minute', '?')
        team   = ev.get('team', '')
        suffix = ''
        if ev['type'] == 'penalty_goal':
            suffix = ' (pen)'
        elif ev['type'] == 'own_goal':
            suffix = ' (og)'
        assister = ev.get('assister')
        assist_str = f", assist: {assister}" if assister else ''
        parts.append(f"{player}{suffix} {minute}' [{team}]{assist_str}")
    return ', '.join(parts) if parts else 'No goals scored'


def _summarise_stats(stats: dict) -> str:
    """Pull a few key stats for the prompt."""
    stat_list = stats.get('list', {})
    if not isinstance(stat_list, dict):
        return ''
    keys_of_interest = ('Ball Possession', 'Total Shots', 'Shots on Target', 'Corners')
    lines = []
    for k in keys_of_interest:
        if k in stat_list:
            v = stat_list[k]
            lines.append(f"{k}: {v.get('home', '?')} – {v.get('away', '?')}")
    return ' | '.join(lines)


_MATCH_SAMPLE_FIELDS = (
    'competition_name', 'group_name', 'gameweek', 'round_name', 'status',
    'minute', 'minute_extra', 'minute_period',
    'date_utc', 'time_utc',
    'team_A_name', 'team_B_name',
    'fs_A', 'fs_B', 'hts_A', 'hts_B', 'ets_A', 'ets_B', 'ps_A', 'ps_B',
)

def _clean_match_sample(ms: dict) -> dict:
    """Keep only caption-relevant fields and convert None/bool to clean strings."""
    out = {}
    for k in _MATCH_SAMPLE_FIELDS:
        v = ms.get(k)
        if v is None or v == '' or v is False:
            continue      # skip empty / false flags entirely
        if v is True:
            v = 'yes'
        out[k] = v
    return out


def _summarise_h2h(analysis: dict) -> str:
    bh = analysis.get('battle_history', {})
    if not isinstance(bh, dict) or bh == 'No data available':
        return ''
    parts = []
    for k, v in bh.items():
        parts.append(f"{k}: {v}")
    return ' | '.join(parts)


def _summarise_recent_form(analysis: dict, home: str, away: str) -> str:
    rr = analysis.get('recent_record', {})
    if not isinstance(rr, dict) or rr == 'No data available':
        return ''
    lines = []
    for side, label in (('team_A', home), ('team_B', away)):
        side_data = rr.get(side)
        if not side_data or side_data == 'No data available':
            continue
        if isinstance(side_data, dict):
            lines.append(f"{label}: {side_data}")
        elif isinstance(side_data, list):
            lines.append(f"{label}: {', '.join(str(x) for x in side_data)}")
    return ' | '.join(lines)


def generate_caption(scraper_data: dict, event_type: str = 'FT',
                     records: list | None = None) -> str:
    """
    Generate an Instagram caption from scraper_data.
    event_type: 'HT' → half-time caption, 'FT' → full-time caption.
    Tries gemini-2.5-flash first, then gemini-2.0-flash, then plain fallback.
    """
    match_sample = scraper_data.get('matchSample', {})
    home_team    = match_sample.get('team_A_name', 'Home')
    away_team    = match_sample.get('team_B_name', 'Away')
    competition  = match_sample.get('competition_name', 'FIFA World Cup 2026')

    if event_type == 'HT':
        home_score = match_sample.get('hts_A', '0')
        away_score = match_sample.get('hts_B', '0')
        moment     = 'half-time'
        moment_tag = 'HT'
    else:
        home_score = match_sample.get('fs_A', '?')
        away_score = match_sample.get('fs_B', '?')
        moment     = 'full-time'
        moment_tag = 'FT'

    goals_str = _summarise_events(scraper_data.get('events', []))
    stats_str = _summarise_stats(scraper_data.get('statistics', {}))
    analysis  = scraper_data.get('matchAnalysis', {})
    h2h_str   = _summarise_h2h(analysis)
    form_str  = _summarise_recent_form(analysis, home_team, away_team)

    # Group table — included for group stage matches
    cup_table = analysis.get('cup_table', 'No data available')
    is_group_stage = (
        isinstance(cup_table, dict)
        and cup_table not in ('No data available', {})
        and 'list' in cup_table
    )
    group_table_str = json.dumps(cup_table, ensure_ascii=False) if is_group_stage else ''

    # Determine result context for richer prompt
    try:
        hs = int(home_score)
        as_ = int(away_score)
        if hs > as_:
            result_context = f"{home_team} win"
        elif as_ > hs:
            result_context = f"{away_team} win"
        else:
            result_context = "a draw"
    except (ValueError, TypeError):
        result_context = "result unknown"

    records_block = ''
    if records:
        records_block = '- Records/Milestones at stake:\n' + '\n'.join(f'  • {r}' for r in records)

    prompt = f"""You are a football content writer for an Instagram page covering the FIFA World Cup 2026.

Write an Instagram caption for a match scorecard post. Follow the exact format and style of the example below — including flag emojis, number emojis in the score line, blank lines between paragraphs, and a blank line before hashtags.

EXAMPLE (for a Bosnia 3-1 Qatar FT post):
🇧🇦 FULL-TIME: Bosnia & Herzegovina 3️⃣-1️⃣ Qatar 🇶🇦

A convincing win, but not enough.

Bosnia finish their group-stage campaign with four points, yet results elsewhere mean they fall short of a guaranteed Round of 32 spot.

A strong finish, but the future lies in other's hands. 🇧🇦

#BosniaAndHerzegovina #Qatar #FIFAWorldCup #TheDragons #MaroonStars

---

RULES FOR THE CAPTION BODY:
- Line 1: [flag emoji] {"HALF-TIME" if event_type == "HT" else "FULL-TIME"}: [Home Team] [score digits as number emojis]-[score digits as number emojis] [Away Team] [flag emoji]
- Then a blank line
- Then 2–3 short punchy paragraphs, each separated by a blank line
- Each paragraph is 1–2 sentences max
- Mention key goal scorers naturally if available
- If a record or milestone was broken, weave it into a paragraph naturally
- If group table data is provided, reference the group standings to add narrative context (e.g. qualification implications, who goes through)
- Use 1–2 emojis within the body (not forced, feel natural)
- Tone: passionate football fan — real, emotional, not corporate, not clickbait

RULES FOR HASHTAGS (exactly 5, on one line after a blank line):
1. #HomeTeamName (no spaces, e.g. #BosniaAndHerzegovina)
2. #AwayTeamName (no spaces)
3. #FIFAWorldCup
4. #HomeTeamNickname (well-known nickname, e.g. #ThreeLions for England, #LesBleus for France — if no well-known nickname, use a relevant tag for that team, NOT a match abbreviation like #NORFRA)
5. #AwayTeamNickname (same rule)

Match Details:
- Full match info: {json.dumps(_clean_match_sample(match_sample), ensure_ascii=False)}
- Moment: {moment_tag} ({moment})
- Score at {moment}: {home_team} {home_score} – {away_score} {away_team}
- Result: {result_context}
- Goals: {goals_str}
{('- Stats: ' + stats_str) if stats_str else ''}{('\n- H2H: ' + h2h_str) if h2h_str else ''}{('\n- Recent form: ' + form_str) if form_str else ''}{('\n- Group table: ' + group_table_str) if group_table_str else ''}{('\n' + records_block) if records_block else ''}

Return ONLY the caption — no labels, no explanations, no markdown formatting."""

    for model in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt
            )
            print(f"[caption] Generated via {model}")
            return response.text.strip()
        except Exception as e:
            print(f"[caption] {model} failed: {e} — trying next model...")

    print("[caption] All Gemini models failed — using fallback caption")
    return _fallback_caption(home_team, away_team, home_score, away_score,
                             competition, event_type)


def _fallback_caption(home, away, hs, as_, comp, event_type):
    moment = "HALF-TIME" if event_type == "HT" else "FULL-TIME"

    try:
        h, a = int(hs), int(as_)
        if h > a:
            result = f"{home} take the win!"
        elif a > h:
            result = f"{away} take the win!"
        else:
            result = "The teams share the spoils!"
    except (ValueError, TypeError):
        result = "What a match!"

    home_tag = home.replace(' ', '')
    away_tag = away.replace(' ', '')

    return (
        f"{moment}: {home} {hs}-{as_} {away}\n"
        f"\n"
        f"{result}\n"
        f"\n"
        f"#{home_tag} #{away_tag} #FIFAWorldCup #WorldCup2026 #Football"
    )