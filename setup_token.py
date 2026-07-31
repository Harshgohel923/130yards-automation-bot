# setup_token.py
# One-time helper: turn a short-lived Graph API Explorer token into a
# long-lived one, verify it has every permission the bot needs (including
# instagram_manage_contents for deleting posts), and save it to .env.
#
# Usage:
#   python setup_token.py <SHORT_LIVED_TOKEN>
#
# Requires FB_APP_ID and FB_APP_SECRET in .env
# (App Dashboard -> Settings -> Basic).

import sys

import requests
from dotenv import dotenv_values, set_key

GRAPH_URL = 'https://graph.facebook.com/v19.0'

REQUIRED_PERMS = {
    'instagram_basic',
    'instagram_content_publish',
    'instagram_manage_contents',   # needed for DELETE /{media_id}
    'pages_show_list',
    'pages_read_engagement',
}


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit('Usage: python setup_token.py <SHORT_LIVED_TOKEN>')
    short_token = sys.argv[1].strip()

    env = dotenv_values('.env')
    app_id     = env.get('FB_APP_ID')
    app_secret = env.get('FB_APP_SECRET')
    if not app_id or not app_secret:
        sys.exit('Add FB_APP_ID and FB_APP_SECRET to .env first '
                 '(App Dashboard -> Settings -> Basic).')

    # 1. Exchange for a long-lived (~60 day) token
    res = requests.get(
        f'{GRAPH_URL}/oauth/access_token',
        params={
            'grant_type':        'fb_exchange_token',
            'client_id':         app_id,
            'client_secret':     app_secret,
            'fb_exchange_token': short_token,
        },
        timeout=15,
    )
    if not res.ok:
        sys.exit(f'Token exchange failed (HTTP {res.status_code}): {res.text}')
    long_token = res.json()['access_token']
    print('[setup_token] Exchanged for long-lived token.')

    # 2. Verify granted permissions
    res = requests.get(
        f'{GRAPH_URL}/me/permissions',
        params={'access_token': long_token},
        timeout=15,
    )
    res.raise_for_status()
    granted = {p['permission'] for p in res.json().get('data', [])
               if p.get('status') == 'granted'}
    missing = REQUIRED_PERMS - granted
    print(f'[setup_token] Granted permissions: {", ".join(sorted(granted))}')
    if missing:
        sys.exit(f'❌ Token is missing: {", ".join(sorted(missing))}. '
                 'Regenerate it in Graph API Explorer with those scopes ticked.')

    # 3. Trade the 60-day user token for the Page token, which never expires.
    # The page id comes from the token's own granular scopes.
    res = requests.get(
        f'{GRAPH_URL}/debug_token',
        params={'input_token': long_token,
                'access_token': f'{app_id}|{app_secret}'},
        timeout=15,
    )
    res.raise_for_status()
    page_ids = next((g.get('target_ids') for g in
                     res.json()['data'].get('granular_scopes', [])
                     if g.get('scope') == 'pages_show_list'), None)
    if not page_ids:
        sys.exit('❌ Token has no authorized Page — reselect your Page in the '
                 'login popup when generating the token.')
    res = requests.get(
        f'{GRAPH_URL}/{page_ids[0]}',
        params={'fields': 'name,access_token', 'access_token': long_token},
        timeout=15,
    )
    res.raise_for_status()
    page_token = res.json()['access_token']
    print(f"[setup_token] Got non-expiring Page token for: {res.json()['name']}")

    # 4. Confirm it can see the IG account
    ig_user_id = env.get('IG_USER_ID')
    if ig_user_id:
        res = requests.get(
            f'{GRAPH_URL}/{ig_user_id}',
            params={'fields': 'username', 'access_token': page_token},
            timeout=15,
        )
        if res.ok:
            print(f"[setup_token] IG account OK: @{res.json().get('username')}")
        else:
            sys.exit(f'❌ Token cannot access IG user {ig_user_id}: {res.text}')

    # 5. Save
    set_key('.env', 'IG_ACCESS_TOKEN', page_token)
    print('✅  IG_ACCESS_TOKEN updated in .env — posting AND deleting will work, '
          'and this token never expires.')


if __name__ == '__main__':
    main()
