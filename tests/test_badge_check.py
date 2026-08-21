"""
Badges for a typed-in team, checked while the name can still be fixed.

A scraped fixture gets this guarantee from validate_matches.py before kickoff.
A typed-in name has none, and a missing crest is not cosmetic — it is a blank
hole in the middle of the card, discovered at the preview when the name that
caused it is eight messages back.

The resolution itself belongs to logo_fetch and is exercised against the real
index by `validate_matches.py --check`. What is pinned here is the decision
layer on top: which of the three outcomes each case produces, and that a badge
supplied by hand can never overwrite one that already exists.
"""

import pytest

import telegram_bot as tb


class FakeLookup:
    """Stands in for logo_fetch + config, recording what was asked of it."""

    def __init__(self, *, official=None, fetch=None, existing=None):
        self.official = official      # str, or LookupError to raise
        self.fetch = fetch            # url, or an Exception to raise
        self.existing = existing      # what get_crest_url finds
        self.fetched = []

    def normalize_team_name(self, name):
        if isinstance(self.official, Exception):
            raise self.official
        return self.official or name

    def fetch_logo(self, name):
        self.fetched.append(name)
        if isinstance(self.fetch, Exception):
            raise self.fetch
        return self.fetch

    def get_crest_url(self, name, alert=True):
        assert alert is False, "the bot must not fire a Telegram alert here"
        return self.existing


@pytest.fixture
def lookup(monkeypatch):
    def install(**kw):
        fake = FakeLookup(**kw)
        import config
        import logo_fetch
        monkeypatch.setattr(logo_fetch, 'normalize_team_name',
                            fake.normalize_team_name)
        monkeypatch.setattr(logo_fetch, 'fetch_logo', fake.fetch_logo)
        monkeypatch.setattr(config, 'get_crest_url', fake.get_crest_url)
        return fake
    return install


# ── The three outcomes ────────────────────────────────────────────────────────

def test_a_known_name_resolves_silently(lookup):
    lookup(official='Arsenal', fetch='https://cdn/arsenal.png')
    official, url, problem = tb._check_crest('Arsenal')
    assert (official, url, problem) == ('Arsenal', 'https://cdn/arsenal.png', None)


def test_a_nickname_is_rewritten_to_the_official_name(lookup):
    """Badges are filed under official names, so resolution doubles as a
    spellchecker — and the official spelling is what belongs on the card, the
    same rewrite validate_matches.py applies to matches.json."""
    lookup(official='Tottenham Hotspur', fetch='https://cdn/spurs.png')
    official, _url, problem = tb._check_crest('Spurs')
    assert official == 'Tottenham Hotspur'
    assert problem is None


def test_an_unknown_name_reports_the_problem(lookup):
    lookup(official=LookupError("No logo found for 'Wingate & Finchley FC'"),
           existing=None)
    official, url, problem = tb._check_crest('Wingate & Finchley FC')
    assert official == 'Wingate & Finchley FC'   # nothing better to call it
    assert url is None
    assert 'Wingate' in problem


# ── Fallbacks ─────────────────────────────────────────────────────────────────

def test_a_hand_uploaded_crest_counts_even_when_the_index_never_heard_of_it(lookup):
    """`logo_fetch.py --local` is how a team outside the index gets a badge, and
    /card's own upload path writes to exactly that place. Ignoring it would ask
    for the same badge every single time."""
    lookup(official=LookupError('not in the index'),
           existing='https://cdn/manual.png')
    _official, url, problem = tb._check_crest('FC Ryukyu')
    assert url == 'https://cdn/manual.png'
    assert problem is None


def test_a_failed_fetch_falls_back_to_whatever_is_already_there(lookup):
    """The upload can fail on a network blip long after the crest itself
    landed. Asking about a crest that exists would be a false alarm."""
    lookup(official='Arsenal', fetch=RuntimeError('cloudinary timeout'),
           existing='https://cdn/arsenal.png')
    _official, url, problem = tb._check_crest('Arsenal')
    assert url == 'https://cdn/arsenal.png'
    assert problem is None


def test_a_failed_fetch_with_nothing_there_is_a_real_problem(lookup):
    lookup(official='Arsenal', fetch=RuntimeError('cloudinary timeout'),
           existing=None)
    _official, url, problem = tb._check_crest('Arsenal')
    assert url is None
    assert 'timeout' in problem


def test_no_alert_is_fired_while_asking(lookup):
    """get_crest_url(alert=True) sends a Telegram alert telling you to run
    validate_matches.py. Here the bot is already talking to you about this
    exact team, so the alert would be noise about a question in front of you.
    Asserted inside FakeLookup.get_crest_url."""
    lookup(official=LookupError('nope'), existing=None)
    tb._check_crest('Nobody FC')


# ── Where a badge question goes next ──────────────────────────────────────────

def test_every_target_knows_which_step_to_re_ask():
    assert tb.BADGE_RETRY == {
        'home': tb.M_HOME, 'away': tb.M_AWAY, 'competition': tb.M_COMPETITION}


def test_a_competition_is_never_a_hole():
    """get_competition_logo_url falls back to the brand mark, so a missing
    competition badge is cosmetic where a missing crest is not — and the
    question says so."""
    from config import get_brand_logo_url
    assert get_brand_logo_url()


# ── Not overwriting a real crest ──────────────────────────────────────────────

def test_an_uploaded_badge_never_replaces_an_existing_one(monkeypatch):
    """The lookup that sent us to the question searches a different table from
    the one that decides where an uploaded file lands, so a name it could not
    resolve can still slugify onto a real club's crest. upload_local_crest
    refuses that, and the refusal has to be honoured rather than forced."""
    import logo_fetch

    def refuse(path, name, kind=None, force=False):
        assert force is False, 'a hand-supplied badge must never be forced'
        raise RuntimeError('A crest already exists at assets/club/inter')

    monkeypatch.setattr(logo_fetch, 'upload_local_crest', refuse)
    monkeypatch.setattr('config.get_crest_url',
                        lambda name, alert=True: 'https://cdn/inter.png')

    url, saved = tb._save_badge('/tmp/x.png', 'Inter', 'home')
    assert url == 'https://cdn/inter.png'
    assert saved is False        # so the reply says "kept the existing one"


def test_a_refusal_with_nothing_behind_it_still_raises(monkeypatch):
    """If the upload was refused and no crest can be found either, something is
    wrong that the user has to hear about — not a silent success."""
    import logo_fetch

    def refuse(path, name, kind=None, force=False):
        raise RuntimeError('refused')

    monkeypatch.setattr(logo_fetch, 'upload_local_crest', refuse)
    monkeypatch.setattr('config.get_crest_url', lambda name, alert=True: None)

    with pytest.raises(RuntimeError):
        tb._save_badge('/tmp/x.png', 'Nobody FC', 'home')


def test_a_competition_badge_is_uploaded_as_a_competition(monkeypatch):
    """kind='competition' is what puts it in the competition folder instead of
    assets/club, where the competition lookup would never find it."""
    import logo_fetch
    seen = {}

    def capture(path, name, kind=None, force=False):
        seen['kind'] = kind
        return 'https://cdn/comp.png'

    monkeypatch.setattr(logo_fetch, 'upload_local_crest', capture)
    tb._save_badge('/tmp/x.png', 'Isthmian League', 'competition')
    assert seen['kind'] == 'competition'
