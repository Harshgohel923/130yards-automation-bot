# cloudinary_upload.py
import os

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv

load_dotenv()

cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
)


def upload_image(local_path: str) -> str:
    """
    Upload a local image to Cloudinary and return the secure URL.
    Raises on failure so the caller can handle retries.
    """
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Scorecard not found at: {local_path}")

    result = cloudinary.uploader.upload(local_path, folder='scorecards')
    url    = result['secure_url']
    print(f"[cloudinary] Uploaded: {url}")
    return url


def upload_match_data(local_path: str) -> str:
    """
    Upload a match data JSON file to the Cloudinary 'data/' folder as a raw
    resource and return the secure URL. Raises on failure.
    """
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Match data not found at: {local_path}")

    filename  = os.path.splitext(os.path.basename(local_path))[0]
    result    = cloudinary.uploader.upload(
        local_path,
        resource_type='raw',
        folder='data',
        public_id=filename,
        use_filename=False,
    )
    url = result['secure_url']
    print(f"[cloudinary] Match data archived: {url}")
    return url