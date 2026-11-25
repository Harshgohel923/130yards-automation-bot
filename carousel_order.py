"""
Choosing the slide order of a carousel group, over Telegram.

Before a group posts, the dispatcher sends the matches in their default order
(kickoff time) with a button per match. Tapping matches one by one builds the
order — first tap is slide 1 — or one tap keeps the order as listed. The
dispatcher waits while taps keep coming and posts once nobody has tapped for
IDLE_SECS: whatever was picked goes first, everything else follows as listed.

Two processes share this and nothing else: the dispatcher asks and waits, the
Telegram bot handles the taps. So the state lives on Cloudinary, one small JSON
per question, and every button carries everything needed to act on the tap:

    co:<question id>:<picks>[=]

<picks> is the default-order index of each match picked so far, one digit per
slide (a group is capped at 10 matches, so 0-9 always fits); a trailing '='
means "done — the rest as listed". Well inside Telegram's 64-byte cap.

The state, at carousel/order/<question id>.json:

    {group, match_ids, labels,      the question — default order
     picks, final, order, source,   the answer so far / the decision
     asked_at, updated_at, message_id}
"""

import hashlib
import io
import json
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

PREFIX = 'co'
FOLDER = 'carousel/order'
IDLE_SECS = 60            # post once nobody has tapped for this long
MAX_WAIT_SECS = 10 * 60   # however busy the tapping, never hold a post longer
POLL_SECS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec='seconds')


def question_id(group_root: str) -> str:
    """Short stable id for a group's question — both sides can compute it."""
    return hashlib.sha1(group_root.encode()).hexdigest()[:8]


# ── Buttons ──────────────────────────────────────────────────────────────────

def callback(qid: str, picks: list[int], done: bool = False) -> str:
    return f"{PREFIX}:{qid}:{''.join(str(i) for i in picks)}{'=' if done else ''}"


def parse_callback(data: str) -> tuple[str, list[int], bool] | None:
    """'co:ab12cd34:20=' → ('ab12cd34', [2, 0], True). None if malformed."""
    parts = str(data or '').split(':')
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1]:
        return None
    raw = parts[2]
    done = raw.endswith('=')
    raw = raw.rstrip('=')
    if not raw.isdigit() and raw != '':
        return None
    picks = [int(c) for c in raw]
    if len(set(picks)) != len(picks):
        return None
    return parts[1], picks, done


def apply_order(match_ids: list[str], picks: list[int]) -> list[str]:
    """Picked matches first, in tap order; the rest in the default order."""
    chosen = [match_ids[i] for i in picks if 0 <= i < len(match_ids)]
    return chosen + [m for m in match_ids if m not in chosen]


def is_complete(n: int, picks: list[int], done: bool) -> bool:
    """Nothing left to choose: finished explicitly, or one match left over."""
    return done or len(picks) >= n - 1


def keyboard(qid: str, labels: list[str], picks: list[int]) -> list[tuple[str, str]]:
    """The buttons for the next tap: each match not yet placed, then the exits."""
    slide = len(picks) + 1
    options = [(f"{slide}. {label}", callback(qid, picks + [i]))
               for i, label in enumerate(labels) if i not in picks]
    if picks:
        options.append(("✅ Rest as listed", callback(qid, picks, done=True)))
        options.append(("↩️ Start over", callback(qid, [])))
    else:
        options.append(("✅ Post in this order", callback(qid, [], done=True)))
    return options


def numbered(labels: list[str]) -> str:
    return '\n'.join(f"{i}. {label}" for i, label in enumerate(labels, 1))


def question_text(group: str, labels: list[str], picks: list[int]) -> str:
    """The message, at any point in the picking."""
    if not picks:
        return (f"🎠 The '{group}' carousel is ready — {len(labels)} matches. "
                f"Which order should the slides go in?\n\n"
                f"As it stands (the scorecards are just above, numbered the "
                f"same way):\n{numbered(labels)}\n\n"
                f"Tap the match you want as slide 1, then slide 2, and so on — "
                f"or keep this order. No tap for a minute and it posts as "
                f"listed.")
    so_far = numbered([labels[i] for i in picks])
    rest = [labels[i] for i in range(len(labels)) if i not in picks]
    return (f"🎠 The '{group}' carousel — picking the order.\n\n"
            f"Slides so far:\n{so_far}\n\n"
            f"Still to place: {', '.join(rest)}\n\n"
            f"Tap slide {len(picks) + 1}, or finish with the rest as listed. "
            f"No tap for a minute and it posts like that.")


def decided_text(group: str, labels_in_order: list[str], source: str) -> str:
    why = {
        'you':     "",
        'partial': " (no tap for a minute — the rest as listed)",
        'default': " (no answer in a minute — kept as listed)",
    }.get(source, "")
    return (f"🎠 The '{group}' carousel is posting in this order{why}:\n\n"
            f"{numbered(labels_in_order)}")


# ── State on Cloudinary ──────────────────────────────────────────────────────

def _cloudinary():
    import cloudinary
    import cloudinary.api
    import cloudinary.uploader
    cloudinary.config(
        cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
        api_key=os.getenv('CLOUDINARY_API_KEY'),
        api_secret=os.getenv('CLOUDINARY_API_SECRET'),
    )
    return cloudinary


def _public_id(qid: str) -> str:
    return f'{FOLDER}/{qid}.json'


def read_state(qid: str) -> dict | None:
    """The question's state, or None if there is none (or it can't be read)."""
    cloudinary = _cloudinary()
    try:
        resource = cloudinary.api.resource(_public_id(qid), resource_type='raw')
        response = requests.get(resource['secure_url'],
                                params={'_': int(_now().timestamp())},
                                timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


def write_state(qid: str, state: dict) -> None:
    """Store the state, stamping updated_at. Raises on failure."""
    cloudinary = _cloudinary()
    state = dict(state, updated_at=_iso(_now()))
    blob = io.BytesIO(json.dumps(state, ensure_ascii=False, indent=2).encode())
    cloudinary.uploader.upload(blob, resource_type='raw',
                               public_id=_public_id(qid),
                               overwrite=True, invalidate=True)


# ── The bot's side: one tap ──────────────────────────────────────────────────

def record_tap(data: str) -> tuple[str, list[tuple[str, str]] | None]:
    """
    Apply one button tap and return (new message text, buttons or None).

    Buttons None means the question is closed — decided now, or already.
    Runs in the bot; blocking I/O, so the bot calls it off the event loop.
    """
    parsed = parse_callback(data)
    if parsed is None:
        return "That button doesn't make sense to me any more.", None
    qid, picks, done = parsed

    state = read_state(qid)
    if state is None:
        return ("That carousel question has expired — the order can't be "
                "changed from here any more."), None
    group, labels = state['group'], state['labels']
    match_ids = state['match_ids']
    if state.get('final'):
        order = state.get('order') or match_ids
        by_id = dict(zip(match_ids, labels))
        return ("Too late — the order was already settled:\n\n"
                + numbered([by_id[m] for m in order])), None
    if any(i >= len(labels) for i in picks):
        return "That button doesn't match this carousel.", None

    if is_complete(len(labels), picks, done):
        order = apply_order(match_ids, picks)
        write_state(qid, dict(state, picks=picks, final=True, order=order,
                              source='you'))
        by_id = dict(zip(match_ids, labels))
        return decided_text(group, [by_id[m] for m in order], 'you'), None

    write_state(qid, dict(state, picks=picks))
    return question_text(group, labels, picks), keyboard(qid, labels, picks)


# ── The dispatcher's side: ask, wait, decide ─────────────────────────────────

def _parse(when: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(when))
    except (TypeError, ValueError):
        return None


def choose_order(group: str, group_root: str, match_ids: list[str],
                 labels: list[str], *, send_choice, edit_message,
                 sleep, now=None, images: list[str] | None = None,
                 send_photos=None) -> list[str]:
    """
    Ask for the slide order and return it, blocking until it is decided.

    Asks once per group: an earlier decision (a retry after a failed post) is
    reused as it is, and a question still open from an interrupted run is
    waited on rather than sent again. Telegram and the clock are passed in so
    this can be driven without either.

    images, aligned with match_ids, are the scorecards: sent first as a
    numbered album so each button can be matched to its card. An album can't
    carry buttons, which is why the question follows it as its own message.
    """
    now = now or _now
    if len(match_ids) < 2:
        return list(match_ids)
    qid = question_id(group_root)
    state = read_state(qid)

    if state and state.get('final') and state.get('order'):
        print(f'[carousel] {group}: order already decided — reusing it.')
        return [m for m in state['order'] if m in match_ids] + \
               [m for m in match_ids if m not in state['order']]

    if not state or state.get('match_ids') != match_ids:
        state = {'group': group, 'match_ids': match_ids, 'labels': labels,
                 'picks': [], 'final': False, 'asked_at': _iso(now())}
        write_state(qid, state)
        if send_photos and images:
            send_photos([(url, f"{i}. {label}")
                         for i, (url, label) in enumerate(zip(images, labels), 1)
                         if url])
        state['message_id'] = send_choice(
            question_text(group, labels, []), keyboard(qid, labels, []))
        write_state(qid, state)
        state = read_state(qid) or dict(state, updated_at=_iso(now()))
        print(f'[carousel] {group}: asked for the slide order.')

    started = now()
    while True:
        last = _parse(state.get('updated_at')) or _parse(state.get('asked_at')) or started
        if state.get('final'):
            break
        if (now() - last).total_seconds() >= IDLE_SECS:
            break
        if (now() - started).total_seconds() >= MAX_WAIT_SECS:
            print(f'[carousel] {group}: order still being picked after '
                  f'{MAX_WAIT_SECS // 60} min — going with it as it stands.')
            break
        sleep(POLL_SECS)
        state = read_state(qid) or state

    if state.get('final') and state.get('order'):
        order = state['order']
    else:
        picks = state.get('picks') or []
        order = apply_order(match_ids, picks)
        source = 'partial' if picks else 'default'
        try:
            write_state(qid, dict(state, final=True, order=order, source=source))
        except Exception as e:
            print(f'[carousel] {group}: could not record the order ({e}).')
        by_id = dict(zip(match_ids, labels))
        if state.get('message_id'):
            edit_message(state['message_id'],
                         decided_text(group, [by_id[m] for m in order], source))
    print(f'[carousel] {group}: slide order {order}.')
    return order
