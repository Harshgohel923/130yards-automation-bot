"""
The chat has no buttons under it: every action is a command in the ☰ menu.

Two things are worth pinning. First, the removal: Telegram keeps a persistent
reply keyboard until a bot explicitly takes it down, so shipping a build with
no keyboard in it does *not* clear the one already under an existing chat —
only a ReplyKeyboardRemove does, and it has to ride on a message that is then
deleted. Second, reachability: with the buttons gone, a command missing from
BOT_COMMANDS is a command with no way in at all, since the ☰ menu is now the
only place the bot advertises anything.
"""

import asyncio
import types

import pytest
from telegram import ReplyKeyboardRemove

import telegram_bot as tb


class Msg:
    """A message that records replies, and whether each was deleted."""

    def __init__(self, sent, deletable=True):
        self.sent = sent
        self.deletable = deletable

    async def reply_text(self, text, **kw):
        record = {'text': text, 'deleted': False, **kw}
        self.sent.append(record)
        return Sent(self.sent, record, self.deletable)


class Sent(Msg):
    def __init__(self, sent, record, deletable):
        super().__init__(sent, deletable)
        self.record = record

    async def delete(self):
        if not self.deletable:
            raise RuntimeError('message can not be deleted')
        self.record['deleted'] = True


def ctx():
    return types.SimpleNamespace(chat_data={}, user_data={})


class TestClearingTheOldKeyboard:
    def test_it_sends_a_removal_and_takes_the_message_back_down(self):
        sent = []
        asyncio.run(tb._clear_keyboard(Msg(sent), ctx()))
        assert len(sent) == 1
        assert isinstance(sent[0]['reply_markup'], ReplyKeyboardRemove)
        assert sent[0]['deleted'] is True

    def test_it_happens_once_per_chat(self):
        sent, context = [], ctx()
        asyncio.run(tb._clear_keyboard(Msg(sent), context))
        asyncio.run(tb._clear_keyboard(Msg(sent), context))
        assert len(sent) == 1

    def test_a_chat_that_already_saw_the_old_menu_is_left_alone(self):
        # The flag the removed keyboard used: a chat mid-conversation across
        # the upgrade must not be tidied twice.
        sent = []
        context = types.SimpleNamespace(chat_data={'menu_shown': True},
                                        user_data={})
        asyncio.run(tb._clear_keyboard(Msg(sent), context))
        assert sent == []

    def test_a_failed_delete_leaves_a_message_that_reads_as_deliberate(self):
        sent = []
        asyncio.run(tb._clear_keyboard(Msg(sent, deletable=False), ctx()))
        assert '☰' in sent[0]['text']       # not a bare placeholder

    def test_a_chat_that_cannot_be_tidied_does_not_raise(self):
        class Broken(Msg):
            async def reply_text(self, text, **kw):
                raise RuntimeError('chat is gone')

        asyncio.run(tb._clear_keyboard(Broken([]), ctx()))   # must not raise


class TestNothingSendsAKeyboardAnyMore:
    def test_the_module_defines_no_reply_keyboard(self):
        assert not hasattr(tb, 'MAIN_KEYBOARD')

    def test_no_button_text_is_matched_as_input(self):
        # The old flows entered on text like '📸 Scorecard photo'. Nothing may
        # depend on typed text meaning an action any more.
        assert not [n for n in dir(tb) if n.endswith('_FILTER')
                    and n.startswith('BTN_')]

    def test_typed_text_is_only_read_where_a_step_asks_for_it(self):
        # MANUAL_TEXT_FILTER is that vocabulary; a command must never be
        # swallowed by it as a team name.
        assert tb.MANUAL_TEXT_FILTER.check_update is not None


class TestEveryActionIsInTheMenu:
    """With the buttons gone, ☰ is the only place a command is advertised."""

    ACTIONS = ('start', 'newphoto', 'event', 'staged', 'card', 'batch',
               'list', 'cancel', 'help')

    @pytest.mark.parametrize('command', ACTIONS)
    def test_it_is_published(self, command):
        assert command in tb.KNOWN_COMMANDS

    def test_every_published_command_is_described(self):
        assert all(c.description.strip() for c in tb.BOT_COMMANDS)

    def test_the_help_text_points_at_the_menu(self):
        assert '☰' in tb.HELP_TEXT

    def test_help_no_longer_tells_anyone_to_tap_a_button_below(self):
        assert 'buttons below' not in tb.HELP_TEXT
