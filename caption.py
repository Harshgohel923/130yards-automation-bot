# caption.py — Gemini caption generator
import json
import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

from hashtags import build_group_hashtags, build_hashtags, competition_label

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
    # Before kickoff — and whenever the scraper has nothing — this field is a
    # message string, not a dict. Reaching .get on it would fail the whole post
    # over a line of prompt garnish.
    if not isinstance(stats, dict):
        return ''
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


def _ensure_hashtags(text: str, hashtag_line: str) -> str:
    """
    Guarantee the caption ends with our exact hashtag line.

    Gemini follows the instruction most of the time, but not always — it
    occasionally drops a tag or invents its own. Strip whatever hashtag-only
    lines it produced at the end and append ours.
    """
    lines = [ln.strip() for ln in (text or '').splitlines()]
    while lines and (not lines[-1]
                     or all(w.startswith('#') for w in lines[-1].split())):
        lines.pop()
    return '\n'.join(lines + ['', hashtag_line])


def _space_out_lines(text: str) -> str:
    """Normalize spacing: exactly one blank line between every non-empty line."""
    lines = [ln.strip() for ln in text.splitlines()]
    return '\n\n'.join(ln for ln in lines if ln)


def generate_caption(scraper_data: dict, event_type: str = 'FT',
                     records: list | None = None,
                     home_name: str | None = None,
                     away_name: str | None = None,
                     competition: str | None = None) -> str:
    """
    Generate an Instagram caption from scraper_data.
    event_type: 'HT' → half-time caption, 'FT' → full-time caption.
    home_name/away_name: validated matches.json names — they win over the
    scraper's spelling in the caption text.
    competition: matches.json value; falls back to the scraper's. Drives the
    competition hashtag and the tone of the prompt.
    Tries gemini-2.5-flash first, then gemini-2.0-flash, then plain fallback.
    """
    match_sample = scraper_data.get('matchSample', {})
    home_team    = home_name or match_sample.get('team_A_name', 'Home')
    away_team    = away_name or match_sample.get('team_B_name', 'Away')
    competition  = competition or match_sample.get('competition_name', '')
    comp_label   = competition_label(competition)
    # Built in code, not by the model — see hashtags.py.
    hashtag_line = build_hashtags(home_team, away_team, competition)

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
    # Same story as the statistics field: a placeholder string when there is
    # nothing to report.
    analysis  = scraper_data.get('matchAnalysis')
    analysis  = analysis if isinstance(analysis, dict) else {}
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

    prompt = f"""You are a football content writer for an Instagram page covering {comp_label}.

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
- Refer to the teams the way fans do; for national teams a flag emoji reads well, for clubs use emojis tied to their colours or nickname instead of flags
- Sprinkle emojis throughout to make it visually engaging — but keep it tasteful. A well-placed 😤, 🔥, 💔, 😳, 🫨, 👏, ⚽, 🏆, 💪 at the end of a line lands well. Do NOT stack 3+ emojis in a row, do NOT use emojis that feel forced or unrelated to the moment.
- Do NOT recap the goal timeline or list out scorers by default — the scorecard graphic already shows that. Only name a scorer/assister if they ARE the standout fact (e.g. a hat-trick, a last-minute winner, a record-breaking goal). Most captions should mention zero or one player by name.
- If there's a genuinely notable stat or record (milestone, streak, historic first, dominant stat line), lead with that instead of the play-by-play. If nothing stands out, it's fine to just ride the emotion of the result — don't force a fact in.
- If records/milestones provided, weave them in naturally
- If group table provided, reference qualification implications
- Write every sentence on its own line and leave a BLANK LINE after each one — never put two sentences in the same paragraph
- Tone: passionate football fan — real, emotional, not corporate, not clickbait
- {"This is a half-time caption — capture the tension and drama of what's happened so far" if event_type == "HT" else "This is a full-time caption — capture the finality and emotion of the result"}

HASHTAGS — end the caption with a blank line, then this line copied EXACTLY as written, character for character. Do not reorder, restyle, translate, add or remove tags:
{hashtag_line}

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
            return _space_out_lines(
                _ensure_hashtags(response.text, hashtag_line))
        except Exception as e:
            print(f"[caption] {model} failed: {e} — trying next model...")

    print("[caption] All Gemini models failed — using fallback caption")
    return _fallback_caption(home_team, away_team, home_score, away_score,
                             event_type, hashtag_line)


def _group_match_facts(match: dict) -> str:
    """One match's facts for the group prompt — everything the model may draw on."""
    bits = [
        f"match_id: {match.get('match_id')}",
        f"{match.get('home_team')} vs {match.get('away_team')}",
        f"competition: {match.get('competition') or 'football'}",
        f"score: {match.get('home_score')}-{match.get('away_score')}",
    ]
    if match.get('penalties'):
        bits.append(f"penalty shootout: {match['penalties']}")
    if match.get('ending') and match['ending'] != 'full-time':
        bits.append(f"decided in: {match['ending']}")
    if match.get('goals'):
        bits.append(f"goals: {match['goals']}")
    if match.get('records'):
        bits.append('records: ' + '; '.join(str(r) for r in match['records']))
    return ' | '.join(bits)


def generate_group_caption(matches: list[dict]) -> str:
    """
    Caption for a carousel covering several matches.

    Gemini picks which one or two stories are worth telling and writes them as
    narrative — it is explicitly forbidden from stating any scoreline, because
    the scorecards in the carousel already carry every result and a model slip
    would otherwise publish a wrong score that Instagram won't let us edit.

    It returns the caption plus the match ids it chose, and those matches' teams
    become the hashtags, so the tags always describe what the caption is about.
    """
    if not matches:
        return ''

    facts = '\n'.join(f'- {_group_match_facts(m)}' for m in matches)
    competitions = []
    for m in matches:
        label = competition_label(m.get('competition'))
        if label and label not in competitions:
            competitions.append(label)

    prompt = f"""You are a football content writer for an Instagram page.

This is a CAROUSEL post collecting the full-time scorecards of {len(matches)} matches
({', '.join(competitions) or 'football'}). Write ONE caption for the whole post.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UNICODE FONT STYLES — use ONLY these two, no others (never script/calligraphic):
  1. Bold Italic Sans (e.g. 𝙏𝙝𝙚 𝙦𝙪𝙞𝙘𝙠 𝙗𝙧𝙤𝙬𝙣 𝙛𝙤𝙭)   → punchy secondary lines
  2. Bold Serif       (e.g. 𝐓𝐡𝐞 𝐪𝐮𝐢𝐜𝐤 𝐛𝐫𝐨𝐰𝐧 𝐟𝐨𝐱)      → the opening headline
Plain text for regular sentences, PLAIN CAPITALS for extra emphasis. Within any
single word use ONE style for every letter — never mix styled and plain
characters inside a word, it renders as broken text.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

THE ONE RULE YOU MUST NOT BREAK:
Never state a score, a scoreline, or a numeric result. No "3-0", no "3️⃣-0️⃣",
no "won 2-1", no "a 4-goal thriller", no aggregate numbers. The scorecards in
the carousel show every score already. Write about MOMENTS instead: who did
something remarkable, what it felt like, what it means. You may name players,
say a team won or lost, and describe a hat-trick or a late winner — just never
attach numbers to the result.

CAPTION RULES:
- Open with a bold styled headline about the single most striking thing that
  happened across all these matches
- Pick only ONE or TWO matches worth writing about. You do NOT need to mention
  every match — most of them will not be mentioned at all, and that is correct.
  Choose whatever is genuinely notable: a standout individual performance, an
  upset, a shootout, a comeback, a milestone.
- LENGTH IS CRITICAL — hard cap 80 words / 600 characters, at most 5 short
  lines. Every line is one punchy sentence or fragment, never a dense paragraph.
- Write every sentence on its own line with a BLANK LINE after it
- Refer to teams the way fans do; club emojis tied to their colours or nickname,
  flags only for national teams
- Emojis throughout but tasteful — never 3+ in a row, never forced
- Tone: passionate football fan — real, emotional, not corporate, not clickbait
- Do NOT write a hashtag line. Hashtags are added separately.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The matches in this carousel:
{facts}

Return ONLY JSON with exactly these two keys:
  "caption"            — the caption text
  "highlight_match_ids" — array of the match_id strings you actually wrote
                          about, most prominent first (1 or 2 ids)"""

    schema = {
        'type': 'object',
        'properties': {
            'caption': {'type': 'string'},
            'highlight_match_ids': {'type': 'array', 'items': {'type': 'string'}},
        },
        'required': ['caption', 'highlight_match_ids'],
    }

    for model in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json',
                    response_schema=schema,
                ),
            )
            payload = json.loads(response.text)
            caption = str(payload.get('caption') or '').strip()
            if not caption:
                raise ValueError('empty caption in response')
            highlights = [str(i) for i in (payload.get('highlight_match_ids') or [])]
            print(f"[caption] Group caption via {model} "
                  f"(highlighting {highlights or 'nothing in particular'})")
            return _space_out_lines(
                _ensure_hashtags(caption, _group_hashtag_line(matches, highlights)))
        except Exception as e:
            print(f"[caption] {model} failed: {e} — trying next model...")

    print("[caption] All Gemini models failed — using fallback group caption")
    return _fallback_group_caption(matches)


def _group_hashtag_line(matches: list[dict], highlight_ids: list[str]) -> str:
    """
    Team tags for a group post, drawn from the matches the caption highlighted.

    Unrecognised ids are ignored, and every other match follows in carousel
    order — so a model that returns nothing usable still yields a full tag line.
    """
    by_id = {str(m.get('match_id')): m for m in matches}
    ordered = [by_id[i] for i in highlight_ids if i in by_id]
    ordered += [m for m in matches if m not in ordered]

    teams = []
    for match in ordered:
        teams.extend([match.get('home_team', ''), match.get('away_team', '')])
    return build_group_hashtags(teams)


def _fallback_group_caption(matches: list[dict]) -> str:
    """Deterministic group caption — no scores, matching the model's brief."""
    count = len(matches)
    return (
        f"FULL TIME.\n"
        f"\n"
        f"Every result from {'today' if count > 1 else 'the day'}, "
        f"all {count} of them.\n"
        f"\n"
        f"Swipe through 👉\n"
        f"\n"
        f"{_group_hashtag_line(matches, [])}"
    )


def _fallback_caption(home, away, hs, as_, event_type, hashtag_line):
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

    return (
        f"{moment}: {home} {hs}-{as_} {away}\n"
        f"\n"
        f"{result}\n"
        f"\n"
        f"{hashtag_line}"
    )