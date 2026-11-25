"""
/help and /info: an intro with a button per topic, each topic in full.

Every topic is one Telegram message, so it must fit in 4,096 characters and
use only the HTML Telegram accepts — a bad tag and Telegram refuses the whole
message, which for a manual means nobody can read it.
"""

import asyncio
import re
import types
from html.parser import HTMLParser

import pytest

import help_text as ht
import telegram_bot as tb

ALLOWED_TAGS = {'b', 'i', 'u', 's', 'code', 'pre', 'a', 'blockquote'}
ALL_TEXTS = [('intro', ht.HELP_INTRO)] + [(k, t) for k, _l, t in ht.HELP_TOPICS]


class _Checker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.bad = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.bad.append(tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.bad.append(f'/{tag}')


@pytest.mark.parametrize('key,text', ALL_TEXTS)
def test_each_message_fits_and_is_valid_telegram_html(key, text):
    assert len(text) <= 4096, f'{key} is {len(text)} characters'
    checker = _Checker()
    checker.feed(text)
    assert not checker.bad and not checker.stack, f'{key}: {checker.bad or checker.stack}'
    # A bare "<" that isn't a tag would make Telegram reject the message.
    assert not re.search(r'<(?!/?(b|i|u|s|code|pre|a|blockquote)\b)', text), key


@pytest.mark.parametrize('phrase', [
    # every feature people now meet in the bot
    'Replace it', 'Keep it and add carousel photos',            # overwrite warning
    'up to <b>8 extras</b>', 'stats page last',                   # FT extras
    'Upload new photo', 'Post the older one', '2 minutes', '10 minutes',  # score changed
    'Post the HT card', 'Use the standard template',              # HT on request
    'Post in this order', '1 minute',                             # carousel order
    'Breaking news', 'every 3 hours', 'No news during match hours',  # news
    'Monday at 09:00 German time', 'LOSER', 'Re-uploads count too',  # weekly recap
    'No date on the card',                                        # optional date
    'positions', 'aggregate', 'two yellow cards',                 # line-ups, cards
    '/info',
])
def test_the_manual_covers_it(phrase):
    assert phrase in ht.HELP_TEXT


def test_every_command_in_the_menu_is_explained():
    commands = ht.TOPIC_TEXT['commands']
    for c in tb.BOT_COMMANDS:
        assert f'/{c.command}' in commands, c.command


def test_buttons_are_short_and_unique():
    keys = [k for k, _l, _t in ht.HELP_TOPICS]
    assert len(keys) == len(set(keys))
    for row in tb._help_keyboard().inline_keyboard:
        assert len(row) <= 2
        for b in row:
            assert len(b.callback_data.encode()) <= 64


def run(coro):
    return asyncio.run(coro)


class Query:
    def __init__(self, data):
        self.data = data
        self.edits = []

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))


def test_a_topic_button_shows_the_topic_with_a_way_back():
    q = Query('help:recap')
    run(tb.help_topic(types.SimpleNamespace(callback_query=q), None))
    text, kw = q.edits[0]
    assert text == ht.TOPIC_TEXT['recap'] and kw['parse_mode'] == 'HTML'
    assert kw['reply_markup'].inline_keyboard[0][0].callback_data == 'help:menu'


def test_back_shows_the_intro_and_all_topics():
    q = Query('help:menu')
    run(tb.help_topic(types.SimpleNamespace(callback_query=q), None))
    text, kw = q.edits[0]
    assert text == ht.HELP_INTRO
    labels = [b.text for row in kw['reply_markup'].inline_keyboard for b in row]
    assert labels == [label for _k, label, _t in ht.HELP_TOPICS]


def test_help_and_info_send_the_intro(monkeypatch):
    sent = []

    class Msg:
        async def reply_text(self, text, **kw):
            sent.append((text, kw))

    async def no_keyboard(*a):
        pass

    monkeypatch.setattr(tb, '_allowed', lambda u: True)
    monkeypatch.setattr(tb, '_clear_keyboard', no_keyboard)
    run(tb.help_cmd(types.SimpleNamespace(message=Msg()), types.SimpleNamespace()))
    text, kw = sent[0]
    assert text == ht.HELP_INTRO and kw['parse_mode'] == 'HTML'
    src = open(tb.__file__, encoding='utf-8').read()
    assert src.count("CommandHandler('info', help_cmd)") == 2      # in flows and out
    assert "CallbackQueryHandler(help_topic, pattern=r'^help:')" in src
