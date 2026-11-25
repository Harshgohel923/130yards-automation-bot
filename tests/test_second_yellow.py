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
