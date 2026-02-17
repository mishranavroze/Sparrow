"""Gmail OAuth2 helper — generates the token JSON for .env.

Usage:
    python scripts/gmail_auth.py

This will print a URL. Open it in your browser, sign in, grant access.
After authorization, your browser will redirect to a localhost URL that
won't load — that's expected. Copy the FULL URL from your browser's
address bar and paste it back here. The script extracts the code and
generates your token.
"""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main():
    from config import settings
    creds_json = settings.gmail_credentials_json

    if not creds_json:
        print("ERROR: No GMAIL_CREDENTIALS_JSON found in .env")
        sys.exit(1)

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
        print("GMAIL AUTHORIZATION")
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

        print()
        print("=" * 60)
        print("SUCCESS!")
        print("=" * 60)
        print()
        print("Your GMAIL_TOKEN_JSON has been saved to .env")
        print()

        # Auto-update the .env file
        env_path = Path(__file__).resolve().parent.parent / ".env"
        env_content = env_path.read_text()
        env_content = env_content.replace(
            "GMAIL_TOKEN_JSON=",
            f"GMAIL_TOKEN_JSON={token_json}",
            1,
        )
        env_path.write_text(env_content)

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
