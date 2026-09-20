"""
Drop-in Gmail OAuth bootstrap. Copy this into your project and edit the
constants below to match your layout — this is a template, not a library
meant to be imported unmodified across projects (TOKEN_PATH/CREDENTIALS_PATH
are relative paths and SCOPES is project-specific).
"""
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Edit these for your project ──────────────────────────────────────────
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',   # needed to apply "summarized" label
]
TOKEN_PATH       = 'token.json'
CREDENTIALS_PATH = 'credentials.json'
# ──────────────────────────────────────────────────────────────────────────


def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        # Read scopes from raw JSON — Credentials.from_authorized_user_file()
        # overwrites creds.scopes with the passed-in SCOPES so it can't be
        # trusted to tell you what was actually granted. See SKILL.md.
        with open(TOKEN_PATH) as _f:
            _tok = json.load(_f)
        _stored = (
            set(_tok.get('scopes', '').split())
            if isinstance(_tok.get('scopes'), str)
            else set(_tok.get('scopes') or [])
        )
        if not set(SCOPES).issubset(_stored):
            print("Scope change detected — deleting token.json and re-authenticating...")
            os.remove(TOKEN_PATH)
        else:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_PATH):
                raise FileNotFoundError(
                    f"Missing '{CREDENTIALS_PATH}'. Download it from Google Cloud "
                    f"Console (APIs & Services > Credentials > Create OAuth client "
                    f"ID > Desktop app)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w') as f:
            f.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)
