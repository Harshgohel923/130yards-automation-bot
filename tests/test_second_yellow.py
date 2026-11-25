"""
A second yellow is a sending-off, and the card has to show it.

The feed draws a dismissal for a second booking with its own yellow-and-red
icon (…Iun8…png). The scraper didn't know it, so it classified as 'other' and
the red never reached a scorecard — nine matches in the archive by 2026-11-25,
Barcelona vs Villarreal (Pau Navarro, 90+3') among them.

It is now a red_card to everything downstream, flagged second_yellow=True.
"""

import json

import main
import manual_match
import overlay_scorebar
import scorecard
import football_scraper_dom as scraper


SECOND_YELLOW = ('https://img-sd.allfootballapp.com/fastdfs7/M00/C1/02/'
                 'rBUC6GcM2SiAIun8AAADLFMWSvE755.png')
YELLOW = ('https://img-sd.allfootballapp.com/fastdfs3/M00/B9/3E/'
          'ChOxM1xGg02AL8TeAAAE1p6R8m4139.png')
RED = ('https://img-sd.allfootballapp.com/fastdfs3/M00/B9/3E/'
       'ChOxM1xGg02AWiDjAAAE3Y8ENZg267.png')


def test_the_yellow_and_red_icon_is_a_red_card():
    assert scraper.parse_event_type(SECOND_YELLOW)[0] == 'red_card'
    assert scraper.is_second_yellow(SECOND_YELLOW)


def test_plain_cards_are_not_mistaken_for_it():
    assert scraper.parse_event_type(YELLOW)[0] == 'yellow_card'
    assert scraper.parse_event_type(RED)[0] == 'red_card'
    assert not scraper.is_second_yellow(YELLOW)
    assert not scraper.is_second_yellow(RED)
    assert not scraper.is_second_yellow(None)


def _page(events):
    """A mobile match page carrying just enough state for _parse_match_page."""
    state = {'matchDetailStore': {
        'matchDetailFormation': {'matchSample': {
            'team_A_name': 'Barcelona', 'team_B_name': 'Villarreal',
            'fs_A': '3', 'fs_B': '2'}},
        'matchOverviewFormation': {'status': 'Played', 'events': events,
                                   'statistics': []},
    }}
    return (f'<script>window.__INITIAL_STATE__ = {json.dumps(state)};'
            f'</script>')


class _Resp:
    status_code = 200
    encoding = 'utf-8'

    def __init__(self, text):
        self.text = text


def _scrape(monkeypatch, events):
    monkeypatch.setattr(scraper.requests, 'get',
                        lambda *a, **k: _Resp(_page(events)))
    return scraper.get_match_data('https://m.allfootballapp.com/match/x/1')


def test_scraper_emits_a_flagged_red_card(monkeypatch):
    data = _scrape(monkeypatch, [
        {'minute': 47, 'teamAEvents': [], 'teamBEvents': [
            {'person': 'Pau Navarro', 'person_id': '50843799',
             'event_pic': YELLOW}]},
        {'minute': 90, 'teamAEvents': [], 'teamBEvents': [
            {'person': 'Pau Navarro', 'person_id': '50843799',
             'event_pic': SECOND_YELLOW}]},
    ])
    cards = [e for e in data['events'] if 'card' in e['type']]
    assert [(e['type'], e['minute'], e.get('second_yellow')) for e in cards] == [
        ('yellow_card', "47'", None),
        ('red_card', "90'", True),
    ]
    assert cards[1]['team'] == 'Villarreal'


def test_a_straight_red_carries_no_flag(monkeypatch):
    data = _scrape(monkeypatch, [
        {'minute': 30, 'teamAEvents': [
            {'person': 'Someone', 'person_id': '1', 'event_pic': RED}],
         'teamBEvents': []},
    ])
    (red,) = [e for e in data['events'] if e['type'] == 'red_card']
    assert 'second_yellow' not in red


EVENT = {'minute': "90'", 'type': 'red_card', 'icon': '🟨🟥',
         'player': 'Pau Navarro', 'player_id': '50843799',
         'team': 'Villarreal', 'second_yellow': True}


def test_it_appears_on_both_card_styles():
    overlay = overlay_scorebar._extract_scorer_lines([EVENT], 'Villarreal')
    assert len(overlay) == 1
    template = scorecard._extract_scorer_lines([EVENT], 'Villarreal')
    assert len(template) == 1
    assert template[0]['symbol_path'].endswith('red_card.jpg')


def test_it_makes_a_live_card_stale():
    # A second yellow after an early card went up changes what the card
    # should show, without touching the score.
    assert main._card_events({'events': [EVENT]}) == [
        "red_card|Villarreal|Pau Navarro|90'"]


def test_manual_cards_accept_it_by_name():
    for typed in ('second yellow', 'double yellow', '2y'):
        assert manual_match.EVENT_ALIASES[typed] == 'red_card'


def test_the_overlay_draws_it_as_two_yellow_cards():
    (line,) = overlay_scorebar._extract_scorer_lines([EVENT], 'Villarreal')
    assert line['type'] == 'second_yellow'

    one = overlay_scorebar._event_symbol('yellow_card', 40)
    pair = overlay_scorebar._event_symbol('second_yellow', 40)
    assert pair.height == one.height
    assert pair.width > 2 * one.width                  # two cards and a gap
    # Both cards are yellow — no red anywhere in the glyph.
    reds = [p for p in pair.convert("RGBA").get_flattened_data() if p[3] == 255 and p[0] > 180 and p[1] < 80]
    assert not reds


def test_a_straight_red_is_still_one_red_card():
    straight = dict(EVENT, second_yellow=None)
    (line,) = overlay_scorebar._extract_scorer_lines([straight], 'Villarreal')
    assert line['type'] == 'red_card'


# ── Classic card, and minutes staying in line ────────────────────────────────

def test_the_classic_card_draws_it_as_two_yellow_cards():
    (line,) = scorecard._extract_scorer_lines([EVENT], 'Villarreal')
    assert line['second_yellow'] is True
    pair = scorecard._entry_symbol(line, 40)
    one = scorecard._load_symbol(scorecard.LOCAL_SYMBOLS['yellow_card'], 40)
    assert one is not None                       # the asset path is real
    assert pair.width > 2 * one.width and pair.height == 40
    # Left half is the yellow asset, not the red one.
    r, g, b, _ = pair.getpixel((one.width // 2, 20))
    assert r > 150 and g > 150 and b < 120


def _minute_starts(module, lines, align):
    """x of every minute the renderer draws, read off its draw.text calls."""
    from PIL import Image, ImageDraw
    img = Image.new('RGBA', (1000, 600), (0, 0, 0, 0))
    calls = []

    class Spy:
        def __init__(self, real):
            self.real = real

        def text(self, xy, text, **kw):
            calls.append((text, xy[0], kw.get('anchor')))
            return self.real.text(xy, text, **kw)

        def __getattr__(self, name):
            return getattr(self.real, name)

    draw = Spy(ImageDraw.Draw(img))
    box = (100, 100, 600, 500)
    if module is scorecard:
        scorecard._draw_scorer_lines(img, draw, lines, box, align, 40, 10)
        minutes = {ln['minute'] for ln in lines}
    else:
        overlay_scorebar._draw_scorer_lines(img, draw, lines, box, align, 40,
                                            60, 10)
        minutes = {ln['minute'] for ln in lines}
    return [x for text, x, _ in calls if text in minutes]


def test_minutes_line_up_beside_a_second_yellow_on_both_cards():
    goal_ev = dict(EVENT, type='goal', minute="5'", player='Buchanan',
                   second_yellow=None)
    events = [goal_ev, EVENT]

    # Classic home side: minutes are left-anchored, so their x must match.
    lines = scorecard._extract_scorer_lines(events, 'Villarreal')
    xs = _minute_starts(scorecard, lines, 'left')
    assert len(xs) == 2 and xs[0] == xs[1]

    # Photo card, both sides: 'lm' / 'rm' anchors, so the x must match too.
    lines = overlay_scorebar._extract_scorer_lines(events, 'Villarreal')
    for align in ('left', 'right'):
        xs = _minute_starts(overlay_scorebar, lines, align)
        assert len(xs) == 2 and xs[0] == xs[1], align


def test_width_helpers_match_what_is_drawn():
    # The fitting pass sizes the layout from these without loading images,
    # so they must agree with the real symbols to the pixel.
    for t in ('goal', 'penalty_goal', 'own_goal', 'penalty_missed',
              'red_card', 'second_yellow'):
        for h in (20, 31, 44):
            assert (overlay_scorebar._event_symbol(t, h).width
                    == overlay_scorebar._symbol_width(t, h)), (t, h)
    for t in ('goal', 'penalty_goal', 'own_goal', 'penalty_missed', 'red_card'):
        for flag in (False, True):
            e = {'symbol_path': scorecard._goal_symbol_path(t),
                 'second_yellow': flag}
            for h in (20, 31, 44):
                assert (scorecard._entry_symbol(e, h).width
                        == scorecard._symbol_width(e, h)), (t, flag, h)
