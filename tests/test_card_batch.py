"""
The pile of cards waiting to be posted together.

Only the pure parts: the manifest a card leaves behind, and the paths that put
the pile back in order. `add`, `pending` and `clear` talk to Cloudinary and
stay out of the suite, like every other network path in this codebase.

The manifest is what matters here. It is read back by a process that may never
have seen the conversation the card was typed into — the bot shuts down twenty
minutes after the last message — so anything missing from it is a card that
cannot be posted, and the only symptom is a carousel quietly one slide short.
"""

import pytest

import card_batch
import manual_match as mm


def built(event_type='FT', home_score='3', away_score='1'):
    return mm.build_scraper_data(
        home_team='Arsenal', away_team='Man City',
        home_score=home_score, away_score=away_score,
        competition='Premier League', when=mm.parse_date('21/08/2026'),
        event_type=event_type,
        home_events=mm.parse_scorers('23 Saka\n45+2 Havertz (pen)', 'Arsenal'),
        away_events=mm.parse_scorers('67 Rice (og)', 'Man City'))


@pytest.fixture
def manifest():
    return card_batch.build_manifest(
        built(), 'FT', 'Premier League', seq=1,
        image_url='https://res.cloudinary.com/x/manual_cards/7/001.png',
        public_id='manual_cards/7/001')


# ── The manifest ──────────────────────────────────────────────────────────────

def test_it_carries_what_the_group_caption_reads(manifest):
    """caption._group_match_facts() reads exactly these, and a missing one
    silently drops that fact from the prompt rather than failing."""
    for field in ('match_id', 'home_team', 'away_team', 'competition',
                  'home_score', 'away_score', 'penalties', 'ending',
                  'goals', 'records'):
        assert field in manifest


def test_it_carries_the_scraper_data_for_a_single_card_post(manifest):
    """A pile of one posts as an ordinary image with an ordinary match caption,
    and generate_caption() needs the whole dict — which by then exists nowhere
    else."""
    assert manifest['scraper_data']['matchSample']['team_A_name'] == 'Arsenal'


def test_full_time_scores_come_from_fs(manifest):
    assert (manifest['home_score'], manifest['away_score']) == ('3', '1')
    assert manifest['ending'] == 'full-time'


def test_half_time_scores_come_from_hts():
    """A half-time card stores hts_* and leaves fs_* unset, so reading fs_*
    unconditionally would caption every HT card as 0–0."""
    m = card_batch.build_manifest(built('HT', '2', '0'), 'HT', 'Premier League',
                                  seq=1, image_url='u', public_id='p')
    assert (m['home_score'], m['away_score']) == ('2', '0')
    assert m['ending'] == 'half-time'


def test_goals_are_summarised_for_the_prompt(manifest):
    assert 'Saka' in manifest['goals']
    assert 'Havertz' in manifest['goals']


def test_the_image_url_is_kept(manifest):
    """The post is built from Cloudinary URLs, never from local files — by the
    time it happens the render is long deleted."""
    assert manifest['image_url'].startswith('https://')


def test_the_ceiling_is_instagrams(manifest):
    from instagram import CAROUSEL_MAX_ITEMS
    assert card_batch.MAX_CARDS == CAROUSEL_MAX_ITEMS


# ── Ordering ──────────────────────────────────────────────────────────────────

def test_ids_are_zero_padded_so_a_raw_listing_sorts_right():
    """Cloudinary lists lexicographically, so card 10 must not sort between 1
    and 2 — the sequence is the carousel order."""
    ids = [card_batch._slide_id('7', n) for n in (1, 2, 10)]
    assert ids == sorted(ids)


def test_the_manifest_sits_beside_its_image():
    assert card_batch._manifest_id('7', 3) == card_batch._slide_id('7', 3) + '.json'


def test_owners_do_not_share_a_pile():
    assert card_batch._prefix('7') != card_batch._prefix('8')


# ── The readback ──────────────────────────────────────────────────────────────

def test_describe_numbers_the_cards_in_carousel_order(manifest):
    second = dict(manifest, seq=2, home_team='Arsenal', away_team='Chelsea',
                  home_score='2', away_score='0')
    text = card_batch.describe([manifest, second])
    assert text.splitlines()[0].startswith('1. Arsenal 3–1 Man City')
    assert text.splitlines()[1].startswith('2. Arsenal 2–0 Chelsea')


def test_describe_includes_the_date_when_there_is_one(manifest):
    assert '21 AUG 2026' in card_batch.describe([manifest])


def test_describe_survives_a_manifest_missing_fields():
    """Written by an older version of this module, say. A pile that can't be
    listed is a pile that can't be cleared."""
    assert card_batch.describe([{'seq': 1}])


def test_describe_of_nothing_is_empty():
    assert card_batch.describe([]) == ''
