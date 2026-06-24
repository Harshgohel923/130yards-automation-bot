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
    for attempt in range(CONTAINER_POLL_ATTEMPTS):
        status_res = requests.get(
            f'{GRAPH_URL}/{container_id}',
            params={'fields': 'status_code', 'access_token': ACCESS_TOKEN},
            timeout=10,
        )
        status_res.raise_for_status()
        status = status_res.json().get('status_code')

        if status == 'FINISHED':
            break
        if status == 'ERROR':
            raise RuntimeError(f'Instagram container {container_id} processing failed')

        print(f"[instagram] Container status: {status} (attempt {attempt + 1})")
        time.sleep(CONTAINER_POLL_INTERVAL)
    else:
        raise RuntimeError(
            f'Instagram container {container_id} not ready after '
            f'{CONTAINER_POLL_ATTEMPTS} attempts'
        )

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