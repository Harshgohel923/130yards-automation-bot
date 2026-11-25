"""
Deciding whether a post_ht-off match's first half is worth asking about.

Hard rules (4+ goals, a hat-trick) say yes on their own; otherwise Gemini
judges against the brief; if Gemini is down, the countable part of the brief
(3+ goals, a brace, a red card) decides.
"""

import json
import types

import pytest

import caption
import ht_judge as hj
from tests.helpers import goal


ENTRY = {'match_id': '1', 'home_team': 'Home', 'away_team': 'Away',
         'competition': 'Serie A'}


def half(hts=('1', '0'), events=()):
    return {'matchSample': {'team_A_name': 'Home', 'team_B_name': 'Away',
                            'hts_A': hts[0], 'hts_B': hts[1]},
            'events': list(events), 'statistics': {}}


def red(player='Defender', team='Away', second_yellow=False):
    e = {'type': 'red_card', 'player': player, 'team': team, 'minute': "30'"}
    if second_yellow:
        e['second_yellow'] = True
    return e


@pytest.fixture
def gemini(monkeypatch):
    """A fake Gemini client: set .answer to a dict, or .fail = True."""
    state = types.SimpleNamespace(answer={'worth_posting': False,
                                          'reason': 'a routine half'},
                                  fail=False, prompts=[])

    def generate_content(model=None, contents=None, config=None):
        state.prompts.append(contents)
        if state.fail:
            raise RuntimeError('429 quota')
        return types.SimpleNamespace(text=json.dumps(state.answer))

    monkeypatch.setattr(caption, 'client', types.SimpleNamespace(
        models=types.SimpleNamespace(generate_content=generate_content)))
    return state


# ── Hard rules ───────────────────────────────────────────────────────────────

def test_four_goals_is_a_yes_without_asking_gemini(gemini):
    v = hj.judge(half(('3', '1')), ENTRY)
    assert v == {'worth': True, 'reason': '4 goals in the first half', 'source': 'rule'}
    assert gemini.prompts == []


def test_a_hat_trick_is_a_yes_without_asking_gemini(gemini):
    events = [goal(team='Home', player='Kane', minute=m) for m in ('10', '22', '35')]
    v = hj.judge(half(('3', '0'), events), ENTRY)
    assert v['worth'] and v['source'] == 'rule' and 'Kane hat-trick' in v['reason']
    assert gemini.prompts == []


def test_own_goals_never_make_a_brace(gemini):
    events = [dict(goal(team='Away', player='Unlucky', minute=m), type='own_goal')
              for m in ('10', '20', '30')]
    assert hj.hard_reasons(half(('3', '0'), events)) == []


# ── Gemini's call ────────────────────────────────────────────────────────────

def test_gemini_decides_the_middle_ground(gemini):
    gemini.answer = {'worth_posting': True, 'reason': 'bottom side 2-0 up on the leaders'}
    v = hj.judge(half(('2', '0')), ENTRY)
    assert v == {'worth': True, 'reason': 'bottom side 2-0 up on the leaders',
                 'source': 'gemini'}


def test_gemini_can_say_no(gemini):
    v = hj.judge(half(('1', '0')), ENTRY)
    assert v['worth'] is False and v['source'] == 'gemini'


def test_the_prompt_carries_the_brief_and_the_half(gemini):
    events = [goal(team='Home', player='Kane', minute="10'"), red()]
    hj.judge(half(('1', '0'), events), dict(ENTRY, records=['Kane 300th goal']))
    prompt = gemini.prompts[0]
    assert 'brace or a hat-trick' in prompt and 'comeback' in prompt
    assert 'Half-time score: Home 1-0 Away' in prompt
    assert "10' goal: Kane (Home)" in prompt
    assert 'red_card: Defender (Away)' in prompt
    assert 'Kane 300th goal' in prompt


def test_the_table_reaches_the_prompt_for_upsets(gemini):
    data = half(('2', '0'))
    data['matchAnalysis'] = {'cup_table': {'list': [
        {'name': 'Home', 'rank': 20, 'points': 5},
        {'name': 'Away', 'rank': 1, 'points': 40}]}}
    hj.judge(data, ENTRY)
    assert 'Home 20 in the table (5 pts) · Away 1 in the table (40 pts)' in gemini.prompts[0]


# ── When Gemini is down ──────────────────────────────────────────────────────

def test_fallback_says_yes_to_a_brace(gemini):
    gemini.fail = True
    events = [goal(team='Home', player='Kane', minute=m) for m in ('10', '22')]
    v = hj.judge(half(('2', '0'), events), ENTRY)
    assert v['worth'] and v['source'] == 'fallback' and 'Kane brace' in v['reason']


def test_fallback_says_yes_to_a_red_card(gemini):
    gemini.fail = True
    v = hj.judge(half(('0', '0'), [red(second_yellow=True)]), ENTRY)
    assert v['worth'] and 'second yellow for Defender' in v['reason']


def test_fallback_says_yes_to_three_goals(gemini):
    gemini.fail = True
    v = hj.judge(half(('2', '1')), ENTRY)
    assert v['worth'] and '3 goals' in v['reason']


def test_fallback_says_no_to_a_routine_half(gemini):
    gemini.fail = True
    v = hj.judge(half(('1', '0')), ENTRY)
    assert v['worth'] is False and v['source'] == 'fallback'


def test_a_garbled_answer_counts_as_gemini_failing(gemini):
    gemini.answer = {'worth_posting': 'maybe', 'reason': '?'}
    v = hj.judge(half(('2', '1')), ENTRY)
    assert v['source'] == 'fallback' and v['worth']
