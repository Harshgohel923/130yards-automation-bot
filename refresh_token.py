# refresh_token.py
# Run this every ~50 days to keep your Instagram long-lived token alive.
# Schedule it as a cron job:  0 9 */45 * * python refresh_token.py

import os

import requests
from dotenv import load_dotenv, set_key

load_dotenv()

res = requests.get(
    'https://graph.instagram.com/refresh_access_token',
    params={
        'grant_type':   'ig_refresh_token',
        'access_token': os.getenv('IG_ACCESS_TOKEN'),
    },
    timeout=10,
)
res.raise_for_status()
new_token = res.json()['access_token']
set_key('.env', 'IG_ACCESS_TOKEN', new_token)
print('✅  Instagram token refreshed successfully.')