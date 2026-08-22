"""
The worker side of staged event photos: what posts, once, and what retries.

event_photos.pending() decides *which* pictures match the timeline; this is
about what the poll loop then does with them. The two failure modes worth
guarding are opposite and both visible on the feed: a picture posted twice
because a moment stays in the timeline for the rest of the match, and a
picture never posted because one Cloudinary hiccup read as "nothing staged".
"""

import pytest

import event_photos as ep
import main


ENTRY = {
    'match_id':   '54483541',
    'home_team':  'Argentina',
    'away_team':  'Brazil',
    'competition': 'Friendly',
    'scraper_url': 'https://example.invalid/match/54483541',
}

TIMELINE = [
    {'type': 'goal', 'player': 'Messi', 'team': 'Argentina', 'minute': "23'"},
    {'type': 'red_card', 'player': 'Neymar', 'team': 'Brazil', 'minute': "70'"},
]

SCRAPE = {
    'matchSample': {'team_A_name': 'Argentina', 'team_B_name': 'Brazil',
                    'fs_A': '1', 'fs_B': '0'},
    'events': TIMELINE,
}


@pytest.fixture
def worker(monkeypatch):
    """main with its outside world replaced, and a record of what it posted."""
    posted = []

    main.MATCH_STATE.clear()
    main._EVENT_PHOTO_CACHE.clear()

    main._EVENT_PHOTO_RETRACTIONS.clear()

    # A real posted_events table rather than a stub, so retraction — which
    # removes a row so the picture can go up again — is actually exercised.
    db: set[tuple[str, str]] = set()
    monkeypatch.setattr(main, '_save_state', lambda: None)
    monkeypatch.setattr(main, 'is_event_posted',
                        lambda mid, key: (mid, key) in db)
    monkeypatch.setattr(main, 'mark_event_posted',
                        lambda mid, key: db.add((mid, key)))
    monkeypatch.setattr(main, 'unmark_event_posted',
                        lambda mid, key: db.discard((mid, key)))
    monkeypatch.setattr(main, 'delete_instagram_post', lambda ig_id: None)
    monkeypatch.setattr(main, 'get_post_permalink', lambda ig_id: None)
    monkeypatch.setattr(main, 'send_music_reminder', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_alert', lambda *a, **k: None)
    monkeypatch.setattr(main, 'generate_event_caption',
                        lambda *a, **k: 'caption')
    monkeypatch.setattr(main, 'post_to_instagram',
                        lambda url, caption: posted.append(url) or 'ig-1')
    return posted


def _staging(*keys):
    """Cloudinary's answer: these (event, player) pairs have a picture."""
    return {ep.public_id(ENTRY['match_id'], k, p) for k, p in keys}


class TestWhatPosts:
    def test_a_match_with_nothing_staged_posts_nothing(self, worker, monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: set())
        main._run_event_photos(ENTRY, SCRAPE)
        assert worker == []

    def test_a_staged_moment_that_happened_posts(self, worker, monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, SCRAPE)
        assert worker == [ep.delivery_url(
            ep.public_id(ENTRY['match_id'], 'GOAL', 'Messi'))]

    def test_a_staged_moment_that_has_not_happened_waits(self, worker, monkeypatch):
        """Álvarez has a picture ready but has not scored — nothing goes up."""
        monkeypatch.setattr(ep, 'staged',
                            lambda mid: _staging(('GOAL', 'Álvarez')))
        main._run_event_photos(ENTRY, SCRAPE)
        assert worker == []

    def test_several_moments_in_one_poll_all_post(self, worker, monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(
            ('GOAL', 'Messi'), ('RED', 'Neymar')))
        main._run_event_photos(ENTRY, SCRAPE)
        assert len(worker) == 2

    def test_a_timeline_that_is_not_a_list_posts_nothing(self, worker, monkeypatch):
        """Before kickoff the scraper puts a sentence where the list goes."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, {'events': 'Match has not started yet'})
        assert worker == []


class TestPostedOnlyOnce:
    def test_the_same_moment_does_not_post_on_the_next_poll(self, worker,
                                                            monkeypatch):
        """A goal stays in the timeline for the rest of the match."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        for _ in range(5):
            main._run_event_photos(ENTRY, SCRAPE)
        assert len(worker) == 1

    def test_a_restart_that_kept_state_does_not_repost(self, worker, monkeypatch):
        """bot.db starts empty every Actions run; state.json is what survives."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main.MATCH_STATE[ENTRY['match_id']] = {
            'event_photos_posted': ['EVENT:GOAL:messi']}
        main._run_event_photos(ENTRY, SCRAPE)
        assert worker == []

    def test_a_restart_that_lost_state_is_caught_by_the_database(self, worker,
                                                                 monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        monkeypatch.setattr(main, 'is_event_posted',
                            lambda mid, key: key == 'EVENT:GOAL:messi')
        main._run_event_photos(ENTRY, SCRAPE)
        assert worker == []


class TestFailureIsRetried:
    def test_a_failed_post_goes_again_on_the_next_poll(self, worker, monkeypatch):
        """Not marking it posted is the whole of the retry — same as a card."""
        attempts = []

        def flaky(url, caption):
            attempts.append(url)
            if len(attempts) == 1:
                raise RuntimeError('Instagram said no')
            return 'ig-2'

        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        monkeypatch.setattr(main, 'post_to_instagram', flaky)

        main._run_event_photos(ENTRY, SCRAPE)
        main._run_event_photos(ENTRY, SCRAPE)
        assert len(attempts) == 2

    def test_a_failed_post_is_not_recorded_as_posted(self, worker, monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        monkeypatch.setattr(main, 'post_to_instagram',
                            lambda *a: (_ for _ in ()).throw(RuntimeError('no')))
        main._run_event_photos(ENTRY, SCRAPE)
        assert not main.MATCH_STATE.get(ENTRY['match_id'], {}).get(
            'event_photos_posted')


class TestStagedListing:
    def test_the_listing_is_cached_between_polls(self, monkeypatch):
        """One Cloudinary call a poll, for every match, would not be free."""
        calls = []
        monkeypatch.setattr(ep, 'staged',
                            lambda mid: calls.append(mid) or {'a'})
        main._EVENT_PHOTO_CACHE.clear()
        for _ in range(4):
            main._staged_event_photos('99')
        assert len(calls) == 1

    def test_an_outage_keeps_the_last_known_set(self, monkeypatch):
        """Answering "nothing staged" would skip a picture sitting right there."""
        main._EVENT_PHOTO_CACHE.clear()
        monkeypatch.setattr(ep, 'staged', lambda mid: {'a'})
        assert main._staged_event_photos('99') == {'a'}

        # Expire the cache, then break Cloudinary.
        main._EVENT_PHOTO_CACHE['99'] = (0.0, {'a'})
        monkeypatch.setattr(ep, 'staged',
                            lambda mid: (_ for _ in ()).throw(RuntimeError('502')))
        assert main._staged_event_photos('99') == {'a'}

    def test_an_outage_with_nothing_cached_yet_is_empty(self, monkeypatch):
        main._EVENT_PHOTO_CACHE.clear()
        monkeypatch.setattr(ep, 'staged',
                            lambda mid: (_ for _ in ()).throw(RuntimeError('502')))
        assert main._staged_event_photos('99') == set()


class TestCleanup:
    def test_everything_staged_is_dropped_when_the_worker_finishes(self,
                                                                   monkeypatch):
        removed = []
        monkeypatch.setattr(ep, 'delete_all',
                            lambda mid: removed.append(mid) or 3)
        main._EVENT_PHOTO_CACHE['54483541'] = (0.0, {'a'})
        main._clear_event_photos(ENTRY)
        assert removed == ['54483541']
        assert '54483541' not in main._EVENT_PHOTO_CACHE

    def test_a_cloudinary_failure_at_the_end_is_survivable(self, monkeypatch):
        """The match is over; leftovers are keyed to an id that never runs again."""
        monkeypatch.setattr(ep, 'delete_all',
                            lambda mid: (_ for _ in ()).throw(RuntimeError('502')))
        main._clear_event_photos(ENTRY)   # must not raise


AMBIGUOUS_SCRAPE = {
    'matchSample': {'team_A_name': 'Argentina', 'team_B_name': 'Brazil',
                    'fs_A': '1', 'fs_B': '0'},
    'events': [
        {'type': 'yellow_card', 'player': 'Bernardo Silva', 'team': 'Brazil',
         'minute': "60'"},
        {'type': 'yellow_card', 'player': 'Thiago Silva', 'team': 'Brazil',
         'minute': "70'"},
        {'type': 'goal', 'player': 'Rodrigo', 'team': 'Brazil', 'minute': "80'"},
    ],
}


class TestAmbiguityIsNeverGuessed:
    def test_a_name_matching_two_players_posts_nothing(self, worker, monkeypatch):
        """The wrong player on a public page has no undo."""
        alerts = []
        monkeypatch.setattr(main, 'send_alert',
                            lambda text, **k: alerts.append(text))
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('YELLOW', 'Silva')))
        main._run_event_photos(ENTRY, AMBIGUOUS_SCRAPE)
        assert worker == []
        assert len(alerts) == 1
        assert 'Bernardo Silva and Thiago Silva' in alerts[0]

    def test_the_full_name_posts_normally(self, worker, monkeypatch):
        monkeypatch.setattr(ep, 'staged',
                            lambda mid: _staging(('YELLOW', 'Thiago Silva')))
        main._run_event_photos(ENTRY, AMBIGUOUS_SCRAPE)
        assert len(worker) == 1

    def test_a_conflict_is_not_marked_posted_and_does_not_repeat_the_post(
            self, worker, monkeypatch):
        monkeypatch.setattr(main, 'send_alert', lambda *a, **k: None)
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('YELLOW', 'Silva')))
        for _ in range(3):
            main._run_event_photos(ENTRY, AMBIGUOUS_SCRAPE)
        assert worker == []


class TestUnfiredPhotosAreExplained:
    """A name that never matched must not simply vanish at full time."""

    @pytest.fixture
    def alerts(self, monkeypatch):
        sent = []
        main.MATCH_STATE.clear()
        main._EVENT_PHOTO_CACHE.clear()
        monkeypatch.setattr(main, '_save_state', lambda: None)
        monkeypatch.setattr(main, 'send_alert', lambda text, **k: sent.append(text))
        monkeypatch.setattr(ep, 'delete_all', lambda mid: 1)
        return sent

    def test_a_name_mismatch_is_named_at_full_time(self, alerts, monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Rodri')))
        main._clear_event_photos(ENTRY, AMBIGUOUS_SCRAPE)
        assert len(alerts) == 1
        assert 'Rodri' in alerts[0] and 'Rodrigo' in alerts[0]

    def test_a_moment_that_never_happened_is_not_an_alert(self, alerts,
                                                          monkeypatch):
        """Staging a photo for a goal that didn't come is the feature working."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('RED', 'Neymar')))
        main._clear_event_photos(ENTRY, AMBIGUOUS_SCRAPE)
        assert alerts == []

    def test_a_photo_that_posted_is_not_reported(self, alerts, monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Rodrigo')))
        main.MATCH_STATE[ENTRY['match_id']] = {
            'event_photos_posted': ['EVENT:GOAL:rodrigo']}
        main._clear_event_photos(ENTRY, AMBIGUOUS_SCRAPE)
        assert alerts == []

    def test_no_scrape_to_compare_against_is_survivable(self, alerts, monkeypatch):
        """A worker that broke before its first poll has none."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Rodri')))
        main._clear_event_photos(ENTRY, None)
        assert alerts == []


class TestOnePostPerMoment:
    def test_a_player_scoring_twice_posts_the_photo_once(self, worker,
                                                          monkeypatch):
        two_goals = {
            'matchSample': {'team_A_name': 'Argentina', 'team_B_name': 'Brazil',
                            'fs_A': '2', 'fs_B': '0'},
            'events': [
                {'type': 'goal', 'player': 'Messi', 'team': 'Argentina',
                 'minute': "23'"},
                {'type': 'goal', 'player': 'Messi', 'team': 'Argentina',
                 'minute': "61'"},
            ],
        }
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        for _ in range(4):
            main._run_event_photos(ENTRY, two_goals)
        assert len(worker) == 1

    def test_a_duplicate_claim_posts_once_and_says_so(self, worker, monkeypatch):
        alerts = []
        monkeypatch.setattr(main, 'send_alert',
                            lambda text, **k: alerts.append(text))
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(
            ('GOAL', 'Messi'), ('GOAL', 'Lionel Messi')))
        scrape = {
            'matchSample': {'team_A_name': 'Argentina', 'team_B_name': 'Brazil',
                            'fs_A': '1', 'fs_B': '0'},
            'events': [{'type': 'goal', 'player': 'Lionel Messi',
                        'team': 'Argentina', 'minute': "23'"}],
        }
        main._run_event_photos(ENTRY, scrape)
        assert len(worker) == 1
        assert len(alerts) == 1
        assert 'same moment' in alerts[0]


# A goal, and the same match after VAR took it away again.
WITH_GOAL = {
    'matchSample': {'team_A_name': 'Argentina', 'team_B_name': 'Brazil',
                    'fs_A': '1', 'fs_B': '0'},
    'events': [{'type': 'goal', 'player': 'Messi', 'team': 'Argentina',
                'minute': "23'"}],
}
DISALLOWED = {
    'matchSample': {'team_A_name': 'Argentina', 'team_B_name': 'Brazil',
                    'fs_A': '0', 'fs_B': '0'},
    'events': [],
}
SCORED_AGAIN = {
    'matchSample': {'team_A_name': 'Argentina', 'team_B_name': 'Brazil',
                    'fs_A': '1', 'fs_B': '0'},
    'events': [{'type': 'goal', 'player': 'Messi', 'team': 'Argentina',
                'minute': "70'"}],
}


class TestVarRetraction:
    """A goal the feed shows and then withdraws.

    The bias here is set by _early_card_stale, which refuses to act on a
    vanished event at all: a single poll without it is far more often the
    scraper dropping it than a decision. But sometimes it *is* VAR, and then a
    celebration photo is on the page and wrong — so the absence is confirmed
    over several polls rather than believed on the first.
    """

    @pytest.fixture
    def deleted(self, monkeypatch):
        gone = []
        monkeypatch.setattr(main, 'delete_instagram_post',
                            lambda ig_id: gone.append(ig_id))
        return gone

    def test_a_blip_does_not_take_the_post_down(self, worker, deleted,
                                                monkeypatch):
        """One poll without the goal is not evidence of anything."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        main._run_event_photos(ENTRY, DISALLOWED)
        assert deleted == []

    def test_it_holds_out_for_the_full_window(self, worker, deleted, monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS - 1):
            main._run_event_photos(ENTRY, DISALLOWED)
        assert deleted == []

    def test_a_confirmed_disappearance_deletes_the_post(self, worker, deleted,
                                                         monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS):
            main._run_event_photos(ENTRY, DISALLOWED)
        assert deleted == ['ig-1']

    def test_an_event_that_comes_back_resets_the_count(self, worker, deleted,
                                                       monkeypatch):
        """The scraper dropping it and restoring it must cost nothing."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS - 1):
            main._run_event_photos(ENTRY, DISALLOWED)
        main._run_event_photos(ENTRY, WITH_GOAL)        # back again
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS - 1):
            main._run_event_photos(ENTRY, DISALLOWED)
        assert deleted == []

    def test_the_photo_stays_on_cloudinary(self, worker, deleted, monkeypatch):
        """The whole point — he may still score one that stands."""
        cleared = []
        monkeypatch.setattr(ep, 'delete_all', lambda mid: cleared.append(mid))
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS):
            main._run_event_photos(ENTRY, DISALLOWED)
        assert deleted == ['ig-1']
        assert cleared == []          # nothing removed from Cloudinary

    def test_a_later_valid_goal_posts_the_same_photo_again(self, worker, deleted,
                                                            monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS):
            main._run_event_photos(ENTRY, DISALLOWED)
        main._run_event_photos(ENTRY, SCORED_AGAIN)
        assert len(worker) == 2          # posted, retracted, posted again

    def test_a_feed_that_keeps_flapping_is_eventually_left_alone(
            self, worker, deleted, monkeypatch):
        """Posting and withdrawing all afternoon happens in public."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        for _ in range(main.MAX_EVENT_PHOTO_RETRACTIONS + 2):
            main._run_event_photos(ENTRY, WITH_GOAL)
            for _ in range(main.EVENT_PHOTO_VANISHED_POLLS):
                main._run_event_photos(ENTRY, DISALLOWED)
        assert len(worker) == main.MAX_EVENT_PHOTO_RETRACTIONS

    def test_the_final_scrape_asks_instead_of_deleting(self, worker, deleted,
                                                       monkeypatch):
        """At the whistle there are no polls left to confirm with, and the
        only data is the scrape the last poll already judged. Deleting on
        that is acting once, irreversibly, on an ambiguous reading."""
        alerts = []
        monkeypatch.setattr(main, 'send_alert',
                            lambda text, **k: alerts.append(text))
        monkeypatch.setattr(ep, 'delete_all', lambda mid: 1)
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        main._clear_event_photos(ENTRY, DISALLOWED)     # worker exiting
        assert deleted == []
        assert any('wasn' in a and 'final timeline' in a for a in alerts)

    def test_a_post_that_stood_is_not_retracted_at_the_whistle(self, worker,
                                                               deleted,
                                                               monkeypatch):
        monkeypatch.setattr(ep, 'delete_all', lambda mid: 1)
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        main._clear_event_photos(ENTRY, WITH_GOAL)
        assert deleted == []

    def test_state_from_before_retraction_existed_is_survivable(self, worker,
                                                                deleted,
                                                                monkeypatch):
        """A worker resuming across the upgrade finds a plain list."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main.MATCH_STATE[ENTRY['match_id']] = {
            'event_photos_posted': ['EVENT:GOAL:messi']}
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS):
            main._run_event_photos(ENTRY, DISALLOWED)   # must not raise
        assert deleted == []          # no media id was ever recorded


class TestRetractionNeverFiresOnBadData:
    """Regression tests for the four routes the council found into a false
    retraction — deleting a correct post because the *timeline read* failed,
    not because the event was withdrawn.

    All four came from the same root cause: liveness was derived from
    `pending()`, which needs the staged Cloudinary listing as well as the
    timeline, so "pending didn't produce it" meant either "the event is gone"
    or "we couldn't read Cloudinary". Liveness now reads the timeline alone.
    """

    @pytest.fixture
    def deleted(self, monkeypatch):
        gone = []
        monkeypatch.setattr(main, 'delete_instagram_post',
                            lambda ig_id: gone.append(ig_id))
        monkeypatch.setattr(ep, 'delete_all', lambda mid: 0)
        return gone

    def post_one(self, monkeypatch):
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)

    # ── Route 1: the final scrape's timeline came back empty ────────────────
    def test_route1_blipped_empty_timeline_at_full_time(self, worker, deleted,
                                                        monkeypatch):
        self.post_one(monkeypatch)
        main._clear_event_photos(ENTRY, DISALLOWED)
        assert deleted == []

    # ── Route 2: the timeline came back as the pre-match sentinel string ────
    def test_route2_sentinel_string_timeline(self, worker, deleted, monkeypatch):
        """Before kickoff the feed puts a sentence where the list goes, and a
        bad scrape can return one again. It is not evidence of anything."""
        self.post_one(monkeypatch)
        sentinel = dict(WITH_GOAL, events='Match has not started yet')
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS + 2):
            main._run_event_photos(ENTRY, sentinel)
        main._clear_event_photos(ENTRY, sentinel)
        assert deleted == []

    # ── Route 3: Cloudinary unreadable, timeline fine ───────────────────────
    def test_route3_cloudinary_down_does_not_retract(self, worker, deleted,
                                                      monkeypatch):
        """The goal never left the timeline; only the listing failed."""
        self.post_one(monkeypatch)
        monkeypatch.setattr(ep, 'staged',
                            lambda mid: (_ for _ in ()).throw(RuntimeError('429')))
        main._EVENT_PHOTO_CACHE.clear()          # and the cache had expired
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS + 2):
            main._run_event_photos(ENTRY, WITH_GOAL)
        main._clear_event_photos(ENTRY, WITH_GOAL)
        assert deleted == []

    # ── Route 4: absent on the final poll only ──────────────────────────────
    def test_route4_one_miss_then_the_whistle(self, worker, deleted, monkeypatch):
        """One miss is not three. The whistle must not shorten the window."""
        self.post_one(monkeypatch)
        main._run_event_photos(ENTRY, DISALLOWED)     # miss 1 of 3
        main._clear_event_photos(ENTRY, DISALLOWED)
        assert deleted == []

    # ── And the real thing still works ──────────────────────────────────────
    def test_a_genuine_var_disallowance_still_retracts(self, worker, deleted,
                                                        monkeypatch):
        """The timeline is readable, has entries, and the goal is not in it."""
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(
            ('GOAL', 'Messi'), ('YELLOW', 'Casemiro')))
        with_both = {
            'matchSample': WITH_GOAL['matchSample'],
            'events': WITH_GOAL['events'] + [
                {'type': 'yellow_card', 'player': 'Casemiro', 'team': 'Brazil',
                 'minute': "30'"}],
        }
        goal_struck_off = {
            'matchSample': WITH_GOAL['matchSample'],
            'events': [{'type': 'yellow_card', 'player': 'Casemiro',
                        'team': 'Brazil', 'minute': "30'"}],
        }
        main._run_event_photos(ENTRY, with_both)
        assert len(worker) == 2
        for _ in range(main.EVENT_PHOTO_VANISHED_POLLS):
            main._run_event_photos(ENTRY, goal_struck_off)
        assert deleted == ['ig-1']          # the goal only; the booking stands


class TestInstagramBudget:
    """Event photos are evaluated before the HT/FT card triggers, so without a
    reservation the post that runs out of budget is the full-time card."""

    @pytest.fixture
    def quota(self, monkeypatch):
        def _set(used, cap=25):
            main._QUOTA_CACHE.clear()
            monkeypatch.setattr(main, 'publishing_limit', lambda: (used, cap))
        main._QUOTA_CACHE.clear()
        return _set

    def test_plenty_of_room_posts_normally(self, worker, quota, monkeypatch):
        quota(5)
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        assert len(worker) == 1

    def test_the_reserve_is_kept_for_the_cards(self, worker, quota, monkeypatch):
        quota(25 - main.INSTAGRAM_QUOTA_RESERVE)
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        assert worker == []

    def test_a_held_back_photo_is_not_marked_posted(self, worker, quota,
                                                     monkeypatch):
        """It must go out on its own if room frees up before the whistle."""
        quota(25 - main.INSTAGRAM_QUOTA_RESERVE)
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        quota(5)
        main._run_event_photos(ENTRY, WITH_GOAL)
        assert len(worker) == 1

    def test_an_unreadable_quota_does_not_block_posting(self, worker, quota,
                                                         monkeypatch):
        """A Graph blip must not become a new way for everything to fail."""
        main._QUOTA_CACHE.clear()
        monkeypatch.setattr(main, 'publishing_limit', lambda: None)
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(('GOAL', 'Messi')))
        main._run_event_photos(ENTRY, WITH_GOAL)
        assert len(worker) == 1

    def test_posts_inside_one_cache_window_still_count_down(self, worker, quota,
                                                             monkeypatch):
        """Several photos firing in one poll must not all see the same reading."""
        quota(25 - main.INSTAGRAM_QUOTA_RESERVE - 2)
        monkeypatch.setattr(ep, 'staged', lambda mid: _staging(
            ('GOAL', 'Messi'), ('RED', 'Neymar')))
        many = {
            'matchSample': WITH_GOAL['matchSample'],
            'events': WITH_GOAL['events'] + [
                {'type': 'red_card', 'player': 'Neymar', 'team': 'Brazil',
                 'minute': "70'"}],
        }
        main._run_event_photos(ENTRY, many)
        assert len(worker) == 2          # exactly the two the budget allowed
        main._run_event_photos(ENTRY, many)
        assert len(worker) == 2          # and no more
