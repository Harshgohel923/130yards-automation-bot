# instagram.py — Meta Graph API poster
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

IG_USER_ID   = os.getenv('IG_USER_ID')
ACCESS_TOKEN = os.getenv('IG_ACCESS_TOKEN')
GRAPH_URL    = 'https://graph.facebook.com/v19.0'

# How many times to poll for container FINISHED status before giving up
CONTAINER_POLL_ATTEMPTS = 20
CONTAINER_POLL_INTERVAL = 3   # seconds

# What Instagram allows per rolling 24h when the API doesn't say. Used only
# as a floor for the reading in publishing_limit().
DEFAULT_PUBLISH_QUOTA = 25

# Instagram's own limits on a carousel post.
CAROUSEL_MIN_ITEMS = 2
CAROUSEL_MAX_ITEMS = 10


def post_to_instagram(public_image_url: str, caption: str) -> str:
    """
    Publish a single image post to Instagram via the Graph API.
    Returns the Instagram media ID on success.
    Raises RuntimeError / requests.HTTPError on failure.
    """
    # Step 1: Create media container
    res = requests.post(
        f'{GRAPH_URL}/{IG_USER_ID}/media',
        data={
            'image_url':    public_image_url,
            'caption':      caption,
            'access_token': ACCESS_TOKEN,
        },
        timeout=15,
    )
    print(res.json()) 
    res.raise_for_status()
    container_id = res.json()['id']
    print(f"[instagram] Container created: {container_id}")

    # Step 2: Poll until container is ready
    _wait_for_container(container_id)

    # Step 3: Publish
    pub = requests.post(
        f'{GRAPH_URL}/{IG_USER_ID}/media_publish',
        data={
            'creation_id':  container_id,
            'access_token': ACCESS_TOKEN,
        },
        timeout=15,
    )
    pub.raise_for_status()
    media_id = pub.json()['id']
    print(f"[instagram] Published: {media_id}")
    return media_id


def _wait_for_container(container_id: str) -> None:
    """Poll a media container until it reports FINISHED. Raises on ERROR or timeout."""
    for attempt in range(CONTAINER_POLL_ATTEMPTS):
        status_res = requests.get(
            f'{GRAPH_URL}/{container_id}',
            params={'fields': 'status_code', 'access_token': ACCESS_TOKEN},
            timeout=10,
        )
        status_res.raise_for_status()
        status = status_res.json().get('status_code')

        if status == 'FINISHED':
            return
        if status == 'ERROR':
            raise RuntimeError(f'Instagram container {container_id} processing failed')

        print(f"[instagram] Container status: {status} (attempt {attempt + 1})")
        time.sleep(CONTAINER_POLL_INTERVAL)

    raise RuntimeError(
        f'Instagram container {container_id} not ready after '
        f'{CONTAINER_POLL_ATTEMPTS} attempts'
    )


def post_carousel_to_instagram(public_image_urls: list[str], caption: str) -> str:
    """
    Publish a multi-image carousel post to Instagram via the Graph API.

    Each image becomes a child container (is_carousel_item), which is then
    collected into one CAROUSEL container carrying the caption. Instagram
    accepts between CAROUSEL_MIN_ITEMS and CAROUSEL_MAX_ITEMS slides and shows
    every slide at the first one's aspect ratio — all our pages are rendered at
    the same size, so nothing gets cropped differently.

    Returns the Instagram media ID on success.
    """
    urls = [u for u in public_image_urls if u]
    if not CAROUSEL_MIN_ITEMS <= len(urls) <= CAROUSEL_MAX_ITEMS:
        raise ValueError(
            f'A carousel needs {CAROUSEL_MIN_ITEMS}-{CAROUSEL_MAX_ITEMS} images, '
            f'got {len(urls)}'
        )

    # Step 1: one child container per slide
    children = []
    for i, url in enumerate(urls, 1):
        res = requests.post(
            f'{GRAPH_URL}/{IG_USER_ID}/media',
            data={
                'image_url':       url,
                'is_carousel_item': 'true',
                'access_token':    ACCESS_TOKEN,
            },
            timeout=15,
        )
        res.raise_for_status()
        child_id = res.json()['id']
        children.append(child_id)
        print(f"[instagram] Carousel item {i}/{len(urls)} created: {child_id}")

    # Step 2: wait for every child to finish processing
    for child_id in children:
        _wait_for_container(child_id)

    # Step 3: the carousel container itself — this is what carries the caption
    parent = requests.post(
        f'{GRAPH_URL}/{IG_USER_ID}/media',
        data={
            'media_type':   'CAROUSEL',
            'children':     ','.join(children),
            'caption':      caption,
            'access_token': ACCESS_TOKEN,
        },
        timeout=15,
    )
    parent.raise_for_status()
    container_id = parent.json()['id']
    print(f"[instagram] Carousel container created: {container_id}")
    _wait_for_container(container_id)

    # Step 4: publish
    pub = requests.post(
        f'{GRAPH_URL}/{IG_USER_ID}/media_publish',
        data={
            'creation_id':  container_id,
            'access_token': ACCESS_TOKEN,
        },
        timeout=15,
    )
    pub.raise_for_status()
    media_id = pub.json()['id']
    print(f"[instagram] Published carousel ({len(urls)} slides): {media_id}")
    return media_id


def publishing_limit() -> tuple[int, int] | None:
    """(posts published in the last 24h, the cap). None if it can't be read.

    Instagram allows a fixed number of API-published posts per rolling 24
    hours per account and simply refuses the next one — there is no warning
    and no queue. Nothing else in this pipeline counts them, which was
    survivable while the number of posts per match was fixed. It stopped
    being survivable once event photos made it unbounded.

    Returns None rather than raising: a reading that can't be taken must not
    stop a post going out, or a Graph blip would cost the full-time card.
    """
    try:
        res = requests.get(
            f'{GRAPH_URL}/{IG_USER_ID}/content_publishing_limit',
            params={'fields': 'config,quota_usage',
                    'access_token': ACCESS_TOKEN},
            timeout=10,
        )
        res.raise_for_status()
        data = (res.json().get('data') or [{}])[0]
        used = int(data.get('quota_usage') or 0)
        cap = int((data.get('config') or {}).get('quota_total') or 0)
    except Exception as e:
        print(f'[instagram] Could not read the publishing limit: {e}')
        return None
    return (used, cap or DEFAULT_PUBLISH_QUOTA)


def get_post_permalink(media_id: str) -> str | None:
    """Return the public URL of a post, or None if the lookup fails."""
    try:
        res = requests.get(
            f'{GRAPH_URL}/{media_id}',
            params={'fields': 'permalink', 'access_token': ACCESS_TOKEN},
            timeout=15,
        )
        return res.json().get('permalink')
    except Exception:
        return None


def delete_instagram_post(media_id: str) -> None:
    """
    Delete an Instagram post by media_id. Raises on failure.
    NOTE: requires the token to have the `instagram_manage_contents`
    permission — `instagram_content_publish` alone can post but NOT delete.
    """
    res = requests.delete(
        f'{GRAPH_URL}/{media_id}',
        params={'access_token': ACCESS_TOKEN},
        timeout=15,
    )
    try:
        body = res.json()
    except ValueError:
        body = {}
    if not res.ok or body.get('success') is not True:
        raise RuntimeError(
            f"Instagram delete of {media_id} failed "
            f"(HTTP {res.status_code}): {body or res.text}"
        )
    print(f"[instagram] Deleted post: {media_id}")