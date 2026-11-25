"""
Everything /help and /info say — the bot's manual, in plain words.

/help (or /info) sends HELP_INTRO with a button per topic; tapping one swaps
the message for that topic, with a button back. Each topic is one Telegram
message, so it has to stay under 4,096 characters (a test checks).

HTML formatting (parse_mode='HTML'), not Markdown: names and usernames have
underscores, and one stray underscore makes Telegram reject a Markdown message.
Write &lt; for a literal "<".

Keep this in step with the code: when a feature changes, change its topic.
"""

HELP_INTRO = """\
👋 <b>Hi! I'm the 130 Yards bot.</b>

I make the Instagram posts for matches by myself — half-time and full-time scorecards, line-ups, carousels. <b>Your job is to send me good photos and answer my questions</b> when I ask something in the group.

<b>Quick cheat sheet</b>
📸 A photo that goes <i>under</i> a scorecard → /start
🎯 A photo for a player's moment (a goal, a red card…) → /event
🗂 See (or remove) the event photos waiting → /staged
🆕 Make a card yourself for a match I don't cover → /card
📦 See cards waiting to be posted together → /batch
📋 See every fixture and what will post → /list
✋ Stop whatever you're in the middle of → /cancel

<b>Tap a topic below</b> for the full step-by-step explanation. If you're not sure what something does, read it before you tap — a post on Instagram can't be taken back easily.

Everything I can do is also in the ☰ menu next to the message box."""


SCORECARD = """\
📸 <b>Scorecard photo — /start</b>

<b>What it is:</b> the background picture of the half-time (HT) or full-time (FT) scorecard. I draw the score, crests and scorers on top of it. One photo per match per moment (one for HT, one for FT).

<b>How to send it:</b>
1. Type /start (or tap it in the ☰ menu).
2. Tap the match.
3. Tap <b>Half Time</b> or <b>Full Time</b>.
4. Send the photo. Tip: send it as a <b>file / document</b> — Telegram makes normal photos blurry. It has to be under 20 MB.
5. I upload it and say "Done ✅". That's it — I use it when the card posts.

<b>Shortcut:</b> just send a photo with no command. I'll ask which match and HT or FT.

<b>If someone already sent one:</b> I show you that photo, who sent it and how long ago, and ask:
• 🔁 <b>Replace it</b> — yours replaces theirs
• ✋ <b>Keep it</b> — nothing changes (your new photo is thrown away)
• ➕ <b>Keep it and add carousel photos</b> (FT only) — keep their background, add your photo as an extra slide (see "FT extra photos")

<b>When to send it:</b> before the moment. I post the HT card at the 45th minute and the FT card at the 90th, without waiting for the whistle. I remind you in the group at 35', 80' and 110' if nobody has sent one yet.

<b>If nobody sends a photo:</b> the card still posts, on the standard template. No problem, just less nice.

<b>If the score changes late</b> after you sent the photo, I ask for a new one — see "During a match".

Every photo you send counts for you in the Monday Weekly Recap — re-uploads too."""


EXTRAS = """\
🖼 <b>FT extra photos (carousel)</b>

A full-time post can have <b>more than one picture</b>: the scorecard first, then your extra photos, then the stats page last (if the match has one).

<b>How to add them:</b>
1. Send the FT background as normal (/start → match → Full Time → photo).
2. Right after, I ask: "Add more photos to the post?"
3. Tap ➕ <b>Add a photo to the carousel</b> and send a photo. Repeat as many times as you want — up to <b>8 extras</b>.
4. Tap ✅ <b>Done</b> when you've finished.

Already a background there and you don't want to replace it? Pick ➕ <b>Keep it and add carousel photos</b> on my warning.

<b>Good to know:</b>
• The slides go in the order you sent them.
• Every slide is cropped to the same shape as the scorecard (tall, 3:4), so <b>keep the important part in the middle</b> — edges can be cut.
• 🗑 <b>Remove</b> deletes all the extras for that post, so you can start again.
• Half-time posts are always one picture — no extras for HT.
• Matches that are part of a carousel group (several matches in one post) don't get extras.
• If there are more photos than fit (Instagram allows 10 slides), only the first ones go up and I tell you."""


EVENT = """\
🎯 <b>Event photo — /event</b>

<b>What it is:</b> a photo you prepare <i>before</i> something happens — "Messi scores", "Haaland hat-trick", "Rodri red card". Nothing is drawn on it. <b>If that moment happens in the match, I post the photo by myself</b>, with its own caption, exactly as you sent it. If it never happens, nothing is posted and I delete the photo at full time.

<b>How to set one up:</b>
1. Type /event.
2. Tap the match.
3. Tap what has to happen (⚽ Goal, ⚽⚽ Brace, ⚽⚽⚽ Hat-trick, 🅰️ Assist, 🟡 Yellow, 🔴 Red…).
4. Tap the team, then the player. Before the line-ups are out (about an hour before kickoff) you type the name instead — <code>messi</code> finds Messi, accents and capitals don't matter.
5. Send the photo.
After each one you can add another player, switch team or change the event without starting again.

<b>Rules:</b>
• One photo per player per event. ⚽ Goal = their first goal; ⚽⚽ Brace = their second; ⚽⚽⚽ Hat-trick = their third.
• 🅰️ Assist posts for the player who set the goal up — so a goal can give two posts (scorer and assister).
• Every one that fires is a <b>separate Instagram post</b>. Don't stage rubbish.
• If a photo is already staged for that player and moment, I show it and ask Replace / Keep.
• If you typed a name I can't match to a player, I may ask in the group "who is this?" — tap the right player.

<b>See or remove them — /staged:</b> lists every photo waiting, with a ❌ on each. Tapping ❌ is the <b>only</b> safe way to remove one. Leftovers are cleared at full time anyway."""


MANUAL = """\
🆕 <b>Make a card yourself — /card</b>

For a match I don't follow automatically. You type the details, I make the card and show it to you. <b>Nothing posts until you tap Post.</b>

<b>The steps (I ask one question at a time):</b>
1. Home team, then away team. If I don't have the badge, I'll ask you to check the spelling or send one.
2. Half Time or Full Time.
3. The score, home first: <code>2-1</code>
4. The competition: <code>Premier League</code>
5. The date — <b>optional</b>. Type it like <code>21/08/2026</code> (day first), or tap <b>No date on the card</b> (or type <code>skip</code>).
6. Each team's scorers, one per line:
<code>23 Saka</code>  (a goal)
<code>45+2 Havertz (pen)</code>  (penalty)
<code>67 Rice (og)</code>  (own goal)
<code>88 Odegaard (red)</code>  (red card — "second yellow" or "2y" works too)
<code>90 Martinelli (miss)</code>  (missed penalty)
Nothing to add? Tap <b>Nothing to add</b>.
7. A background photo, or <b>Use the standard template</b>.
8. I show you the card with three buttons:
• 📤 <b>Post</b> — goes to Instagram now
• ➕ <b>Add another card</b> — keep this one and make another; they post together as one carousel
• 🗑 <b>Discard</b> — throw it away

If you post 2+ cards together, I ask for a <b>theme</b> (one line, like "Arsenal's last five") for the caption — or skip it.

<b>/batch</b> shows the cards waiting to go out together. /cancel stops the card you're making (saved cards stay)."""


MATCH = """\
⏱ <b>During a match — what I send and why</b>

<b>📋 Line-ups</b> (only for matches set to post them): about an hour before kickoff, once the line-ups <i>and the players' positions</i> are published. If the positions never come, I skip the post and tell you — nothing for you to do.

<b>📸 Photo reminders</b> at 35' (for HT), 80' (for FT) and 110' (extra time) — only if nobody has sent a photo yet.

<b>🕐 Half time</b>
• Matches set to post HT: the card posts at the 45th minute by itself.
• Matches <i>not</i> set to post HT: if the first half was special (3+ goals, a brace or hat-trick, a red card, a big upset, a comeback, a record), I ask "<b>Post the HT card?</b>" with the reason. Tap <b>📸 Post the HT card</b> → I ask for a photo (or tap <b>Use the standard template</b>) → it posts within a minute. Tap <b>No thanks</b> to skip. When the second half starts, the question closes.

<b>🔄 The score changed after your photo</b> (after 35' for HT, 80' for FT — an equaliser, a lead change, a comeback): your photo may now show the wrong story, so I ask:
• 📸 <b>Upload new photo</b> → send it within <b>10 minutes</b> and the card uses it (after that I use the old one)
• <b>Post the older one</b> → posts straight away
• No answer for <b>2 minutes</b> → I post with the old photo.
The card waits for your answer.

<b>⚡ Early posts get fixed:</b> I post HT at 45' and FT at 90' without waiting for the whistle. If a late goal changes the score, I delete the old card and post a corrected one. If I can't delete it, I send 🙋 with the link — then please delete it by hand in the app.

<b>🎵 After every post</b> I remind you to add music — Instagram won't let me do that, it has to be done in the app.

<b>If a post fails</b> I try again every minute by myself.

Small details: a red card for a second yellow shows as two yellow cards; in the second leg of a knockout tie the card shows the aggregate (AGG) under the score."""


CAROUSELS = """\
🎠 <b>Carousels — several matches in one post</b>

Some matches are grouped so their full-time scorecards go out together as one Instagram post (one slide per match), instead of filling the page with separate posts. I post the group once <b>every</b> match in it has finished.

<b>Before posting, I ask you the order:</b>
1. I send all the scorecards as a numbered album, so you can see them.
2. Right under it I ask which order the slides should go in, with a button per match.
3. Tap the match you want <b>first</b>, then the <b>second</b>, and so on. When one match is left, it goes last by itself and the post goes up.
4. Happy with the order shown? Tap ✅ <b>Post in this order</b>.
5. Changed your mind halfway? ↩️ <b>Start over</b>. Done picking the top ones? ✅ <b>Rest as listed</b>.

<b>If nobody taps anything for 1 minute</b>, I post: the matches you picked first, the rest in kickoff order. Every tap gives you another minute.

<b>If a match never finishes</b> (abandoned, no data), I wait 4 hours after the last kickoff and then post the group without it, and tell you.

If posting fails, I retry every minute by myself."""


NEWS = """\
📰 <b>Football news — post ideas</b>

I watch football news (BBC, The Guardian, Sky Sports, ESPN and Google News) for the <b>top 5 leagues only</b>: Premier League, La Liga, Serie A, Bundesliga, Ligue 1.

<b>Everything I send is a post idea</b> — something worth making a post about if you want. I don't post news to Instagram myself.

<b>🚨 Breaking news</b> — within 15 minutes: confirmed transfers ("here we go", official signings), managers sacked or appointed, serious injuries to big players, bans, records, big off-field stories.

<b>📰 Regular news</b> — every 3 hours, the 5 best stories since the last round-up: rumours, quotes, press conferences, smaller injuries.
🔥 = trending (lots of outlets talking about it)
🔄 = an update to a story I already sent (I say what's new)

<b>What I never send:</b>
• Match results, scores, highlights or player ratings — we already post a scorecard for those.
• The same story twice — only again if there's real new information.

<b>No news during match hours:</b> from the first kickoff of the day to the end of the last match, I stay quiet so match alerts don't get buried. I catch up right after.

Tap <b>Read on …</b> to open the article."""


RECAP = """\
🏆 <b>Weekly Recap — every Monday</b>

Every <b>Monday at 09:00 German time</b> (13:30 in India in winter) I post last week's leaderboard in the group (Monday to Sunday).

<b>📸 Photos sent through the bot</b>
Everyone, ranked by how many photos they sent: HT and FT backgrounds, event photos and carousel extras. <b>Re-uploads count too</b> (like replacing a photo after the score changed) and are shown separately.

<b>📈 Posts your photos went into</b>
For every post I made, I remember whose photos were in it, and add up how those posts did: number of posts, likes and comments (reach and saves will appear too once the Instagram account allows it).

<b>Everyone is listed</b> — and you get tagged (your @username), so you'll get a notification.
Sent nothing? You're at the bottom as <b>🫵 LOSER</b>. Simple fix: send photos.

<b>What counts:</b> only photos sent <b>through this bot</b> and posts <b>the bot made</b>. Photos sent somewhere else, or posts made directly in the Instagram app, don't count."""


ALERTS = """\
🚦 <b>What my messages mean</b>

🙋 <b>A person is needed.</b> Read it and do what it says — e.g. delete a post by hand, check a team name.
❌ <b>Something failed.</b> Usually I'm already retrying by myself; it says if you need to do anything.
⚠️ <b>Heads-up.</b> Normally nothing to do — e.g. a badge is missing, no stats for a small match.
📸 <b>Photo reminder</b> — send the photo with /start if you have a good one.
🔄 <b>The score changed after your photo</b> — upload a new one or post the older one.
🎵 <b>Add the music</b> to the post that just went up (in the Instagram app).
🎠 <b>Pick the carousel order</b> — 1 minute to answer.
🚨 / 📰 <b>News</b> — post ideas, nothing to do unless you want to make a post.
🏆 <b>Weekly Recap</b> — Monday leaderboard.

<b>Buttons:</b> when I ask something, the answers are buttons on my message. Tap one — don't type an answer unless I ask you to type.

<b>Typing:</b> text only means something when I asked for it (a player's name, details for /card). Otherwise I ignore it and say so.

<b>Stuck?</b> /cancel stops whatever you're doing. Then start again."""


COMMANDS = """\
📋 <b>All commands</b>

/start — send the background photo for a half-time or full-time card
/newphoto — same as /start
/event — prepare a photo for a player's moment (goal, assist, red card…)
/staged — see the event photos waiting, and remove one (❌)
/card — make a match card yourself, from details you type
/batch — cards waiting to be posted together
/list — every fixture, with kickoff times and what will post
/cancel — stop whatever you're in the middle of
/help or /info — this manual

You can also just <b>send a photo</b> with no command: it becomes a scorecard background, and I ask which match and HT or FT.

Commands work in a private chat with me. The group is where I send alerts, questions, news and the Weekly Recap."""


# (key, button label, text) — the order of the buttons under /help.
HELP_TOPICS = [
    ('scorecard', '📸 Scorecard photo', SCORECARD),
    ('extras', '🖼 FT extra photos', EXTRAS),
    ('event', '🎯 Event photo', EVENT),
    ('manual', '🆕 Make a card', MANUAL),
    ('match', '⏱ During a match', MATCH),
    ('carousels', '🎠 Carousels', CAROUSELS),
    ('news', '📰 News', NEWS),
    ('recap', '🏆 Weekly Recap', RECAP),
    ('alerts', '🚦 What messages mean', ALERTS),
    ('commands', '📋 All commands', COMMANDS),
]

TOPIC_TEXT = {key: text for key, _label, text in HELP_TOPICS}

# The whole manual in one string — for searching and tests, never sent as one
# message (it is far over Telegram's limit).
HELP_TEXT = '\n\n'.join([HELP_INTRO] + [text for _k, _l, text in HELP_TOPICS])
