"""
/staged: what is armed right now, and taking one back down.

A staged photo is invisible — a Cloudinary name nobody sees, doing nothing
for hours, then posting on its own in public. This command is the only place
that answers "what did I arm?", and its ❌ is the only safe abort: the
alternative is deleting an object in the Cloudinary console by hand, where
nothing checks which match the name belongs to.

So what is pinned here is the part that would fail silently: that a tap
removes the picture it was drawn for and no other, and that every way the
listing can fail leaves everything armed.
"""

import asyncio
import types

import pytest

import event_photos as ep
import telegram_bot as tb


MATCHES = [
    {'match_id': '111', 'home_team': 'Argentina', 'away_team': 'Brazil'},
    {'match_id': '222', 'home_team': 'Hull City', 'away_team': 'Man Utd'},
]


def pid(match_id, key, player):
    return ep.public_id(match_id, key, player)


# ── The Cloudinary side ──────────────────────────────────────────────────────

class TestMatchIdOf:
    def test_reads_the_fixture_back_out(self):
        assert ep.match_id_of('event_photos/54483541_GOAL_messi') == '54483541'

    def test_an_underscored_key_does_not_confuse_it(self):
        assert ep.match_id_of('event_photos/99_SUB_IN_messi') == '99'

    def test_something_from_another_folder_is_not_one(self):
        assert ep.match_id_of('scorecards/99_HT') == ''


class TestStagedAll:
    @pytest.fixture
    def listing(self, monkeypatch):
        """Install a canned folder listing; record the prefixes asked for."""
        def install(contents):
            asked = []

            def fake_list(prefix):
                asked.append(prefix)
                return dict(contents)

            monkeypatch.setattr(ep, '_list', fake_list)
            return asked
        return install

    def test_sorts_the_folder_into_fixtures(self, listing):
        listing({
            pid('111', 'GOAL', 'Messi'): {},
            pid('222', 'RED', 'Casemiro'): {},
        })
        found = ep.staged_all(['111', '222'])
        assert set(found['111']) == {pid('111', 'GOAL', 'Messi')}
        assert set(found['222']) == {pid('222', 'RED', 'Casemiro')}

    def test_a_fixture_with_nothing_armed_is_present_and_empty(self, listing):
        """"Nothing staged" and "not asked about" are different answers."""
        listing({pid('111', 'GOAL', 'Messi'): {}})
        assert ep.staged_all(['111', '222'])['222'] == {}

    def test_a_leftover_from_an_unlisted_match_is_ignored(self, listing):
        listing({pid('999', 'GOAL', 'Someone'): {}})
        assert ep.staged_all(['111']) == {'111': {}}

    def test_ten_fixtures_cost_one_listing(self, listing):
        """The point of listing the folder rather than each prefix: this is
        answered for a person who just typed /staged, on a shared quota."""
        asked = listing({})
        ep.staged_all([str(i) for i in range(10)])
        assert len(asked) == 1

    def test_the_context_travels_with_the_picture(self, listing):
        listing({pid('111', 'GOAL', 'Messi'): {'player_id': '50000116'}})
        found = ep.staged_all(['111'])
        assert found['111'][pid('111', 'GOAL', 'Messi')]['player_id'] == '50000116'


class TestFindByDigest:
    def test_a_digest_finds_the_picture_it_was_made_from(self):
        staged = {pid('111', 'GOAL', 'Messi'): {},
                  pid('111', 'RED', 'Neymar'): {}}
        wanted = pid('111', 'RED', 'Neymar')
        assert ep.find_by_digest(staged, ep.digest(wanted)) == wanted

    def test_an_unknown_digest_finds_nothing(self):
        assert ep.find_by_digest({pid('111', 'GOAL', 'Messi'): {}}, 'deadbeef') is None

    def test_a_bare_set_is_accepted_too(self):
        wanted = pid('111', 'GOAL', 'Messi')
        assert ep.find_by_digest({wanted}, ep.digest(wanted)) == wanted

    def test_the_button_fits_telegrams_budget(self):
        """callback_data is capped at 64 bytes and a long name would blow a
        public_id past it — which is the whole reason the tap carries a
        digest instead."""
        longest = pid('54483541', 'HAT_TRICK',
                      'Alejandro Dario Gomez de la Fuente Rodriguez')
        assert len(f"sd:{ep.digest(longest)}".encode()) <= 64


class TestDescribe:
    def test_uses_the_feeds_spelling_once_it_is_known(self):
        assert (ep.describe(pid('111', 'GOAL', 'felix'), {'player': 'Joao Felix'})
                == 'Joao Felix — ⚽ Goal')

    def test_falls_back_to_what_was_typed(self):
        """Before anything pins it, the slug *is* the string that has to
        match — so seeing it is the point, not a shortcoming."""
        assert ep.describe(pid('111', 'GOAL', 'rodri')) == 'rodri — ⚽ Goal'

    def test_something_that_is_not_staged_describes_to_nothing(self):
        assert ep.describe('scorecards/111_HT') == ''


class TestDeleteOne:
    def test_removes_exactly_the_one_named(self, monkeypatch):
        asked = []
        monkeypatch.setattr(ep.cloudinary.api, 'delete_resources',
                            lambda ids: asked.append(ids) or
                            {'deleted': {ids[0]: 'deleted'}})
        assert ep.delete_one('event_photos/111_GOAL_messi') is True
        assert asked == [['event_photos/111_GOAL_messi']]

    def test_says_so_when_there_was_nothing_there(self, monkeypatch):
        monkeypatch.setattr(ep.cloudinary.api, 'delete_resources',
                            lambda ids: {'deleted': {ids[0]: 'not_found'}})
        assert ep.delete_one('event_photos/111_GOAL_messi') is False


# ── The bot side ─────────────────────────────────────────────────────────────

class Msg:
    """A Telegram message that records what was said back to it."""

    def __init__(self, sent):
        self.sent = sent

    async def reply_text(self, text, **kw):
        self.sent.append({'text': text, **kw})
        return Msg(self.sent)


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


def update(sent, user_id=7, data=None):
    up = types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=user_id),
        message=Msg(sent),
        callback_query=Query(data, sent) if data else None,
    )
    return up


def ctx():
    return types.SimpleNamespace(chat_data={'menu_shown': True}, user_data={})


@pytest.fixture
def bot(monkeypatch):
    """The bot with matches.json and Cloudinary replaced.

    Returns the staged map; mutate it and the next call sees the change,
    which is how "already gone" is exercised.
    """
    monkeypatch.delenv('TELEGRAM_ALLOWED_USER_IDS', raising=False)
    monkeypatch.setattr(tb, '_load_matches', lambda: MATCHES)

    staged = {'111': {}, '222': {}}
    monkeypatch.setattr(ep, 'staged_all',
                        lambda ids: {m: dict(v) for m, v in staged.items()})

    deleted = []

    def fake_delete(public_id):
        deleted.append(public_id)
        for per_match in staged.values():
            per_match.pop(public_id, None)
        return True

    monkeypatch.setattr(ep, 'delete_one', fake_delete)
    return types.SimpleNamespace(staged=staged, deleted=deleted)


def run_staged(sent, context=None):
    asyncio.run(tb.staged_cmd(update(sent), context or ctx()))


def buttons(sent):
    return [b
            for msg in sent
            for row in (msg.get('reply_markup').inline_keyboard
                        if msg.get('reply_markup') else [])
            for b in row]


class TestListing:
    def test_nothing_armed_says_so_and_offers_no_buttons(self, bot):
        sent = []
        run_staged(sent)
        assert 'Nothing is staged' in sent[0]['text']
        assert buttons(sent) == []

    def test_one_message_per_fixture_with_something_armed(self, bot):
        bot.staged['111'][pid('111', 'GOAL', 'Messi')] = {}
        bot.staged['222'][pid('222', 'RED', 'Casemiro')] = {}
        sent = []
        run_staged(sent)
        assert len(sent) == 2
        assert 'Argentina vs Brazil' in sent[0]['text']
        assert 'Hull City vs Man Utd' in sent[1]['text']

    def test_a_fixture_with_nothing_armed_is_not_mentioned(self, bot):
        bot.staged['111'][pid('111', 'GOAL', 'Messi')] = {}
        sent = []
        run_staged(sent)
        assert len(sent) == 1
        assert 'Hull City' not in sent[0]['text']

    def test_every_armed_photo_gets_its_own_button(self, bot):
        bot.staged['111'].update({
            pid('111', 'GOAL', 'Messi'): {},
            pid('111', 'RED', 'Neymar'): {},
        })
        sent = []
        run_staged(sent)
        assert len(buttons(sent)) == 2
        assert all(b.callback_data.startswith('sd:') for b in buttons(sent))

    def test_a_name_matched_photo_is_flagged(self, bot):
        """It fires only if the scoreboard spells it that way — the one thing
        worth knowing before kickoff, while it can still be fixed."""
        bot.staged['111'][pid('111', 'GOAL', 'rodri')] = {}
        sent = []
        run_staged(sent)
        assert '(by name)' in sent[0]['text']

    def test_a_pinned_photo_is_not(self, bot):
        bot.staged['111'][pid('111', 'GOAL', 'rodrigo')] = {
            'player_id': '50000116', 'player': 'Rodrigo'}
        sent = []
        run_staged(sent)
        assert '(by name)' not in sent[0]['text']
        assert 'Rodrigo — ⚽ Goal' in sent[0]['text']

    def test_a_cloudinary_outage_says_nothing_changed(self, bot, monkeypatch):
        def boom(ids):
            raise RuntimeError('connection reset')

        monkeypatch.setattr(ep, 'staged_all', boom)
        sent = []
        run_staged(sent)
        assert 'Nothing has changed' in sent[0]['text']
        assert bot.deleted == []

    def test_an_unauthorized_user_is_told_nothing(self, bot, monkeypatch):
        monkeypatch.setenv('TELEGRAM_ALLOWED_USER_IDS', '999')
        sent = []
        run_staged(sent)
        assert sent[0]['text'] == 'Not authorized.'


class TestDisarming:
    def _tap(self, bot, public_id, sent):
        up = update(sent, data=f"sd:{ep.digest(public_id)}")
        asyncio.run(tb.staged_drop(up, ctx()))
        return up.callback_query

    def test_a_tap_removes_that_picture(self, bot):
        wanted = pid('111', 'GOAL', 'Messi')
        bot.staged['111'][wanted] = {}
        self._tap(bot, wanted, [])
        assert bot.deleted == [wanted]

    def test_and_no_other(self, bot):
        """The failure this guards is the one with no symptom: a tap resolved
        by position against a list that has since changed takes down a photo
        nobody asked about, and nothing anywhere says so."""
        keep = pid('111', 'RED', 'Neymar')
        bot.staged['111'].update({pid('111', 'GOAL', 'Messi'): {}, keep: {}})
        self._tap(bot, pid('111', 'GOAL', 'Messi'), [])
        assert keep in bot.staged['111']

    def test_the_listing_is_redrawn_without_it(self, bot):
        gone = pid('111', 'GOAL', 'Messi')
        bot.staged['111'].update({gone: {}, pid('111', 'RED', 'Neymar'): {}})
        query = self._tap(bot, gone, [])
        text = query.edits[-1]['text']
        assert 'Removed messi — ⚽ Goal' in text
        assert 'neymar' in text

    def test_the_last_one_leaves_an_empty_fixture(self, bot):
        only = pid('111', 'GOAL', 'Messi')
        bot.staged['111'][only] = {}
        query = self._tap(bot, only, [])
        assert 'Nothing is staged for Argentina vs Brazil' in query.edits[-1]['text']
        assert 'reply_markup' not in query.edits[-1]

    def test_tapping_something_already_gone_deletes_nothing(self, bot):
        """Two /staged listings open, or a restart in between. The stale one's
        buttons must not resolve to whatever is there now."""
        query = self._tap(bot, pid('111', 'GOAL', 'Messi'), [])
        assert bot.deleted == []
        assert "isn't staged any more" in query.edits[-1]['text']

    def test_a_cloudinary_outage_removes_nothing(self, bot, monkeypatch):
        wanted = pid('111', 'GOAL', 'Messi')
        bot.staged['111'][wanted] = {}

        def boom(ids):
            raise RuntimeError('connection reset')

        monkeypatch.setattr(ep, 'staged_all', boom)
        sent = []
        self._tap(bot, wanted, sent)
        assert bot.deleted == []
        assert 'Nothing was removed' in sent[-1]['text']

    def test_an_unauthorized_tap_costs_nothing(self, bot, monkeypatch):
        """Refused before the listing, not after: a tap from outside the
        allowlist must not spend Cloudinary quota either."""
        monkeypatch.setenv('TELEGRAM_ALLOWED_USER_IDS', '999')
        wanted = pid('111', 'GOAL', 'Messi')
        bot.staged['111'][wanted] = {}
        query = self._tap(bot, wanted, [])
        assert bot.deleted == []
        assert query.edits == []

    def test_a_failed_delete_says_it_is_still_armed(self, bot, monkeypatch):
        wanted = pid('111', 'GOAL', 'Messi')
        bot.staged['111'][wanted] = {}

        def boom(public_id):
            raise RuntimeError('403')

        monkeypatch.setattr(ep, 'delete_one', boom)
        sent = []
        self._tap(bot, wanted, sent)
        assert 'still staged' in sent[-1]['text']


class TestItIsReachable:
    def test_the_command_is_advertised(self):
        assert 'staged' in tb.KNOWN_COMMANDS

    def test_and_explained(self):
        assert '/staged' in tb.HELP_TEXT
