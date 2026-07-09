# # cloudinary_utils.py
# """
# Template fetcher.

# Templates are stored in Cloudinary under:
#   scorecard_templates/ht_<match_id>   ← halftime template
#   scorecard_templates/ft_<match_id>   ← fulltime template

# If no match-specific template exists, the caller falls back to a local asset.
# """

# import os

# import cloudinary
# import cloudinary.api
# import requests
# from dotenv import load_dotenv

# load_dotenv()

# cloudinary.config(
#     cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
#     api_key=os.getenv('CLOUDINARY_API_KEY'),
#     api_secret=os.getenv('CLOUDINARY_API_SECRET'),
# )

# TEMPLATE_FOLDER = 'scorecard_templates'
# LOCAL_CACHE_DIR = 'assets/templates'


# def fetch_template(template_key: str) -> str | None:
#     """
#     Try to download a template from Cloudinary identified by template_key.
#     template_key examples: 'ft_54328046', 'ht_54328046'

#     Returns the local cached path if found, None otherwise.
#     """
#     os.makedirs(LOCAL_CACHE_DIR, exist_ok=True)
#     local_path = os.path.join(LOCAL_CACHE_DIR, f'{template_key}.png')

#     if os.path.exists(local_path):
#         return local_path

#     public_id = f'{TEMPLATE_FOLDER}/{template_key}'
#     try:
#         result = cloudinary.api.resource(public_id)
#         url    = result['secure_url']

#         response = requests.get(url, timeout=15)
#         response.raise_for_status()
#         with open(local_path, 'wb') as f:
#             f.write(response.content)

#         print(f"[cloudinary] Template downloaded: {template_key}.png")
#         return local_path

#     except cloudinary.api.NotFound:
#         print(f"[cloudinary] No template for key '{template_key}' — using fallback.")
#         return None
#     except Exception as e:
#         print(f"[cloudinary] Error fetching '{template_key}': {e}")
#         return None


# cloudinary_utils.py
"""
Template fetcher.

Templates are stored in Cloudinary as fixed assets defined in config.py:
  CLOUDINARY_TEMPLATES = {
      "HT": "Half_time_template_ujqiub",
      "FT": "Full_time_template_hckszk",
  }

Both templates are shared across all matches — there are no per-match
template variants. Templates are cached locally after the first download
so subsequent calls within the same run are instant.
"""

import os

import cloudinary
import cloudinary.uploader
import requests
from dotenv import load_dotenv

from config import CLOUDINARY_TEMPLATES, CLOUD_NAME

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)

LOCAL_CACHE_DIR = 'assets/templates'
MATCH_PHOTO_FOLDER = 'match_photos'


def photo_public_id(match_id: str, event_type: str) -> str:
    """Deterministic Cloudinary public_id for a match photo uploaded via the
    Telegram bot: match_photos/<match_id>_<HT|FT>."""
    return f"{MATCH_PHOTO_FOLDER}/{match_id}_{event_type.upper()}"


def fetch_match_photo(match_id: str, event_type: str, dest_dir: str = 'assets/match_photos') -> str | None:
    """Download the bot-uploaded photo for a match/event to a local file.
    Returns the local path, or None if no photo has been uploaded."""
    os.makedirs(dest_dir, exist_ok=True)
    local_path = os.path.join(dest_dir, f"{match_id}_{event_type.upper()}.jpg")
    url = f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{photo_public_id(match_id, event_type)}"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except Exception:
        return None
    with open(local_path, 'wb') as f:
        f.write(response.content)
    return local_path


def fetch_template(event_type: str) -> str | None:
    """
    Download the HT or FT template from Cloudinary and cache it locally.

    event_type: 'HT' or 'FT'

    Returns the local cached file path on success, None on failure.
    """
    public_id = CLOUDINARY_TEMPLATES.get(event_type.upper())
    if not public_id:
        print(f"[cloudinary] No template configured for event_type '{event_type}'")
        return None

    os.makedirs(LOCAL_CACHE_DIR, exist_ok=True)
    local_path = os.path.join(LOCAL_CACHE_DIR, f'{event_type.lower()}_template.png')

    # Return cached copy if already downloaded
    if os.path.exists(local_path):
        return local_path

    url = f"https://res.cloudinary.com/{CLOUD_NAME}/image/upload/{public_id}"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        with open(local_path, 'wb') as f:
            f.write(response.content)
        print(f"[cloudinary] Template downloaded: {event_type} → {local_path}")
        return local_path

    except Exception as e:
        print(f"[cloudinary] Error fetching template '{event_type}': {e}")
        return None