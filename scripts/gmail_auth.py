"""Gmail OAuth2 helper — generates the token JSON for .env.

Usage:
    python scripts/gmail_auth.py [show_id]

    show_id: optional, e.g. "hootline" or "sparrow".
             When SHOW_IDS is configured, defaults to the first show.

This will print a URL. Open it in your browser, sign in, grant access.
After authorization, your browser will redirect to a localhost URL that
won't load — that's expected. Copy the FULL URL from your browser's
address bar and paste it back here. The script extracts the code and
generates your token.
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _resolve_show(show_id: str | None) -> tuple[str, str, str]:
    """Return (creds_json, token_env_key, label) for the given show.

    When show_id is None and SHOW_IDS is set, uses the first show.
    Falls back to flat GMAIL_CREDENTIALS_JSON for legacy single-show mode.
    """
    from dotenv import dotenv_values
    from config import settings

    env = dotenv_values(".env")

    if settings.show_ids.strip():
        ids = [s.strip().lower() for s in settings.show_ids.split(",") if s.strip()]
        sid = show_id or ids[0]
        if sid not in ids:
            print(f"ERROR: Show '{sid}' not in SHOW_IDS ({', '.join(ids)})")
            sys.exit(1)
        prefix = f"SHOW_{sid.upper()}_"
        creds_json = env.get(f"{prefix}GMAIL_CREDENTIALS_JSON", "")
        token_key = f"{prefix}GMAIL_TOKEN_JSON"
        label = sid
    else:
        creds_json = settings.gmail_credentials_json
        token_key = "GMAIL_TOKEN_JSON"
        label = "default"

    if not creds_json:
        print(f"ERROR: No Gmail credentials found for show '{label}'")
        sys.exit(1)

    return creds_json, token_key, label


def main():
    show_id = sys.argv[1] if len(sys.argv) > 1 else None

    creds_json, token_env_key, label = _resolve_show(show_id)
    creds_data = json.loads(creds_json)

    tmp_creds = Path("/tmp/noctua_oauth_creds.json")
    tmp_creds.write_text(json.dumps(creds_data))

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(tmp_creds), SCOPES,
            redirect_uri="http://localhost"
        )

        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )

        print()
        print("=" * 60)
        print(f"GMAIL AUTHORIZATION — show: {label}")
        print("=" * 60)
        print()
        print("1. Open this URL in your browser:")
        print()
        print(auth_url)
        print()
        print("2. Sign in and grant Gmail read-only access")
        print("3. You'll be redirected to a page that won't load")
        print("   (http://localhost?code=... — that's expected!)")
        print("4. Copy the FULL URL from your address bar")
        print("5. Paste it below:")
        print()

        redirect_url = input("Paste URL here: ").strip()

        # Extract the authorization code from the redirect URL
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)

        if "code" not in params:
            print("ERROR: No authorization code found in the URL.")
            print("Make sure you copied the full URL including ?code=...")
            sys.exit(1)

        code = params["code"][0]
        flow.fetch_token(code=code)
        creds = flow.credentials

        token_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes),
        }

        token_json = json.dumps(token_data)

        # Auto-update the .env file
        env_path = Path(__file__).resolve().parent.parent / ".env"
        env_content = env_path.read_text()

        # Replace existing token value or append
        pattern = re.compile(rf"^{re.escape(token_env_key)}=.*$", re.MULTILINE)
        if pattern.search(env_content):
            env_content = pattern.sub(f"{token_env_key}={token_json}", env_content)
        else:
            env_content += f"\n{token_env_key}={token_json}\n"

        env_path.write_text(env_content)

        print()
        print("=" * 60)
        print("SUCCESS!")
        print("=" * 60)
        print()
        print(f"{token_env_key} has been updated in .env")
        print()

        # Verify it works
        print("Verifying token...")
        test_creds = Credentials.from_authorized_user_info(token_data)
        if test_creds.expired and test_creds.refresh_token:
            test_creds.refresh(Request())
        print("Token is valid! Gmail API is ready.")

    finally:
        tmp_creds.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
