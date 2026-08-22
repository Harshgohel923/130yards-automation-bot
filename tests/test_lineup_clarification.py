"""
Turning a typed name into the feed's own id, once the team sheets are out.

This is the second and last chance to fix the failure the whole feature is
most likely to have: a photo staged as "Rodri" against a feed that prints
"Rodrigo" simply never fires, and until full time nothing anywhere says so.
The first chance is suggest(), in the bot — and it isn't available for a
photo staged before the team news, which is most of them.

Two things are pinned here. That a name beyond doubt is settled *silently*:
an alert for every correctly typed name would train you to ignore the ones
that matter. And that anything short of beyond doubt is asked rather than
guessed — the wrong player on a public page has no undo.
"""

import asyncio
import types

import pytest

import event_photos as ep
import main
import telegram_bot as tb
import telegram_notify


MATCH_ID = '54483541'
ENTRY = {
    'match_id':   MATCH_ID,
    'home_team':  'Real Madrid',
    'away_team':  'Barcelona',
    'scraper_url': 'https://example.invalid/match/54483541',
}

# Two Silvas on purpose — the ambiguity this must refuse to resolve — plus a
# name people shorten (Joao Felix) and one they say differently (Rodrigo).
HOME = [('Rodrigo', '11'), ('Thiago Silva', '12'), ('Bernardo Silva', '13')]
AWAY = [('Joao Felix', '21'), ('Lionel Messi', '22')]


def sheets(home=HOME, away=AWAY, bench=True):
    """A scrape whose team sheets name these people."""
    def block(people):
        # The bench half is what benches_named() waits for; splitting the
        # list is enough to exercise both groups being read.
        starters, subs = people[:-1], people[-1:]
        return {
            'lineups': [{'person': n, 'person_id': i} for n, i in starters],
            'sub': [{'person': n, 'person_id': i} for n, i in subs] if bench else [],
        }
    return {'matchFormation': {'team_A': block(home), 'team_B': block(away)}}


def staged(*specs):
    """{public_id: context} for (event_key, typed name[, context]) triples."""
    out = {}
    for spec in specs:
        key, name = spec[0], spec[1]
        out[ep.public_id(MATCH_ID, key, name)] = spec[2] if len(spec) > 2 else {}
    return out


# ── Reading the sheets ───────────────────────────────────────────────────────

class TestSquad:
    def test_names_everyone_on_both_sheets(self):
        found = dict(ep.squad(sheets()))
        assert found['Rodrigo'] == '11'
        assert found['Lionel Messi'] == '22'

    def test_the_bench_counts_too(self):
        """A photo staged for ⬆️ Subbed on is about somebody on it."""
        assert ('Bernardo Silva', '13') in ep.squad(sheets())

    def test_nothing_published_names_nobody(self):
        assert ep.squad({}) == []
        assert ep.squad({'matchFormation': 'not yet'}) == []


class TestBenchesNamed:
    def test_both_benches_published(self):
        assert ep.benches_named(sheets()) is True

    def test_an_xi_on_its_own_is_not_enough(self):
        """The sheet fills in stages, and asking about a name before the
        substitutes are listed reports half the squad as missing."""
        assert ep.benches_named(sheets(bench=False)) is False

    def test_nothing_published_at_all(self):
        assert ep.benches_named({}) is False


# ── Who each staged picture is of ────────────────────────────────────────────

class TestClarify:
    def run(self, staged_map, squad=None):
        return ep.clarify(staged_map, MATCH_ID,
                          HOME + AWAY if squad is None else squad)

    def test_an_exact_name_is_settled_without_asking(self):
        [item] = self.run(staged(('GOAL', 'Rodrigo')))
        assert item['match'] == ('Rodrigo', '11')
        assert item['options'] == []

    def test_a_surname_inside_the_full_name_is_settled_too(self):
        """names_match() would have fired this unattended anyway — settling it
        here only makes it exact, and costs nobody a tap."""
        [item] = self.run(staged(('GOAL', 'felix')))
        assert item['match'] == ('Joao Felix', '21')

    def test_a_different_word_is_a_question(self):
        [item] = self.run(staged(('GOAL', 'rodri')))
        assert item['match'] is None
        assert item['options'] == [('Rodrigo', '11')]

    def test_a_name_two_players_share_is_a_question(self):
        """Refusing here is the point: picking one puts the wrong player on a
        public page, and there is no undo."""
        [item] = self.run(staged(('GOAL', 'silva')))
        assert item['match'] is None
        assert {n for n, _i in item['options']} == {'Thiago Silva', 'Bernardo Silva'}

    def test_a_name_nobody_resembles_has_nothing_to_offer(self):
        [item] = self.run(staged(('GOAL', 'haaland')))
        assert item['match'] is None
        assert item['options'] == []

    def test_a_picture_already_pinned_is_left_alone(self):
        assert self.run(staged(('GOAL', 'rodri', {'player_id': '11'}))) == []

    def test_and_so_is_one_already_answered(self):
        """"Leave it as I typed it" looks exactly like an unanswered question
        unless it is recorded — this is what stops the hourly re-ask."""
        assert self.run(staged(('GOAL', 'rodri', {'clarified': 'as-typed'}))) == []

    def test_a_squad_member_with_no_id_can_settle_nothing(self):
        assert self.run(staged(('GOAL', 'ghost')),
                        squad=[('Ghost', '')]) == []

    def test_no_sheets_means_no_opinion(self):
        assert self.run(staged(('GOAL', 'rodri')), squad=[]) == []

    def test_each_picture_is_judged_on_its_own(self):
        items = self.run(staged(('GOAL', 'Rodrigo'), ('RED', 'rodri')))
        assert [i['event_key'] for i in items] == ['GOAL', 'RED']
        assert items[0]['match'] and items[1]['match'] is None

    def test_it_carries_the_name_as_it_was_typed(self):
        """What the question has to quote back — the string that has to match."""
        [item] = self.run(staged(('GOAL', 'rodri')))
        assert item['staged'] == 'rodri'


# ── What the worker does with the answer ─────────────────────────────────────

@pytest.fixture
def worker(monkeypatch):
    """main's poll-time clarification, with Cloudinary and Telegram replaced."""
    main.MATCH_STATE.clear()
    main._EVENT_PHOTO_CACHE.clear()

    sent = {'alerts': [], 'choices': []}
    pinned = []

    monkeypatch.setattr(main, 'send_alert',
                        lambda text, **kw: sent['alerts'].append(text))
    monkeypatch.setattr(main, 'send_choice',
                        lambda text, options, **kw:
                        sent['choices'].append((text, options)))
    monkeypatch.setattr(ep, 'set_player_id',
                        lambda pid, ident, player='':
                        pinned.append((pid, ident, player)))

    box = types.SimpleNamespace(sent=sent, pinned=pinned, staged={})
    monkeypatch.setattr(ep, 'staged', lambda match_id: dict(box.staged))
    return box


def clarify(worker, staged_map, data=None):
    worker.staged = staged_map
    main._clarify_staged_photos(ENTRY, data if data is not None else sheets())


class TestWorkerPass:
    def test_a_settled_name_is_pinned_and_nothing_is_said(self, worker):
        clarify(worker, staged(('GOAL', 'felix')))
        assert worker.pinned == [
            (ep.public_id(MATCH_ID, 'GOAL', 'felix'), '21', 'Joao Felix')]
        assert worker.sent['alerts'] == [] and worker.sent['choices'] == []

    def test_an_unclear_name_is_asked_about_and_not_pinned(self, worker):
        clarify(worker, staged(('GOAL', 'rodri')))
        assert worker.pinned == []
        [(text, options)] = worker.sent['choices']
        assert 'rodri' in text
        assert options[0][0] == 'Rodrigo'

    def test_the_question_always_offers_a_way_out(self, worker):
        """Leaving it alone must be a tap, not a thing you have to know."""
        clarify(worker, staged(('GOAL', 'rodri')))
        [(_text, options)] = worker.sent['choices']
        assert options[-1][1].endswith(':-')
        assert 'rodri' in options[-1][0]

    def test_every_button_fits_telegrams_callback_budget(self, worker):
        clarify(worker, staged(('GOAL', 'silva')))
        [(_text, options)] = worker.sent['choices']
        assert all(len(data.encode()) <= 64 for _label, data in options)

    def test_the_button_names_the_picture_the_bot_will_find(self, worker):
        """The two processes share nothing but the picture — so the handle in
        the button has to be computable from it on both sides."""
        public_id = ep.public_id(MATCH_ID, 'GOAL', 'rodri')
        clarify(worker, staged(('GOAL', 'rodri')))
        [(_text, options)] = worker.sent['choices']
        assert ep.find_by_digest({public_id: {}},
                                 options[0][1].split(':')[1]) == public_id

    def test_a_name_nobody_has_is_reported_while_it_can_be_fixed(self, worker):
        clarify(worker, staged(('GOAL', 'haaland')))
        assert worker.sent['choices'] == []
        assert 'haaland' in worker.sent['alerts'][0]

    def test_the_offer_is_capped(self, worker):
        """Past a handful this stops being a choice and becomes a squad sheet."""
        crowd = [(f'Silva {i}', str(i)) for i in range(20)]
        clarify(worker, staged(('GOAL', 'silva')), data=sheets(crowd, crowd))
        [(_text, options)] = worker.sent['choices']
        assert len(options) == main.MAX_CLARIFY_OPTIONS + 1   # + "leave it"

    def test_nothing_happens_before_the_benches_are_published(self, worker):
        clarify(worker, staged(('GOAL', 'rodri')), data=sheets(bench=False))
        assert worker.sent['choices'] == [] and worker.pinned == []

    def test_nothing_happens_with_no_team_news_at_all(self, worker):
        clarify(worker, staged(('GOAL', 'rodri')), data={})
        assert worker.sent['choices'] == [] and worker.pinned == []

    def test_nothing_staged_asks_nothing(self, worker):
        clarify(worker, {})
        assert worker.sent['choices'] == [] and worker.sent['alerts'] == []

    def test_a_failed_pin_is_survivable(self, worker, monkeypatch):
        """The photo still matches by name, exactly as it did before."""
        def boom(*a, **kw):
            raise RuntimeError('cloudinary 500')

        monkeypatch.setattr(ep, 'set_player_id', boom)
        clarify(worker, staged(('GOAL', 'felix')))     # must not raise

    def test_a_pin_is_not_rewritten_on_the_next_poll(self, worker):
        """The staged listing is cached for minutes; without carrying the pin
        into it, every poll in that window writes the same context again."""
        clarify(worker, staged(('GOAL', 'felix')))
        main._clarify_staged_photos(ENTRY, sheets())
        assert len(worker.pinned) == 1

    def test_one_unclear_name_does_not_hold_up_the_rest(self, worker):
        clarify(worker, staged(('GOAL', 'rodri'), ('RED', 'felix')))
        assert len(worker.pinned) == 1
        assert len(worker.sent['choices']) == 1


# ── The tap, back in the bot ─────────────────────────────────────────────────

class Msg:
    def __init__(self, sent):
        self.sent = sent

    async def reply_text(self, text, **kw):
        self.sent.append({'text': text, **kw})
        return Msg(self.sent)


class Query:
    def __init__(self, data, sent):
        self.data = data
        self.message = Msg(sent)
        self.edits = []

    async def answer(self, text=None, **kw):
        self.answered = text

    async def edit_message_text(self, text, **kw):
        self.edits.append({'text': text, **kw})


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.delenv('TELEGRAM_ALLOWED_USER_IDS', raising=False)
    monkeypatch.setattr(tb, '_load_matches', lambda: [dict(ENTRY)])

    box = types.SimpleNamespace(staged={}, pinned=[], settled=[])
    monkeypatch.setattr(ep, 'staged_all',
                        lambda ids: {MATCH_ID: dict(box.staged)})
    monkeypatch.setattr(ep, 'set_player_id',
                        lambda pid, ident, player='':
                        box.pinned.append((pid, ident, player)))
    monkeypatch.setattr(ep, 'set_clarified',
                        lambda pid, answer='as-typed': box.settled.append(pid))
    monkeypatch.setattr(tb, 'get_match_data', lambda url: sheets())
    return box


def tap(data, sent=None):
    up = types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=7),
        message=None,
        callback_query=Query(data, sent if sent is not None else []),
    )
    asyncio.run(tb.staged_clarify(up, types.SimpleNamespace(
        chat_data={'menu_shown': True}, user_data={})))
    return up.callback_query


class TestTheTap:
    def test_choosing_a_player_pins_the_picture_to_them(self, bot):
        public_id = ep.public_id(MATCH_ID, 'GOAL', 'rodri')
        bot.staged[public_id] = {}
        query = tap(f"ec:{ep.digest(public_id)}:11")
        assert bot.pinned == [(public_id, '11', 'Rodrigo')]
        assert 'Rodrigo' in query.edits[-1]['text']

    def test_leaving_it_pins_nothing_and_stops_the_asking(self, bot):
        public_id = ep.public_id(MATCH_ID, 'GOAL', 'rodri')
        bot.staged[public_id] = {}
        query = tap(f"ec:{ep.digest(public_id)}:-")
        assert bot.pinned == []
        assert bot.settled == [public_id]
        assert 'Left as staged' in query.edits[-1]['text']

    def test_a_tap_on_a_photo_that_has_gone_pins_nothing(self, bot):
        """The match finished, or /staged took it down, while the question sat
        unanswered in the chat."""
        query = tap(f"ec:{ep.digest('event_photos/1_GOAL_x')}:11")
        assert bot.pinned == []
        assert "isn't staged any more" in query.edits[-1]['text']

    def test_a_name_lookup_failure_still_pins_the_id(self, bot, monkeypatch):
        """The id is what makes it fire; the name is only what the reply
        says back."""
        def boom(url):
            raise RuntimeError('scraper down')

        monkeypatch.setattr(tb, 'get_match_data', boom)
        public_id = ep.public_id(MATCH_ID, 'GOAL', 'rodri')
        bot.staged[public_id] = {}
        tap(f"ec:{ep.digest(public_id)}:11")
        assert bot.pinned == [(public_id, '11', '')]

    def test_a_failed_pin_says_the_photo_is_untouched(self, bot, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError('cloudinary 500')

        monkeypatch.setattr(ep, 'set_player_id', boom)
        public_id = ep.public_id(MATCH_ID, 'GOAL', 'rodri')
        bot.staged[public_id] = {}
        sent = []
        tap(f"ec:{ep.digest(public_id)}:11", sent)
        assert 'still staged' in sent[-1]['text']

    def test_the_worker_and_the_bot_agree_on_the_button(self, worker, bot):
        """The one contract between two processes that never import each
        other: what main puts in a button, telegram_bot has to be able to
        act on."""
        public_id = ep.public_id(MATCH_ID, 'GOAL', 'rodri')
        clarify(worker, {public_id: {}})
        [(_text, options)] = worker.sent['choices']
        bot.staged[public_id] = {}
        tap(options[0][1])
        assert bot.pinned == [(public_id, '11', 'Rodrigo')]


# ── The transport ────────────────────────────────────────────────────────────

class TestSendChoice:
    @pytest.fixture
    def posted(self, monkeypatch):
        calls = []

        class Res:
            ok = True

        monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'token')
        monkeypatch.setenv('TELEGRAM_ALERT_CHAT_ID', '42')
        monkeypatch.setattr(telegram_notify.requests, 'post',
                            lambda url, data=None, timeout=None:
                            calls.append(data) or Res())
        telegram_notify._last_sent.clear()
        return calls

    def test_the_answers_go_out_as_one_button_per_row(self, posted):
        """Two names side by side on a phone truncate to two prefixes."""
        telegram_notify.send_choice('who?', [('Rodrigo', 'ec:a:1'),
                                             ('Rodriguez', 'ec:a:2')])
        keyboard = posted[0]['reply_markup']
        assert '"callback_data": "ec:a:1"' in keyboard
        assert keyboard.count('[{') == 2

    def test_a_plain_alert_carries_no_keyboard(self, posted):
        telegram_notify.send_alert('just saying')
        assert 'reply_markup' not in posted[0]

    def test_the_same_question_is_not_asked_twice_inside_the_cooldown(self, posted):
        for _ in range(3):
            telegram_notify.send_choice('who?', [('A', 'ec:a:1')],
                                        key='k', cooldown=600)
        assert len(posted) == 1

    def test_an_unconfigured_bot_drops_it_rather_than_raising(self, monkeypatch):
        monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
        telegram_notify.send_choice('who?', [('A', 'ec:a:1')])   # must not raise
