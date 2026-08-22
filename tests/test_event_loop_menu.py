"""
Staging several pictures without answering the same four questions each time.

/event used to end at the photo, so a second player for the same team meant
match → event → team → player all over again. A goal keyboard for one side is
usually three or four players and none of those answers change between them.

So the flow now returns to a *step*: the same squad, the other squad, or a
different event for the same match. What is pinned here is which step each
button goes back to, and — the part that would corrupt a picture rather than
merely annoy — that nothing belonging to the photo just staged survives the
loop. A `pending` photo left behind would be uploaded a second time, against
the next player chosen.

The match is deliberately never re-asked: /event is still how that changes.
"""

import asyncio
import types

import pytest

import telegram_bot as tb


MATCH = {'match_id': '54465223',
         'home_team': 'Borussia Dortmund', 'away_team': 'Bayern Munich'}

SQUADS = {'home': ['Bensebaini', 'Fábio Silva'],
          'away': ['Nathaniel Brown', 'Olise', 'Harry Kane']}


class Msg:
    def __init__(self, sent):
        self.sent = sent
        self.retired = False

    async def reply_text(self, text, **kw):
        self.sent.append({'text': text, **kw})
        sent_msg = Msg(self.sent)
        self.sent[-1]['msg'] = sent_msg
        return sent_msg

    async def edit_reply_markup(self, reply_markup=None, **kw):
        self.retired = True


class Query:
    def __init__(self, data, sent):
        self.data = data
        self.sent = sent
        self.message = Msg(sent)
        self.edits = []

    async def answer(self, text=None, **kw):
        self.answered = text

    async def edit_message_text(self, text, **kw):
        self.edits.append({'text': text, **kw})
        self.sent.append({'text': text, **kw})


def staged_state(side='away', squad=SQUADS, **extra):
    """user_data as it stands the moment a picture has just been staged."""
    data = {'match': MATCH, 'event_key': 'GOAL', 'player': 'Olise',
            'player_id': '50397003', 'squad': squad}
    if side:
        data['side'] = side
        data['squad_side'] = list(squad[side])
    data.update(extra)
    return types.SimpleNamespace(chat_data={'menu_shown': True}, user_data=data)


def labels(sent):
    """Button captions from the last thing the bot said."""
    markup = sent[-1].get('reply_markup')
    return [b.text for row in (markup.inline_keyboard if markup else []) for b in row]


def datas(sent):
    markup = sent[-1].get('reply_markup')
    return [b.callback_data
            for row in (markup.inline_keyboard if markup else []) for b in row]


def menu(context):
    sent = []
    state = asyncio.run(tb._staged_menu(Msg(sent), context))
    return state, sent


def tap(data, context):
    sent = []
    up = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=7),
                               message=Msg(sent), callback_query=Query(data, sent))
    state = asyncio.run(tb.event_more_chosen(up, context))
    return state, sent


# ── What the menu offers ──────────────────────────────────────────────────────

class TestMenu:
    def test_it_waits_on_the_menu_rather_than_ending(self):
        state, _ = menu(staged_state())
        assert state == tb.E_MORE

    def test_both_squads_are_offered_by_name(self):
        _, sent = menu(staged_state(side='away'))
        assert '➕ Another Bayern Munich player' in labels(sent)
        assert '🔁 Borussia Dortmund instead' in labels(sent)

    def test_the_other_side_is_relative_to_the_one_in_hand(self):
        _, sent = menu(staged_state(side='home'))
        assert '➕ Another Borussia Dortmund player' in labels(sent)
        assert '🔁 Bayern Munich instead' in labels(sent)

    def test_the_event_can_be_changed_and_the_flow_ended(self):
        _, sent = menu(staged_state())
        assert 'ev:more:event' in datas(sent)
        assert 'ev:more:done' in datas(sent)

    def test_the_match_is_never_offered_again(self):
        _, sent = menu(staged_state())
        assert not any(d.startswith('ev:match') for d in datas(sent))

    def test_a_typed_name_gets_the_team_question_not_a_squad_list(self):
        """No side was chosen, so there is no list to reopen — but a squad exists."""
        _, sent = menu(staged_state(side=None))
        assert '➕ Another player' in labels(sent)
        assert not any(t.startswith('🔁') for t in labels(sent))

    def test_with_no_team_news_only_event_and_done_are_offered(self):
        _, sent = menu(staged_state(side=None, squad=None))
        assert datas(sent) == ['ev:more:event', 'ev:more:done']

    def test_a_side_with_no_squad_behind_it_is_not_offered(self):
        _, sent = menu(staged_state(side='away', squad={'away': [], 'home': ['X']}))
        assert not any(t.startswith('➕ Another Bayern') for t in labels(sent))
        assert '🔁 Borussia Dortmund instead' in labels(sent)


# ── Where each button goes back to ────────────────────────────────────────────

class TestWhereTheButtonsGo:
    def test_another_player_reopens_the_same_squad(self):
        ctx = staged_state(side='away')
        state, sent = tap('ev:more:player', ctx)
        assert state == tb.E_PLAYER
        assert 'Bayern Munich' in sent[-1]['text']
        assert 'Olise' in labels(sent)

    def test_other_team_opens_the_other_squad(self):
        ctx = staged_state(side='away')
        state, sent = tap('ev:more:team', ctx)
        assert state == tb.E_PLAYER
        assert ctx.user_data['side'] == 'home'
        assert 'Bensebaini' in labels(sent)

    def test_different_event_reasks_the_event_question(self):
        ctx = staged_state()
        state, sent = tap('ev:more:event', ctx)
        assert state == tb.E_EVENT
        assert 'ev:evt:GOAL' in datas(sent)
        assert 'ev:evt:YELLOW' in datas(sent)

    def test_different_event_forgets_the_old_event(self):
        """Left in place it would stage the next picture against the old moment."""
        ctx = staged_state()
        tap('ev:more:event', ctx)
        assert 'event_key' not in ctx.user_data

    def test_done_ends_the_conversation(self):
        state, _ = tap('ev:more:done', staged_state())
        assert state == tb.ConversationHandler.END

    def test_cancel_ends_the_conversation(self):
        state, _ = tap('cancel', staged_state())
        assert state == tb.ConversationHandler.END

    def test_other_team_without_a_side_asks_which_team(self):
        ctx = staged_state(side=None)
        state, sent = tap('ev:more:team', ctx)
        assert state == tb.E_TEAM
        assert 'ev:side:home' in datas(sent)
        assert 'ev:side:away' in datas(sent)

    def test_another_player_without_a_side_falls_back_to_typing(self):
        ctx = staged_state(side=None)
        state, _ = tap('ev:more:player', ctx)
        assert state == tb.E_TYPE_PLAYER


# ── Nothing from the last picture survives the loop ───────────────────────────

class TestTheLoopIsClean:
    @pytest.mark.parametrize('choice', ['player', 'team', 'event'])
    def test_the_staged_player_is_forgotten(self, choice):
        ctx = staged_state()
        tap(f'ev:more:{choice}', ctx)
        assert 'player' not in ctx.user_data
        assert 'player_id' not in ctx.user_data

    @pytest.mark.parametrize('choice', ['player', 'team', 'event'])
    def test_a_photo_sent_at_the_menu_survives_the_loop(self, choice):
        """
        The menu says a photo sent there is held for the next player, and
        _player_settled uploads it the moment a name is settled. Clearing it
        here would drop a picture the bot has already acknowledged.
        """
        ctx = staged_state(pending=('file-123', None))
        tap(f'ev:more:{choice}', ctx)
        assert ctx.user_data['pending'] == ('file-123', None)

    @pytest.mark.parametrize('choice', ['player', 'team', 'event'])
    def test_the_match_is_kept(self, choice):
        ctx = staged_state()
        tap(f'ev:more:{choice}', ctx)
        assert ctx.user_data['match'] == MATCH

    def test_the_squad_is_kept_so_no_second_fetch_is_needed(self):
        ctx = staged_state()
        tap('ev:more:player', ctx)
        assert ctx.user_data['squad'] == SQUADS

    def test_state_lost_under_the_menu_ends_rather_than_guessing(self):
        ctx = types.SimpleNamespace(chat_data={}, user_data={})
        state, sent = tap('ev:more:player', ctx)
        assert state == tb.ConversationHandler.END
        assert '/event' in sent[-1]['text']


# ── The step that records the side ────────────────────────────────────────────

def test_choosing_a_squad_records_which_side_it_was():
    """The menu can only reopen 'the same team' if the side was written down."""
    ctx = staged_state(side=None)
    sent = []
    state = asyncio.run(tb._ask_player_list(Msg(sent), ctx, 'away'))
    assert state == tb.E_PLAYER
    assert ctx.user_data['side'] == 'away'
    assert ctx.user_data['squad_side'] == SQUADS['away']


# ── The held photo belongs to exactly one name ────────────────────────────────
# Two halves of one rule, and the loop needs both: _player_settled consumes a
# held photo, so the loop must not clear it (a photo sent at the menu is for the
# next player) and the consumer must (or it is uploaded again for every player
# chosen after it).

class TestTheHeldPhoto:
    @pytest.fixture
    def settled(self, monkeypatch):
        uploads = []

        async def fake_upload(msg, context, ref):
            uploads.append(ref)
            return True
        monkeypatch.setattr(tb, '_upload_event_photo', fake_upload)

        def run(context, player='Kane'):
            sent = []
            state = asyncio.run(tb._player_settled(Msg(sent), context, player))
            return state, sent
        run.uploads = uploads
        return run

    def test_it_is_uploaded_for_the_name_just_settled(self, settled):
        ctx = staged_state(pending=('file-123', None))
        settled(ctx)
        assert settled.uploads == [('file-123', None)]

    def test_it_is_consumed_not_left_for_the_next_player(self, settled):
        ctx = staged_state(pending=('file-123', None))
        settled(ctx)
        assert 'pending' not in ctx.user_data

    def test_the_next_player_gets_no_photo_of_their_own_accord(self, settled):
        """The whole point: one photo, one player, even inside a long session."""
        ctx = staged_state(pending=('file-123', None))
        settled(ctx, player='Kane')
        settled(ctx, player='Olise')
        assert settled.uploads == [('file-123', None)]

    def test_with_nothing_held_it_asks_for_the_photo(self, settled):
        ctx = staged_state()
        state, sent = settled(ctx)
        assert state == tb.E_PHOTO
        assert 'send the photo' in sent[0]['text'].lower()


# ── A typed name belongs to no squad ─────────────────────────────────────────

class TestTypedNameClearsTheSide:
    def test_typing_forgets_which_squad_was_open(self):
        ctx = staged_state(side='away')
        asyncio.run(tb._ask_typed_player(Msg([]), ctx))
        assert 'side' not in ctx.user_data

    def test_so_the_menu_stops_offering_the_wrong_team(self):
        """Bayern was open, the typed player is Dortmund's — offer neither."""
        ctx = staged_state(side='away')
        asyncio.run(tb._ask_typed_player(Msg([]), ctx))
        ctx.user_data['player'] = 'A Dortmund sub'
        _, sent = menu(ctx)
        assert not any(t.startswith('➕ Another Bayern') for t in labels(sent))
        assert '➕ Another player' in labels(sent)


# ── Team news that arrives mid-conversation ──────────────────────────────────

class TestSquadMissIsNotCachedForever:
    def test_a_cached_miss_is_dropped_so_the_next_step_looks_again(self):
        """
        The conversation now outlives the team news it was told didn't exist.
        Before the loop every photo was a fresh /event and a fresh fetch.
        """
        ctx = staged_state(side=None, squad=None)
        tap('ev:more:event', ctx)
        assert 'squad' not in ctx.user_data

    def test_a_real_squad_is_kept(self):
        ctx = staged_state()
        tap('ev:more:player', ctx)
        assert ctx.user_data['squad'] == SQUADS


# ── Only the newest menu has live buttons ────────────────────────────────────

class TestStaleMenus:
    def test_the_previous_menu_is_retired(self):
        ctx = staged_state()
        _, first = menu(ctx)
        menu(ctx)
        assert first[-1]['msg'].retired is True

    def test_the_newest_menu_is_left_alone(self):
        ctx = staged_state()
        menu(ctx)
        _, second = menu(ctx)
        assert second[-1]['msg'].retired is False

    def test_a_menu_that_cannot_be_retired_does_not_break_the_next_one(self):
        class Stubborn(Msg):
            async def edit_reply_markup(self, **kw):
                raise RuntimeError("message is too old to edit")

        ctx = staged_state()
        ctx.user_data['menu_msg'] = Stubborn([])
        state, sent = menu(ctx)
        assert state == tb.E_MORE
        assert 'ev:more:done' in datas(sent)

    def test_a_tapped_menu_is_not_retired_afterwards(self):
        """It has already become the next question — stripping it would take
        the player keyboard away."""
        ctx = staged_state()
        menu(ctx)
        tap('ev:more:player', ctx)
        assert 'menu_msg' not in ctx.user_data


# ── One picture per player per moment ────────────────────────────────────────

class TestReplacingAPicture:
    @pytest.fixture
    def upload(self, monkeypatch):
        async def fake(msg, context, match, event_key, ref, **kw):
            return True
        monkeypatch.setattr(tb, '_upload_photo', fake)

        def run(context):
            sent = []
            ok = asyncio.run(tb._upload_event_photo(Msg(sent), context, ('f', None)))
            return ok, sent
        return run

    def test_the_first_one_says_nothing_extra(self, upload):
        ok, sent = upload(staged_state())
        assert ok is True
        assert not any('replaced' in m['text'] for m in sent)

    def test_the_same_player_and_moment_twice_is_called_out(self, upload):
        ctx = staged_state()
        upload(ctx)
        ok, sent = upload(ctx)
        assert ok is True
        assert any('replaced' in m['text'] for m in sent)

    def test_a_different_player_is_not_a_replacement(self, upload):
        ctx = staged_state()
        upload(ctx)
        ctx.user_data['player'] = 'Harry Kane'
        _, sent = upload(ctx)
        assert not any('replaced' in m['text'] for m in sent)

    def test_the_same_player_at_a_different_moment_is_not_a_replacement(self, upload):
        ctx = staged_state()
        upload(ctx)
        ctx.user_data['event_key'] = 'YELLOW'
        _, sent = upload(ctx)
        assert not any('replaced' in m['text'] for m in sent)


# ── Ids, when both squads answer to the same surname ─────────────────────────

class TestPlayerIdForSide:
    IDS = {'home': {'Silva': 'dortmund-silva'},
           'away': {'Silva': 'bayern-silva', 'Olise': 'olise-1'}}

    def ctx(self, side):
        return types.SimpleNamespace(chat_data={}, user_data={
            'squad_ids': self.IDS, **({'side': side} if side else {})})

    def test_the_chosen_side_settles_a_shared_surname(self):
        assert tb._player_id_for(self.ctx('home'), 'Silva') == 'dortmund-silva'
        assert tb._player_id_for(self.ctx('away'), 'Silva') == 'bayern-silva'

    def test_a_typed_name_only_one_squad_answers_to_is_pinned(self):
        assert tb._player_id_for(self.ctx(None), 'Olise') == 'olise-1'

    def test_a_typed_name_both_squads_answer_to_is_left_unpinned(self):
        """Unpinned means the worker settles it on spelling — a wrong id
        would be wrong for good."""
        assert tb._player_id_for(self.ctx(None), 'Silva') == ''

    def test_an_unknown_name_has_no_id(self):
        assert tb._player_id_for(self.ctx('home'), 'Nobody') == ''

    def test_no_squad_at_all_has_no_id(self):
        blank = types.SimpleNamespace(chat_data={}, user_data={})
        assert tb._player_id_for(blank, 'Olise') == ''
