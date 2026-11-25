"""
The aggregate on a second leg's scorecard and caption.

matches.json carries "second_leg": true and "first_leg_score" written the way
the first leg finished — first leg's home team first, which is tonight's away
team. The card shows "AGG h - a" under the score, sharing the line with the
shootout when there is one; the caption is told who goes through.
"""

import pytest

import two_legs as tl
import validate_matches as vm


# Liverpool 2-1 Club Brugge in the first leg; tonight Club Brugge host.
ENTRY = {'match_id': '1', 'home_team': 'Club Brugge', 'away_team': 'Liverpool',
         'second_leg': True, 'first_leg_score': '2-1'}


def test_the_first_leg_is_turned_round_to_tonights_sides():
    assert tl.first_leg_for(ENTRY) == (1, 2)


@pytest.mark.parametrize('text,expected', [
    ('2-1', (2, 1)), ('2 – 1', (2, 1)), ('0:0', (0, 0)), ('10-9', (10, 9)),
    ('two-one', None), ('', None), (None, None), ('2-1-1', None),
])
def test_first_leg_parsing(text, expected):
    assert tl.parse_first_leg(text) == expected


def test_aggregate_adds_tonights_score():
    assert tl.aggregate(ENTRY, '2', '0') == (3, 2)
    assert tl.aggregate(ENTRY, 1, 0) == (2, 2)


def test_no_aggregate_unless_it_is_a_second_leg():
    assert tl.aggregate(dict(ENTRY, second_leg=False), '2', '0') is None
    assert tl.aggregate({'home_team': 'A', 'away_team': 'B'}, '2', '0') is None
    assert tl.aggregate(dict(ENTRY, first_leg_score='junk'), '2', '0') is None


# ── The score it is added to ─────────────────────────────────────────────────

def test_half_time_uses_the_half_time_score():
    data = {'matchSample': {'hts_A': '1', 'hts_B': '0', 'fs_A': '3', 'fs_B': '0'}}
    tl.annotate(data, ENTRY, 'HT')
    assert tl.aggregate_of(data['matchSample']) == ('2', '2')


def test_shootout_goals_never_count_towards_the_aggregate():
    # 1-0 after extra time (2-2 on aggregate), then 4-2 on penalties: the feed's
    # fs_* carries the shootout goals.
    data = {'matchSample': {'fs_A': '5', 'fs_B': '2', 'ps_A': '4', 'ps_B': '2'}}
    tl.annotate(data, ENTRY, 'FT')
    assert tl.aggregate_of(data['matchSample']) == ('2', '2')


def test_annotate_clears_a_stale_value():
    data = {'matchSample': {'fs_A': '1', 'fs_B': '0', 'agg_A': '9', 'agg_B': '9'}}
    tl.annotate(data, {'home_team': 'A', 'away_team': 'B'}, 'FT')
    assert tl.aggregate_of(data['matchSample']) is None


# ── The line on the card ─────────────────────────────────────────────────────

def test_the_result_lines():
    assert tl.result_lines(('3', '2'), None) == ('AGG 3 - 2', None)
    assert tl.result_lines(('2', '2'), ('4', '2')) == ('AGG 2 - 2', 'PENALTIES: 4 - 2')
    assert tl.result_lines(None, ('4', '2')) == (None, 'PENALTIES: 4 - 2')  # as before
    assert tl.result_lines(None, None) == (None, None)


def test_both_cards_draw_the_line(monkeypatch):
    import overlay_scorebar as ob
    import scorecard as sc
    seen = {}

    def fake_overlay(*a, **k):
        seen['overlay'] = tl.result_lines(k.get('aggregate'), k.get('penalties'))

    monkeypatch.setattr(ob, 'add_scorecard_overlay', fake_overlay)
    data = {'matchSample': {'team_A_name': 'Club Brugge', 'team_B_name': 'Liverpool',
                            'fs_A': '5', 'fs_B': '2', 'ps_A': '4', 'ps_B': '2'},
            'events': []}
    tl.annotate(data, ENTRY, 'FT')
    ob._render_from_scraper_data(data, 'photo.png', 'out.png', 'FT')
    assert seen['overlay'] == ('AGG 2 - 2', 'PENALTIES: 4 - 2')

    src = open(sc.__file__, encoding='utf-8').read()
    assert 'two_legs.aggregate_of(match_sample)' in src


# ── The caption ──────────────────────────────────────────────────────────────

def test_a_team_can_win_the_night_and_go_out():
    # First leg Liverpool 3-0 Club Brugge; Brugge win 2-1 tonight: 2-4 overall.
    entry = dict(ENTRY, first_leg_score='3-0')
    agg = tl.aggregate(entry, '2', '1')
    assert agg == (2, 4)
    line = tl.tie_summary('Club Brugge', 'Liverpool', agg, None, final=True)
    assert 'Liverpool go through on aggregate' in line


def test_penalties_decide_a_level_tie():
    line = tl.tie_summary('Club Brugge', 'Liverpool', ('2', '2'), ('4', '2'),
                          final=True)
    assert 'Club Brugge go through on penalties (4-2)' in line


def test_half_time_only_says_who_is_ahead_so_far():
    line = tl.tie_summary('Club Brugge', 'Liverpool', ('2', '2'), None, final=False)
    assert 'so far' in line and 'go through' not in line


def test_the_caption_prompt_gets_the_tie(monkeypatch):
    import types
    import caption
    prompts = []

    def generate_content(model=None, contents=None, **k):
        prompts.append(contents)
        raise RuntimeError('no network in tests')

    monkeypatch.setattr(caption, 'client', types.SimpleNamespace(
        models=types.SimpleNamespace(generate_content=generate_content)))
    data = {'matchSample': {'team_A_name': 'Club Brugge', 'team_B_name': 'Liverpool',
                            'fs_A': '2', 'fs_B': '1'}, 'events': []}
    # Liverpool won the first leg 3-0, so Brugge win the night and go out.
    tl.annotate(data, dict(ENTRY, first_leg_score='3-0'), 'FT')
    caption.generate_caption(data, 'FT', home_name='Club Brugge',
                             away_name='Liverpool', competition='UEFA Champions League')
    assert prompts, 'Gemini was never asked'
    assert 'SECOND LEG' in prompts[0]
    assert 'Aggregate: Club Brugge 2-4 Liverpool — Liverpool go through on aggregate' \
        in prompts[0]


def test_an_ordinary_match_gets_no_tie_line(monkeypatch):
    import types
    import caption
    prompts = []

    def generate_content(model=None, contents=None, **k):
        prompts.append(contents)
        raise RuntimeError('no network in tests')

    monkeypatch.setattr(caption, 'client', types.SimpleNamespace(
        models=types.SimpleNamespace(generate_content=generate_content)))
    data = {'matchSample': {'team_A_name': 'A', 'team_B_name': 'B',
                            'fs_A': '2', 'fs_B': '1'}, 'events': []}
    caption.generate_caption(data, 'FT', home_name='A', away_name='B',
                             competition='Premier League')
    assert prompts and 'SECOND LEG' not in prompts[0]


# ── validate_matches.py ──────────────────────────────────────────────────────

def _problems(entry):
    base = {'match_id': '1', 'scraper_url': 'u', 'kickoff_utc': '2026-11-25T20:00:00Z',
            'home_team': 'A', 'away_team': 'B', 'competition': 'C'}
    return [p for p in vm._check_structure([dict(base, **entry)])
            if 'leg' in p]


def test_validation_needs_a_readable_first_leg():
    assert _problems({'second_leg': True, 'first_leg_score': '2-1'}) == []
    assert _problems({'second_leg': True})
    assert _problems({'second_leg': True, 'first_leg_score': 'two-one'})
    assert _problems({'second_leg': 'yes', 'first_leg_score': '2-1'})
    assert _problems({'first_leg_score': '2-1'})         # flag forgotten


def test_validation_reads_the_first_leg_back_in_words():
    (note,) = vm._second_leg_notes([ENTRY])
    assert note == ('Club Brugge vs Liverpool: first leg Liverpool 2-1 Club Brugge '
                    '→ aggregate before kickoff Club Brugge 1-2 Liverpool')


# ── Labels on the photo card: a real bold, properly tracked ──────────────────

def test_labels_use_oswald_bold_not_a_faked_stroke():
    import os
    import overlay_scorebar as ob
    assert os.path.exists(ob.LABEL_FONT_PATH)
    font = ob._label_font(29)
    assert 'Oswald' in font.getname()[0]
    assert font.getname()[1] == 'Bold'
    src = open(ob.__file__, encoding='utf-8').read()
    assert 'stroke_width' not in src


def test_tracked_text_is_centred_and_evenly_spaced():
    import overlay_scorebar as ob
    xs = []

    class Draw:
        def text(self, xy, ch, **kw):
            xs.append((ch, xy[0]))

    font = ob._label_font(20)
    ob._draw_tracked(Draw(), (500, 0), 'AGG', font, (0, 0, 0, 255), tracking=4)
    widths = [font.getlength(c) for c in 'AGG']
    total = sum(widths) + 2 * 4
    assert xs[0][1] == pytest.approx(500 - total / 2)
    assert xs[1][1] - xs[0][1] == pytest.approx(widths[0] + 4)


def test_half_time_label_renders(tmp_path, monkeypatch):
    from PIL import Image
    import overlay_scorebar as ob
    for name in ('_fetch_crest', '_load_competition_logo', '_load_brand_logo'):
        monkeypatch.setattr(ob, name, lambda *a, **k: None)
    photo = tmp_path / 'p.png'
    Image.new('RGB', (900, 1200), (30, 60, 120)).save(photo)
    out = tmp_path / 'o.png'
    ob.add_scorecard_overlay(str(photo), str(out), 'A', 'B', '1', '0',
                             event_type='HT', aggregate=('2', '1'))
    assert out.exists()
