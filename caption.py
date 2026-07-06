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
        home_score = str(match_sample.get('hts_A') or '0')
        away_score = str(match_sample.get('hts_B') or '0')
        moment     = 'half-time'
        moment_tag = 'HT'
        score_line = f"{home_team} {home_score}-{away_score} {away_team} (Half-Time)"
        match_ending = 'half-time'
    else:
        ps_a = str(match_sample.get('ps_A') or '').strip()
        ps_b = str(match_sample.get('ps_B') or '').strip()
        fs_a = str(match_sample.get('fs_A') or '0')
        fs_b = str(match_sample.get('fs_B') or '0')
        ets_a = str(match_sample.get('ets_A') or '').strip()
        ets_b = str(match_sample.get('ets_B') or '').strip()

        if ps_a and ps_b:
            # Match went to penalties — display score is AET score (excl. penalties)
            try:
                disp_a = str(int(fs_a) - int(ps_a))
                disp_b = str(int(fs_b) - int(ps_b))
            except (ValueError, TypeError):
                disp_a, disp_b = fs_a, fs_b
            home_score, away_score = disp_a, disp_b
            score_line = (
                f"{home_team} {disp_a}-{disp_b} {away_team} (AET) "
                f"| Penalties: {home_team} {ps_a}-{ps_b} {away_team}"
            )
            match_ending = 'penalties'
            try:
                result_context = f"{away_team} win on penalties" if int(ps_b) > int(ps_a) else f"{home_team} win on penalties"
            except (ValueError, TypeError):
                result_context = "won on penalties"
        elif ets_a and ets_b:
            # Match went to ET but no penalties
            home_score, away_score = fs_a, fs_b
            score_line = f"{home_team} {fs_a}-{fs_b} {away_team} (AET)"
            match_ending = 'extra time'
            try:
                result_context = f"{home_team} win (AET)" if int(fs_a) > int(fs_b) else f"{away_team} win (AET)"
            except (ValueError, TypeError):
                result_context = "result after extra time"
        else:
            home_score, away_score = fs_a, fs_b
            score_line = f"{home_team} {fs_a}-{fs_b} {away_team} (Full-Time)"
            match_ending = 'full-time'
            try:
                hs, as_ = int(fs_a), int(fs_b)
                if hs > as_:
                    result_context = f"{home_team} win"
                elif as_ > hs:
                    result_context = f"{away_team} win"
                else:
                    result_context = "a draw"
            except (ValueError, TypeError):
                result_context = "result unknown"

        moment     = 'full-time'
        moment_tag = 'FT'

    if event_type == 'HT':
        try:
            hs, as_ = int(home_score), int(away_score)
            if hs > as_:
                result_context = f"{home_team} leading"
            elif as_ > hs:
                result_context = f"{away_team} leading"
            else:
                result_context = "level at half-time"
        except (ValueError, TypeError):
            result_context = "result unknown"

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

    records_block = ''
    if records:
        records_block = '- Records/Milestones at stake:\n' + '\n'.join(f'  • {r}' for r in records)

    prompt = f"""You are a football content writer for an Instagram page covering the FIFA World Cup 2026.

Write a creative, engaging Instagram caption for a match scorecard post. Every caption should feel UNIQUE — vary the structure, length, tone, and opening style. Never repeat the same template twice.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UNICODE FONT STYLES — use ONLY these five, no others (do NOT use script/calligraphic fonts under any circumstance):
  1. Bold Italic Sans (e.g. 𝙏𝙝𝙚 𝙦𝙪𝙞𝙘𝙠 𝙗𝙧𝙤𝙬𝙣 𝙛𝙤𝙭)          → punchy secondary lines, achievement callouts
  2. Bold Serif       (e.g. 𝐓𝐡𝐞 𝐪𝐮𝐢𝐜𝐤 𝐛𝐫𝐨𝐰𝐧 𝐟𝐨𝐱)             → main headlines, team names in openers
Mix both styles per caption. Use plain text for regular sentences. PLAIN CAPITALS are also allowed for extra emphasis where styled fonts feel like too much.
IMPORTANT: within any single word, use ONE style consistently for every letter — never mix a styled font with plain characters or digits inside the same word (this produces broken, unreadable text like "𝕋𝟟ℍ").
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STYLE REFERENCES (vary between these approaches — don't copy, use as inspiration):

Example A — short & punchy (winner advances):
𝐌𝐎𝐑𝐎𝐂𝐂𝐎 𝐈𝐍𝐓𝐎 𝐓𝐇𝐄 𝐑𝐎𝐔𝐍𝐃 𝐎𝐅 𝟏𝟔 𝐀𝐅𝐓𝐄𝐑 𝐀 𝐃𝐑𝐀𝐌𝐀𝐓𝐈𝐂 𝐏𝐄𝐍𝐀𝐋𝐓𝐘 𝐒𝐇𝐎𝐎𝐓𝐎𝐔𝐓 🦁🇲🇦

Morocco are now 33 games unbeaten 😳

Example B — upset/shock (with multi-style fonts):
𝐏𝐀𝐑𝐀𝐆𝐔𝐀𝐘 𝐄𝐋𝐈𝐌𝐈𝐍𝐀𝐓𝐄 𝐆𝐄𝐑𝐌𝐀𝐍𝐘 𝐅𝐑𝐎𝐌 𝐓𝐇𝐄 𝐖𝐎𝐑𝐋𝐃 𝐂𝐔𝐏 🤯🤯🤯

𝗔𝗙𝗧𝗘𝗥 𝗣𝗘𝗡𝗔𝗟𝗧𝗜𝗘𝗦, 𝗧𝗛𝗘𝗬 𝗔𝗥𝗘 𝗧𝗛𝗥𝗢𝗨𝗚𝗛 𝗧𝗢 𝗧𝗛𝗘 𝗥𝗢𝗨𝗡𝗗 𝗢𝗙 𝟭𝟲 😲

𝙒𝙃𝘼𝙏 𝘼𝙉 𝘼𝘾𝙃𝙄𝙀𝙑𝙀𝙈𝙀𝙉𝙏 👏🇵🇾

Example C — last-second drama with player stats:
𝐁𝐑𝐀𝐙𝐈𝐋 𝐈𝐍 𝐓𝐇𝐄 𝐕𝐄𝐑𝐘 𝐋𝐀𝐒𝐓 𝐒𝐄𝐂𝐎𝐍𝐃 🫨🫨🫨

𝗧𝗛𝗘𝗬 𝗔𝗗𝗩𝗔𝗡𝗖𝗘 𝗧𝗢 𝗧𝗛𝗘 𝗥𝗢𝗨𝗡𝗗 𝗢𝗙 𝟭𝟲 💪🇧🇷

Casemiro & Martinelli score the goals, with Bruno Guimarães adding another assist 😲

Bruno Guimarães this World Cup:
🅰️ vs Japan
🅰️🅰️ vs Scotland
🅰️ vs Japan

Example D — emotional farewell (losing team):
🇯🇵 𝐓𝐇𝐄 𝐃𝐑𝐄𝐀𝐌 𝐄𝐍𝐃𝐒.

𝙁𝙧𝙤𝙢 𝙜𝙧𝙤𝙪𝙥-𝙨𝙩𝙖𝙜𝙚 𝙝𝙚𝙧𝙤𝙚𝙨… 𝙩𝙤 𝙠𝙣𝙤𝙘𝙠𝙤𝙪𝙩 𝙝𝙚𝙖𝙧𝙩𝙗𝙧𝙚𝙖𝙠. 💔

Japan's FIFA World Cup 2026 journey comes to an end, falling just one step short of matching their best-ever World Cup finish.

𝐇𝐄𝐀𝐃𝐒 𝐇𝐄𝐋𝐃 𝐇𝐈𝐆𝐇. 💙

👏 ありがとう、日本. Until next time.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CAPTION BODY RULES:
- Open with a bold styled headline — make it feel like breaking news or a match-defining moment
- When showing the score, use EXACTLY the "EXACT SCORE LINE" provided — convert digits to number emojis (1️⃣, 2️⃣ etc.) but do not change the structure or recompute anything
- LENGTH IS CRITICAL — keep it SHORT. Hard cap: 90 words / 700 characters total (excluding hashtags), and no more than 6 short lines/paragraphs of body text. Do NOT write dense, multi-sentence paragraphs — every line should be one punchy sentence or fragment, not a run-on explanation.
- Default to short & punchy (3–5 short lines). Only occasionally (roughly 1 in 4 captions) go slightly longer to include a standout stat or record — even then, stay under the word cap and keep each line brief.
- Pick ONE or TWO standout facts to mention (a goal, a record, a stat) — do not try to cram in every scorer, every stat, and every record. Cut anything not essential to the headline moment.
- Use flag emojis for both teams naturally
- Sprinkle emojis throughout to make it visually engaging — but keep it tasteful. A well-placed 😤, 🔥, 💔, 😳, 🫨, 👏, ⚽, 🏆, 💪 at the end of a line lands well. Do NOT stack 3+ emojis in a row, do NOT use emojis that feel forced or unrelated to the moment.
- Do NOT recap the goal timeline or list out scorers by default — the scorecard graphic already shows that. Only name a scorer/assister if they ARE the standout fact (e.g. a hat-trick, a last-minute winner, a record-breaking goal). Most captions should mention zero or one player by name.
- If there's a genuinely notable stat or record (milestone, streak, historic first, dominant stat line), lead with that instead of the play-by-play. If nothing stands out, it's fine to just ride the emotion of the result — don't force a fact in.
- If records/milestones provided, weave them in naturally
- If group table provided, reference qualification implications
- Tone: passionate football fan — real, emotional, not corporate, not clickbait
- {"This is a half-time caption — capture the tension and drama of what's happened so far" if event_type == "HT" else "This is a full-time caption — capture the finality and emotion of the result"}

HASHTAG RULES (exactly 5, on one line after a blank line):
1. #HomeTeamName (no spaces, e.g. #BosniaAndHerzegovina)
2. #AwayTeamName (no spaces)
3. #FIFAWorldCup
4. #HomeTeamNickname (well-known nickname e.g. #ThreeLions, #LesBleus — if none, use a relevant tag, NOT a match abbreviation)
5. #AwayTeamNickname (same rule)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Match Details:
- Full match info: {json.dumps(_clean_match_sample(match_sample), ensure_ascii=False)}
- Moment: {moment_tag} ({moment})
- EXACT SCORE LINE (use this verbatim when showing the score — do not recompute or reformat it): {score_line}
- Match ended via: {match_ending}
- Result: {result_context}
- Goals: {goals_str}
{('- Stats: ' + stats_str) if stats_str else ''}{('\n- H2H: ' + h2h_str) if h2h_str else ''}{('\n- Recent form: ' + form_str) if form_str else ''}{('\n- Group table: ' + group_table_str) if group_table_str else ''}{('\n' + records_block) if records_block else ''}

Return ONLY the caption text — no labels, no explanations, no markdown formatting."""

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