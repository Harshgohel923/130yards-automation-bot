"""
Staged event photos: the key a picture is stored under, and what fires it.

Everything here is the seam between three processes that never talk to each
other. The bot writes a Cloudinary public_id, the match worker recomputes it
from a scrape and looks it up, and if the two disagree by so much as an accent
the picture silently never posts — no error anywhere, just a page missing the
photo somebody uploaded. So the naming and the matching are pinned down here.
"""

import pytest

import event_photos as ep


class TestPlayerSlug:
    @pytest.mark.parametrize('name,expected', [
        ('Messi', 'messi'),
        ('Cristian Romero', 'cristian-romero'),
        ('Gerónimo Rulli', 'geronimo-rulli'),      # accent folded
        ('N. Gonzalez', 'n-gonzalez'),             # the dot is not part of it
        ('  Lautaro  ', 'lautaro'),
        ("O'Reilly", 'o-reilly'),
        ('Şahin', 'sahin'),
    ])
    def test_folds_to_a_stable_slug(self, name, expected):
        assert ep.player_slug(name) == expected

    def test_typing_it_plainly_finds_the_feeds_spelling(self):
        """The whole reason names are slugged rather than compared."""
        assert ep.player_slug('geronimo rulli') == ep.player_slug('Gerónimo Rulli')

    @pytest.mark.parametrize('junk', ['', None, '   ', '???', '—'])
    def test_unusable_names_slug_to_nothing(self, junk):
        assert ep.player_slug(junk) == ''


class TestPublicId:
    def test_is_match_event_and_player(self):
        assert (ep.public_id('54483541', 'GOAL', 'Messi')
                == 'event_photos/54483541_GOAL_messi')

    def test_two_scorers_in_one_match_do_not_collide(self):
        messi = ep.public_id('99', 'GOAL', 'Messi')
        alvarez = ep.public_id('99', 'GOAL', 'Álvarez')
        assert messi != alvarez

    def test_a_goal_and_a_red_card_for_one_player_do_not_collide(self):
        assert (ep.public_id('99', 'GOAL', 'Neymar')
                != ep.public_id('99', 'RED', 'Neymar'))

    def test_the_same_player_in_two_matches_does_not_collide(self):
        assert (ep.public_id('1', 'GOAL', 'Messi')
                != ep.public_id('2', 'GOAL', 'Messi'))

    def test_delivery_url_asks_cloudinary_for_a_jpeg(self):
        """Instagram fetches this URL itself and only takes JPEG."""
        url = ep.delivery_url('event_photos/99_GOAL_messi')
        assert url.endswith('/event_photos/99_GOAL_messi.jpg')
        assert 'f_jpg' in url


class TestEventKeys:
    @pytest.mark.parametrize('scraper_type,key', [
        ('goal', 'GOAL'),
        ('penalty_goal', 'PEN'),
        ('own_goal', 'OG'),
        ('yellow_card', 'YELLOW'),
        ('red_card', 'RED'),
        ('substitution_in', 'SUB_IN'),
        ('substitution_out', 'SUB_OUT'),
    ])
    def test_every_offered_event_maps_from_the_scraper(self, scraper_type, key):
        ev = {'type': scraper_type, 'player': 'X', 'player_id': '50000116'}
        assert ep.event_keys_for(ev) == [(key, 'X', '50000116')]

    def test_a_missing_id_is_an_empty_string_not_a_crash(self):
        """Older archived scrapes, and any entry the feed left thin."""
        assert ep.event_keys_for({'type': 'goal', 'player': 'X'}) \
            == [('GOAL', 'X', '')]

    def test_a_paired_substitution_offers_both_players(self):
        """The feed merges an on/off pair into one entry — either can be staged."""
        ev = {'type': 'substitution',
              'player_in': 'Lautaro', 'player_in_id': '50238753',
              'player_out': 'De Paul', 'player_out_id': '50159964'}
        assert ep.event_keys_for(ev) == [('SUB_IN', 'Lautaro', '50238753'),
                                         ('SUB_OUT', 'De Paul', '50159964')]

    def test_a_half_substitution_still_offers_the_half_it_has(self):
        ev = {'type': 'substitution', 'player_in': 'Lautaro',
              'player_in_id': '50238753', 'player_out': None}
        assert ep.event_keys_for(ev) == [('SUB_IN', 'Lautaro', '50238753')]

    @pytest.mark.parametrize('ev', [
        {'type': 'var', 'player': 'Messi'},
        {'type': 'penalty_missed', 'player': 'Messi'},
        {'type': 'half_time', 'player': 'N/A', 'team': None},
    ])
    def test_events_with_no_button_are_ignored(self, ev):
        """Nothing can be staged against these, so nothing can fire on them."""
        assert ep.event_keys_for(ev) == []

    def test_a_nameless_event_is_ignored(self):
        """'N/A' is what the feed writes where it has no name."""
        assert ep.event_keys_for({'type': 'red_card', 'player': 'N/A'}) == []

    def test_every_button_has_something_that_fires_it(self):
        """A button with neither a scraper type nor a milestone behind it
        would be offered, staged against, and then never post."""
        derived = set(ep.GOAL_MILESTONES.values())
        for key, _label, types in ep.EVENT_CHOICES:
            assert types or key in derived, f'{key} fires on nothing'
            assert all(ep.TYPE_TO_KEY[t] == key for t in types)

    def test_a_derived_event_has_no_scraper_type(self):
        """The two are alternatives — a key claiming both would fire twice."""
        for key in ep.GOAL_MILESTONES.values():
            assert key not in ep.TYPE_TO_KEY.values()


class TestPending:
    MATCH = '54483541'

    def timeline(self):
        return [
            {'type': 'goal', 'player': 'Messi', 'team': 'Argentina',
             'minute': "23'", 'assister': 'Álvarez'},
            {'type': 'goal', 'player': 'Álvarez', 'team': 'Argentina',
             'minute': "41'"},
            {'type': 'red_card', 'player': 'Neymar', 'team': 'Brazil',
             'minute': "70'"},
            {'type': 'yellow_card', 'player': 'Casemiro', 'team': 'Brazil',
             'minute': "72'"},
        ]

    def test_nothing_staged_means_nothing_to_post(self):
        """The whole of what makes this opt-in per match."""
        assert ep.pending(self.timeline(), set(), self.MATCH) == []

    def test_only_the_staged_moment_fires(self):
        staged = {ep.public_id(self.MATCH, 'GOAL', 'Messi')}
        got = ep.pending(self.timeline(), staged, self.MATCH)
        assert [m['player'] for m in got] == ['Messi']
        assert got[0]['event_key'] == 'GOAL'
        assert got[0]['minute'] == "23'"
        assert got[0]['assister'] == 'Álvarez'

    def test_two_scorers_in_one_match_fire_separately(self):
        staged = {ep.public_id(self.MATCH, 'GOAL', 'Messi'),
                  ep.public_id(self.MATCH, 'GOAL', 'Álvarez')}
        got = ep.pending(self.timeline(), staged, self.MATCH)
        assert [m['player'] for m in got] == ['Messi', 'Álvarez']
        assert len({m['posted_key'] for m in got}) == 2

    def test_a_card_and_a_goal_in_the_same_match_fire_separately(self):
        staged = {ep.public_id(self.MATCH, 'GOAL', 'Messi'),
                  ep.public_id(self.MATCH, 'RED', 'Neymar')}
        got = ep.pending(self.timeline(), staged, self.MATCH)
        assert [(m['player'], m['event_key']) for m in got] == [
            ('Messi', 'GOAL'), ('Neymar', 'RED')]

    def test_a_picture_staged_for_the_wrong_event_does_not_fire(self):
        """Messi's red-card picture must not go up because Messi scored."""
        staged = {ep.public_id(self.MATCH, 'RED', 'Messi')}
        assert ep.pending(self.timeline(), staged, self.MATCH) == []

    def test_a_picture_staged_for_another_match_does_not_fire(self):
        staged = {ep.public_id('99999999', 'GOAL', 'Messi')}
        assert ep.pending(self.timeline(), staged, self.MATCH) == []

    def test_a_typed_name_matches_the_feeds_spelling(self):
        staged = {ep.public_id(self.MATCH, 'GOAL', 'alvarez')}
        got = ep.pending(self.timeline(), staged, self.MATCH)
        assert [m['player'] for m in got] == ['Álvarez']

    def test_the_posted_key_is_stable_across_polls(self):
        """It is the restart guard, so it must not carry the minute or score."""
        staged = {ep.public_id(self.MATCH, 'GOAL', 'Messi')}
        first = ep.pending(self.timeline(), staged, self.MATCH)[0]
        later = self.timeline()
        later[0]['minute'] = "23+1'"          # the feed revised the clock
        second = ep.pending(later, staged, self.MATCH)[0]
        assert first['posted_key'] == second['posted_key']

    def test_a_second_goal_reuses_the_picture_and_posts_once(self):
        """One picture, one post — and it fires on the first of them."""
        timeline = self.timeline() + [
            {'type': 'goal', 'player': 'Messi', 'team': 'Argentina',
             'minute': "88'"},
        ]
        staged = {ep.public_id(self.MATCH, 'GOAL', 'Messi')}
        got = ep.pending(timeline, staged, self.MATCH)
        assert len(got) == 1
        assert got[0]['minute'] == "23'"     # the earlier of the two

    @pytest.mark.parametrize('events', [None, 'Match has not started yet', {}, []])
    def test_a_timeline_that_is_not_a_list_is_survivable(self, events):
        """Before kickoff the scraper puts a sentence here, not a list."""
        assert ep.pending(events, {'anything'}, self.MATCH) == []

    def test_junk_entries_are_skipped_not_fatal(self):
        staged = {ep.public_id(self.MATCH, 'GOAL', 'Messi')}
        timeline = ['nonsense', None, *self.timeline()]
        assert len(ep.pending(timeline, staged, self.MATCH)) == 1


class TestNamesMatch:
    """The rule that decides, unattended, whether two spellings are one player.

    It runs against a live match with nobody watching, so its bias is fixed:
    a miss costs a photo that didn't post, a false positive costs the wrong
    player's photo on a public page with no undo. It is strict on purpose, and
    what it deliberately misses is caught by suggest() at staging time.
    """

    @pytest.mark.parametrize('staged,actual', [
        ('messi', 'messi'),                       # exact
        ('felix', 'joao-felix'),                  # typed the name people say
        ('messi', 'lionel-messi'),
        ('joao-felix', 'felix'),                  # feed shortened it instead
        ('cristian-romero', 'romero'),
    ])
    def test_one_name_inside_the_other_is_the_same_player(self, staged, actual):
        assert ep.names_match(staged, actual)

    @pytest.mark.parametrize('staged,actual', [
        ('rodri', 'rodrigo'),          # a different word, not a shorter name
        ('rodri', 'rodriguez'),        # …and this is why: same prefix, not him
        ('messi', 'alvarez'),
        ('n-gonzalez', 'nico-gonzalez'),   # initial vs given name
        ('', 'messi'),
        ('messi', ''),
    ])
    def test_anything_less_certain_is_left_to_a_human(self, staged, actual):
        assert not ep.names_match(staged, actual)

    def test_it_is_symmetric(self):
        """Which side typed the shorter name can't change the answer."""
        for a, b in [('felix', 'joao-felix'), ('rodri', 'rodrigo')]:
            assert ep.names_match(a, b) == ep.names_match(b, a)


class TestSuggest:
    SQUAD = ['Rodrigo', 'Rodri', 'Rodríguez', 'Joao Felix', 'N. Gonzalez',
             'Erling Haaland', 'Bernardo Silva', 'Thiago Silva']

    def test_the_exact_name_comes_first(self):
        assert ep.suggest('Rodri', self.SQUAD)[0] == 'Rodri'

    def test_a_prefix_finds_the_longer_name(self):
        """The Rodri/Rodrigo case, caught where a person can confirm it."""
        assert 'Rodrigo' in ep.suggest('Rodri', self.SQUAD)

    def test_a_surname_finds_the_full_name(self):
        assert ep.suggest('felix', self.SQUAD) == ['Joao Felix']

    def test_an_initial_is_not_an_obstacle(self):
        assert 'N. Gonzalez' in ep.suggest('Gonzalez', self.SQUAD)

    def test_a_typo_still_finds_it(self):
        assert 'Erling Haaland' in ep.suggest('Halland', self.SQUAD)

    def test_an_ambiguous_surname_offers_everyone_it_could_be(self):
        assert set(ep.suggest('Silva', self.SQUAD)) == {'Bernardo Silva',
                                                        'Thiago Silva'}

    def test_something_unrelated_suggests_nothing(self):
        assert ep.suggest('Zidane', self.SQUAD) == []

    @pytest.mark.parametrize('junk', ['', '   ', '???', None])
    def test_junk_suggests_nothing(self, junk):
        assert ep.suggest(junk, self.SQUAD) == []


class TestTolerantFiring:
    MATCH = '54483541'
    TIMELINE = [
        {'type': 'goal', 'player': 'Joao Felix', 'team': 'Chelsea',
         'minute': "23'"},
        {'type': 'goal', 'player': 'Rodrigo', 'team': 'City', 'minute': "55'"},
        {'type': 'yellow_card', 'player': 'Bernardo Silva', 'team': 'City',
         'minute': "60'"},
        {'type': 'yellow_card', 'player': 'Thiago Silva', 'team': 'Chelsea',
         'minute': "70'"},
    ]

    def fire(self, *staged):
        ids = {ep.public_id(self.MATCH, k, p) for k, p in staged}
        return ep.pending(self.TIMELINE, ids, self.MATCH)

    def test_a_surname_fires_the_full_name(self):
        """Staged before team news, when there was no list to pick from."""
        got = self.fire(('GOAL', 'Felix'))
        assert [m['player'] for m in got] == ['Joao Felix']
        assert got[0]['minute'] == "23'"

    def test_the_posted_key_follows_what_was_staged_not_the_feed(self):
        """The feed can revise a name mid-match; a key that moved would repost."""
        got = self.fire(('GOAL', 'Felix'))
        assert got[0]['posted_key'] == 'EVENT:GOAL:felix'

    def test_a_different_word_does_not_fire(self):
        """Rodri vs Rodrigo — silently wrong is what the FT report is for."""
        assert self.fire(('GOAL', 'Rodri')) == []

    def test_an_ambiguous_name_is_reported_not_guessed(self):
        got = self.fire(('YELLOW', 'Silva'))
        assert len(got) == 1
        assert got[0]['conflict'] == ['Bernardo Silva', 'Thiago Silva']

    def test_the_full_name_resolves_the_ambiguity(self):
        got = self.fire(('YELLOW', 'Thiago Silva'))
        assert [m['player'] for m in got] == ['Thiago Silva']
        assert 'conflict' not in got[0]

    def test_results_come_back_in_timeline_order(self):
        got = self.fire(('GOAL', 'Joao Felix'), ('YELLOW', 'Thiago Silva'))
        assert [m['minute'] for m in got] == ["23'", "70'"]


class TestUnfiredReport:
    MATCH = '54483541'
    TIMELINE = TestTolerantFiring.TIMELINE

    def report(self, staged, posted=()):
        ids = {ep.public_id(self.MATCH, k, p) for k, p in staged}
        return ep.unfired(self.TIMELINE, ids, self.MATCH, posted)

    def test_a_near_miss_names_who_the_feed_meant(self):
        """The whole point: turn a silent non-post into a sentence."""
        got = self.report([('GOAL', 'Rodri')])
        assert len(got) == 1
        assert got[0]['near'] == ['Rodrigo']

    def test_a_moment_that_never_happened_has_nothing_to_explain(self):
        got = self.report([('RED', 'Haaland')])
        assert got[0]['near'] == []

    def test_a_photo_that_posted_is_not_reported(self):
        got = self.report([('GOAL', 'Joao Felix')],
                          posted=['EVENT:GOAL:joao-felix'])
        assert got == []

    def test_it_only_looks_at_the_same_event(self):
        """Rodrigo scored; a red-card photo for 'Rodri' is not a near miss."""
        got = self.report([('RED', 'Rodri')])
        assert got[0]['near'] == []


class TestGoalMilestones:
    """A hat-trick is a count, not a timeline entry.

    Nothing in the feed says "hat-trick" — it says goal, goal, goal, and the
    third one is the moment. So these are derived by walking the timeline in
    order, which is the only place the count means anything.
    """

    MATCH = '54533571'

    def timeline(self, *goals):
        """goals: (player, minute, type) in the order the feed lists them."""
        return [{'type': t, 'player': p, 'team': 'Al-Nassr', 'minute': m}
                for p, m, t in goals]

    def keys(self, timeline):
        return [(m['event_key'], m['player'])
                for m in ep._timeline_moments(timeline, self.MATCH)]

    def test_a_third_goal_is_a_hat_trick(self):
        tl = self.timeline(('Ronaldo', "12'", 'goal'),
                           ('Ronaldo', "48'", 'goal'),
                           ('Ronaldo', "77'", 'goal'))
        assert ('HAT_TRICK', 'Ronaldo') in self.keys(tl)

    def test_a_second_goal_is_a_brace(self):
        tl = self.timeline(('Ronaldo', "12'", 'goal'),
                           ('Ronaldo', "48'", 'goal'))
        assert ('BRACE', 'Ronaldo') in self.keys(tl)
        assert ('HAT_TRICK', 'Ronaldo') not in self.keys(tl)

    def test_a_penalty_counts_towards_it(self):
        tl = self.timeline(('Ronaldo', "12'", 'goal'),
                           ('Ronaldo', "48'", 'penalty_goal'),
                           ('Ronaldo', "77'", 'goal'))
        assert ('HAT_TRICK', 'Ronaldo') in self.keys(tl)

    def test_an_own_goal_does_not(self):
        """Nobody has ever called two tap-ins and an own goal a hat-trick."""
        tl = self.timeline(('Ronaldo', "12'", 'goal'),
                           ('Ronaldo', "48'", 'own_goal'),
                           ('Ronaldo', "77'", 'goal'))
        assert ('HAT_TRICK', 'Ronaldo') not in self.keys(tl)
        assert ('BRACE', 'Ronaldo') in self.keys(tl)

    def test_goals_by_different_players_do_not_add_up(self):
        tl = self.timeline(('Ronaldo', "12'", 'goal'),
                           ('Mane', "48'", 'goal'),
                           ('Talisca', "77'", 'goal'))
        assert not [k for k, _p in self.keys(tl) if k in ('BRACE', 'HAT_TRICK')]

    def test_the_milestone_lands_on_the_goal_that_completed_it(self):
        tl = self.timeline(('Ronaldo', "12'", 'goal'),
                           ('Ronaldo', "48'", 'goal'),
                           ('Ronaldo', "77'", 'goal'))
        hat = next(m for m in ep._timeline_moments(tl, self.MATCH)
                   if m['event_key'] == 'HAT_TRICK')
        assert hat['minute'] == "77'"
        assert hat['goal_minutes'] == ["12'", "48'", "77'"]

    def test_the_goal_itself_still_happens_alongside(self):
        """A GOAL photo and a HAT_TRICK photo are both his, and both fire."""
        tl = self.timeline(('Ronaldo', "12'", 'goal'),
                           ('Ronaldo', "48'", 'goal'),
                           ('Ronaldo', "77'", 'goal'))
        keys = self.keys(tl)
        assert keys.count(('GOAL', 'Ronaldo')) == 3
        assert keys.count(('HAT_TRICK', 'Ronaldo')) == 1

    def test_a_fourth_goal_does_not_fire_it_again(self):
        tl = self.timeline(*[('Ronaldo', f"{m}'", 'goal')
                             for m in (12, 48, 77, 88)])
        assert self.keys(tl).count(('HAT_TRICK', 'Ronaldo')) == 1


class TestStagingAMilestone:
    MATCH = '54533571'
    TIMELINE = [
        {'type': 'goal', 'player': 'Ronaldo', 'team': 'Al-Nassr', 'minute': "12'"},
        {'type': 'goal', 'player': 'Mane', 'team': 'Al-Nassr', 'minute': "25'"},
        {'type': 'penalty_goal', 'player': 'Ronaldo', 'team': 'Al-Nassr',
         'minute': "48'"},
        {'type': 'goal', 'player': 'Ronaldo', 'team': 'Al-Nassr', 'minute': "77'"},
    ]

    def fire(self, *staged):
        ids = {ep.public_id(self.MATCH, k, p) for k, p in staged}
        return ep.pending(self.TIMELINE, ids, self.MATCH)

    def test_a_hat_trick_photo_posts_on_the_third_goal(self):
        got = self.fire(('HAT_TRICK', 'Ronaldo'))
        assert len(got) == 1
        assert got[0]['minute'] == "77'"
        assert got[0]['goal_minutes'] == ["12'", "48'", "77'"]

    def test_it_does_not_post_before_the_third(self):
        two_goals = self.TIMELINE[:3]
        ids = {ep.public_id(self.MATCH, 'HAT_TRICK', 'Ronaldo')}
        assert ep.pending(two_goals, ids, self.MATCH) == []

    def test_goal_brace_and_hat_trick_are_three_separate_photos(self):
        got = self.fire(('GOAL', 'Ronaldo'), ('BRACE', 'Ronaldo'),
                        ('HAT_TRICK', 'Ronaldo'))
        assert [(m['event_key'], m['minute']) for m in got] == [
            ('GOAL', "12'"), ('BRACE', "48'"), ('HAT_TRICK', "77'")]
        assert len({m['posted_key'] for m in got}) == 3

    def test_a_hat_trick_that_never_came_posts_nothing(self):
        got = self.fire(('HAT_TRICK', 'Mane'))
        assert got == []

    def test_a_name_mismatch_on_a_milestone_is_reported_at_full_time(self):
        staged = {ep.public_id(self.MATCH, 'HAT_TRICK', 'Renaldo')}
        report = ep.unfired(self.TIMELINE, staged, self.MATCH, posted_keys=[])
        assert report[0]['near'] == ['Ronaldo']


class TestAssists:
    """A goal entry names two people, and the second one is not a goal.

    The scraper folds an assist into the goal it produced rather than emitting
    it separately, so 🅰️ Assist is read off the same timeline entry as ⚽ Goal
    — the same shape as a paired substitution. What has to hold: both fire,
    they don't collide, and nobody is credited with an assist for an own goal.
    """

    MATCH = '54483541'
    TIMELINE = [
        {'type': 'goal', 'player': 'Cristian Romero', 'player_id': '50255850',
         'team': 'Argentina', 'minute': "23'",
         'assister': 'Messi', 'assister_id': '50000116'},
        {'type': 'goal', 'player': 'Messi', 'player_id': '50000116',
         'team': 'Argentina', 'minute': "41'",
         'assister': 'Gonzalo Montiel', 'assister_id': '50251053'},
    ]

    def fire(self, *staged):
        ids = {ep.public_id(self.MATCH, k, p) for k, p in staged}
        return ep.pending(self.TIMELINE, ids, self.MATCH)

    # ── Reading it off the entry ────────────────────────────────────────────

    def test_a_goal_offers_the_scorer_and_the_assister(self):
        ev = {'type': 'goal', 'player': 'Romero', 'player_id': '1',
              'assister': 'Messi', 'assister_id': '2'}
        assert ep.event_keys_for(ev) == [('GOAL', 'Romero', '1'),
                                         ('ASSIST', 'Messi', '2')]

    def test_a_penalty_can_carry_one_too(self):
        ev = {'type': 'penalty_goal', 'player': 'Kane', 'player_id': '1',
              'assister': 'Musiala', 'assister_id': '2'}
        assert ('ASSIST', 'Musiala', '2') in ep.event_keys_for(ev)

    def test_an_own_goal_never_does(self):
        """Nobody is credited with an assist for an own goal — and the scraper
        pairs goals with assists by position in the minute, not by asserting a
        link, so believing this one would put the wrong name on a public page."""
        ev = {'type': 'own_goal', 'player': 'Maguire', 'player_id': '1',
              'assister': 'Someone', 'assister_id': '2'}
        assert ep.event_keys_for(ev) == [('OG', 'Maguire', '1')]

    def test_a_goal_with_nobody_credited_offers_only_the_scorer(self):
        ev = {'type': 'goal', 'player': 'Haaland', 'player_id': '1'}
        assert ep.event_keys_for(ev) == [('GOAL', 'Haaland', '1')]

    @pytest.mark.parametrize('assister', ['N/A', '', None, '   '])
    def test_an_unnamed_assister_is_not_a_person(self, assister):
        ev = {'type': 'goal', 'player': 'Haaland', 'player_id': '1',
              'assister': assister}
        assert ep.event_keys_for(ev) == [('GOAL', 'Haaland', '1')]

    def test_an_orphaned_assist_entry_still_works(self):
        """The scraper emits a bare 'assist' when it couldn't pair one with a
        goal in the same minute. Then the assister is the entry's own player."""
        ev = {'type': 'assist', 'player': 'Messi', 'player_id': '2'}
        assert ep.event_keys_for(ev) == [('ASSIST', 'Messi', '2')]

    # ── Firing ──────────────────────────────────────────────────────────────

    def test_a_photo_staged_for_the_assister_posts(self):
        got = self.fire(('ASSIST', 'Messi'))
        assert [m['player'] for m in got] == ['Messi']
        assert got[0]['minute'] == "23'"

    def test_the_scorer_and_the_assister_both_post(self):
        """One entry, two people, two pictures, two posts. The regression this
        guards is the duplicate check reading them as one claim on one moment
        and silently dropping the second."""
        got = self.fire(('GOAL', 'Cristian Romero'), ('ASSIST', 'Messi'))
        assert [m['event_key'] for m in got] == ['GOAL', 'ASSIST']
        assert not any(m.get('duplicate') or m.get('conflict') for m in got)

    def test_scoring_is_not_assisting(self):
        """Messi scored in the 41st and assisted in the 23rd. A picture staged
        for one must not fire on the other."""
        got = self.fire(('ASSIST', 'Messi'))
        assert got[0]['minute'] == "23'"
        got = self.fire(('GOAL', 'Messi'))
        assert got[0]['minute'] == "41'"

    def test_it_fires_on_the_assisters_id_not_their_name(self):
        staged = {ep.public_id(self.MATCH, 'ASSIST', 'leo'):
                  {'player_id': '50000116'}}
        got = ep.pending(self.TIMELINE, staged, self.MATCH)
        assert [m['player'] for m in got] == ['Messi']

    def test_one_assist_per_picture_however_many_he_makes(self):
        timeline = self.TIMELINE + [
            {'type': 'goal', 'player': 'Enzo', 'player_id': '3',
             'team': 'Argentina', 'minute': "77'",
             'assister': 'Messi', 'assister_id': '50000116'}]
        got = ep.pending(timeline, {ep.public_id(self.MATCH, 'ASSIST', 'Messi')},
                         self.MATCH)
        assert len(got) == 1 and got[0]['minute'] == "23'"

    # ── What the caption is told ────────────────────────────────────────────

    def test_the_moment_names_who_finished_it(self):
        """The photo is of the player who made the pass; a caption that never
        says who scored is describing half of what happened."""
        got = self.fire(('ASSIST', 'Messi'))
        assert got[0]['scorer'] == 'Cristian Romero'

    def test_and_does_not_credit_him_with_assisting_himself(self):
        got = self.fire(('ASSIST', 'Messi'))
        assert not got[0]['assister']

    def test_a_goal_moment_is_unaffected(self):
        got = self.fire(('GOAL', 'Cristian Romero'))
        assert got[0]['assister'] == 'Messi'
        assert got[0]['scorer'] is None

    def test_an_orphaned_assist_knows_of_no_goal(self):
        got = ep.pending([{'type': 'assist', 'player': 'Messi',
                           'player_id': '2', 'minute': "23'"}],
                         {ep.public_id(self.MATCH, 'ASSIST', 'Messi')},
                         self.MATCH)
        assert got[0]['scorer'] is None

    def test_the_caption_has_words_for_it(self):
        assert 'assist' in ep.EVENT_DESCRIPTION['ASSIST']


class TestKeyboardLayout:
    def test_every_event_appears_exactly_once(self):
        """A key missing from the layout is a moment you can never stage for."""
        laid_out = [k for row in ep.EVENT_KEYBOARD_ROWS for k in row]
        assert sorted(laid_out) == sorted(ep.EVENT_KEYS)

    def test_every_laid_out_key_has_a_label(self):
        for row in ep.EVENT_KEYBOARD_ROWS:
            for key in row:
                assert ep.EVENT_LABELS[key]


class TestOneMomentOnePost:
    """The contract in both directions.

    A picture must not post twice, and a moment must not carry two posts. The
    first is the obvious one — a goal stays in the timeline for the rest of the
    match. The second is only reachable because names are matched tolerantly:
    "felix" and "joao-felix" are two files and one player.
    """

    MATCH = '54533571'

    def fire(self, timeline, staged):
        ids = {ep.public_id(self.MATCH, k, p) for k, p in staged}
        return ep.pending(timeline, ids, self.MATCH)

    def goals(self, player, *minutes, type='goal'):
        return [{'type': type, 'player': player, 'team': 'A', 'minute': m}
                for m in minutes]

    def test_scoring_twice_yields_one_entry(self):
        got = self.fire(self.goals('Ronaldo', "12'", "48'"), [('GOAL', 'Ronaldo')])
        assert len(got) == 1

    def test_it_fires_on_the_first_goal_not_the_second(self):
        got = self.fire(self.goals('Ronaldo', "12'", "48'"), [('GOAL', 'Ronaldo')])
        assert got[0]['minute'] == "12'"

    def test_a_second_booking_is_not_a_second_occasion(self):
        """Two yellows is a sending-off, not two chances for the same photo."""
        got = self.fire(self.goals('Casemiro', "30'", "70'", type='yellow_card'),
                        [('YELLOW', 'Casemiro')])
        assert len(got) == 1 and got[0]['minute'] == "30'"

    def test_scoring_five_times_still_yields_one_goal_entry(self):
        got = self.fire(self.goals('Haaland', "5'", "20'", "44'", "61'", "80'"),
                        [('GOAL', 'Haaland')])
        assert [m['event_key'] for m in got] == ['GOAL']

    def test_the_same_entry_comes_back_identical_every_poll(self):
        """The poll loop re-reads the timeline whole; the answer must not drift."""
        timeline = self.goals('Ronaldo', "12'", "48'")
        first = self.fire(timeline, [('GOAL', 'Ronaldo')])
        second = self.fire(timeline, [('GOAL', 'Ronaldo')])
        assert first == second

    # ── The other direction ─────────────────────────────────────────────────

    def test_two_spellings_of_one_player_do_not_both_post(self):
        got = self.fire(self.goals('Joao Felix', "34'"),
                        [('GOAL', 'Felix'), ('GOAL', 'Joao Felix')])
        postable = [m for m in got if not m.get('duplicate')]
        assert len(postable) == 1

    def test_the_exact_spelling_is_the_one_that_posts(self):
        got = self.fire(self.goals('Joao Felix', "34'"),
                        [('GOAL', 'Felix'), ('GOAL', 'Joao Felix')])
        winner = next(m for m in got if not m.get('duplicate'))
        assert winner['posted_key'] == 'EVENT:GOAL:joao-felix'

    def test_the_loser_names_what_superseded_it(self):
        got = self.fire(self.goals('Joao Felix', "34'"),
                        [('GOAL', 'Felix'), ('GOAL', 'Joao Felix')])
        loser = next(m for m in got if m.get('duplicate'))
        assert loser['duplicate'] == ['event_photos/54533571_GOAL_joao-felix']

    def test_with_nothing_to_choose_between_them_neither_posts(self):
        """Both partial spellings — picking one at random is not an option."""
        got = self.fire(self.goals('Joao Pedro Felix', "34'"),
                        [('GOAL', 'Felix'), ('GOAL', 'Joao Felix')])
        assert all(m.get('duplicate') for m in got)

    def test_two_pictures_for_two_different_moments_both_post(self):
        """The guard is about one moment, not about one player."""
        got = self.fire(self.goals('Ronaldo', "12'", "48'", "77'"),
                        [('GOAL', 'Ronaldo'), ('BRACE', 'Ronaldo'),
                         ('HAT_TRICK', 'Ronaldo')])
        assert not any(m.get('duplicate') for m in got)
        assert [m['minute'] for m in got] == ["12'", "48'", "77'"]
