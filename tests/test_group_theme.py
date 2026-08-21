"""
The theme a carousel is given, and the flow that has none.

A pile of results tells the caption writer that some matches happened. It
cannot tell it that these are Arsenal's last five — so without a theme the
model writes the matchday round-up that the scraped `carousel_group` flow
wants and a hand-built retrospective does not.

The half worth pinning is that adding this changed nothing for that flow:
`carousel.py` calls `generate_group_caption` with no theme, always, because a
matchday post has one by definition and no human to ask.
"""

import pytest

import caption
import manual_match as mm

MATCHES = [
    {'match_id': '1', 'home_team': 'Arsenal', 'away_team': 'Man City',
     'competition': 'Premier League', 'home_score': '2', 'away_score': '0'},
    {'match_id': '2', 'home_team': 'Arsenal', 'away_team': 'Chelsea',
     'competition': 'Premier League', 'home_score': '3', 'away_score': '1'},
]


@pytest.fixture
def prompt(monkeypatch):
    """Capture the prompt, then fail every model so the fallback also runs."""
    seen = {}

    class Client:
        class models:
            @staticmethod
            def generate_content(model, contents, config=None):
                seen['text'] = contents
                raise RuntimeError('no network in tests')

    monkeypatch.setattr(caption, 'client', Client())
    return seen


# ── The prompt ────────────────────────────────────────────────────────────────

def test_the_theme_reaches_the_model(prompt):
    caption.generate_group_caption(MATCHES, theme="Arsenal's last five")
    assert "Arsenal's last five" in prompt['text']


def test_it_is_stated_twice(prompt):
    """Once framing the task and once beside the results — a long prompt
    otherwise buries the framing by the time the model reaches the facts."""
    caption.generate_group_caption(MATCHES, theme="Arsenal's last five")
    assert prompt['text'].count("Arsenal's last five") == 2


def test_no_theme_leaves_the_prompt_as_it_was(prompt):
    caption.generate_group_caption(MATCHES)
    assert 'What this set IS' not in prompt['text']
    assert 'Remember, this carousel is' not in prompt['text']


def test_an_empty_theme_is_the_same_as_none(prompt):
    """carousel.py passes nothing; a skipped question passes None. Neither may
    put a dangling 'What this set IS:' in the prompt."""
    caption.generate_group_caption(MATCHES, theme='   ')
    assert 'What this set IS' not in prompt['text']


def test_the_no_scores_rule_survives(prompt):
    """The one rule the group prompt must not lose."""
    caption.generate_group_caption(MATCHES, theme="Arsenal's last five")
    assert 'Never state a score' in prompt['text']


# ── The fallback ──────────────────────────────────────────────────────────────

def test_the_fallback_uses_the_theme():
    text = caption._fallback_group_caption(MATCHES, "Arsenal's last five")
    assert "Arsenal's last five" in text
    assert 'Every result from today' not in text


def test_the_fallback_without_a_theme_is_unchanged():
    """This is what the scraped carousel_group flow gets, and it was right for
    that flow before any of this existed."""
    text = caption._fallback_group_caption(MATCHES)
    assert 'Every result from today, all 2 of them.' in text


def test_the_fallback_still_carries_hashtags():
    for text in (caption._fallback_group_caption(MATCHES),
                 caption._fallback_group_caption(MATCHES, 'Matchweek 3')):
        assert '#Arsenal' in text


def test_a_trailing_full_stop_is_not_doubled():
    assert '..' not in caption._fallback_group_caption(MATCHES, 'Matchweek 3.')


# ── Parsing what was typed ────────────────────────────────────────────────────

def test_an_ordinary_theme_passes():
    assert mm.parse_theme("  Arsenal's   last five ") == "Arsenal's last five"


@pytest.mark.parametrize('text', ['', '   ', '\n'])
def test_empty_is_rejected(text):
    with pytest.raises(mm.ParseError, match='empty'):
        mm.parse_theme(text)


def test_something_with_no_letters_is_rejected():
    with pytest.raises(mm.ParseError, match='no letters'):
        mm.parse_theme('12345')


def test_too_long_is_rejected():
    with pytest.raises(mm.ParseError, match='limit'):
        mm.parse_theme('A' * (mm.MAX_THEME + 1))


def test_a_theme_that_looks_like_a_date_is_fine():
    """Unlike a team name, a theme is a phrase — 'Every match in August 2026'
    is a good one, so the wrong-step check must not run here."""
    assert mm.parse_theme('21 Aug 2026') == '21 Aug 2026'
    assert mm.parse_theme('Matchweek 3') == 'Matchweek 3'
