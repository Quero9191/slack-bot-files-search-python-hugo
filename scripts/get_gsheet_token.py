#!/usr/bin/env python3
"""
One-time script to perform OAuth desktop flow and save a token file

Place your downloaded OAuth client JSON in `secrets/credentials.json` and run this
script once. It will open the browser to authorize and create `secrets/token.pickle`.

Usage:
  python3 scripts/get_gsheet_token.py

Do NOT commit the files inside `secrets/`.
"""
from pathlib import Path
import pickle
import sys

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
CREDENTIALS_PATH = SECRETS / "credentials.json"
TOKEN_PATH = SECRETS / "token.pickle"


def main() -> None:
    SECRETS.mkdir(parents=True, exist_ok=True)

    if not CREDENTIALS_PATH.exists():
        print(f"Missing credentials.json at {CREDENTIALS_PATH}. Put your OAuth client JSON there.")
        sys.exit(1)

    creds = None
    if TOKEN_PATH.exists():
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)

    if not creds or not getattr(creds, "valid", False):
        if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
        print(f"Saved token to {TOKEN_PATH}")

    print("Access token valid:", getattr(creds, "valid", False))
    print("Expiry:", getattr(creds, "expiry", None))


if __name__ == "__main__":
    main()
