# caption.py — Gemini caption generator
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


def generate_caption(scraper_data: dict, event_type: str = 'FT') -> str:
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

    prompt = f"""You are a sharp football content writer for an Instagram page covering the FIFA World Cup 2026.

Write a punchy, engaging Instagram caption for a match scorecard post.

Match Details:
- {home_team} {home_score} – {away_score} {away_team}
- Competition: {competition}
- Moment: {moment_tag} ({moment})
- Result: {result_context}
- Goals: {goals_str}
{('- Stats: ' + stats_str) if stats_str else ''}

Instructions:
- Open with the result — make it feel like breaking news or a dramatic reveal
- Use 1–3 relevant emojis naturally within the caption (not just at the end)
- Mention goal scorers if available, keep it punchy
- Tone: passionate football fan, not corporate, not clickbait
- Caption must be under 180 characters
- Add exactly 5 targeted hashtags on a new line (mix of match-specific and broad World Cup tags)
- Return ONLY the caption text + hashtags. No commentary, no labels, no quotes."""

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
    moment = "⏱️ Half Time" if event_type == "HT" else "🏁 Full Time"

    try:
        h, a = int(hs), int(as_)
        if h > a:
            result = f"⚽ {home} take the win!"
        elif a > h:
            result = f"⚽ {away} take the win!"
        else:
            result = "🤝 The teams share the spoils!"
    except (ValueError, TypeError):
        result = "⚽ What a match!"

    home_tag  = home.replace(' ', '')
    away_tag  = away.replace(' ', '')
    comp_tag  = comp.replace(' ', '')

    return (
        f"{moment} | {home} {hs} – {as_} {away}\n"
        f"{result}\n"
        f"#{comp_tag} #WorldCup2026 #{home_tag} #{away_tag} #Football"
    )