"""One-time Google OAuth. Produces token.json (keep it private, put it in GOOGLE_TOKEN_JSON).

1. Google Cloud Console -> create a project -> enable Gmail API, Drive API, Sheets API.
2. OAuth consent screen (External, add yourself as test user).
3. Credentials -> OAuth client ID -> Desktop app -> download as client_secret.json here.
4. python auth_google.py
"""
from google_auth_oauthlib.flow import InstalledAppFlow

from pipeline.google import SCOPES

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
open("token.json", "w", encoding="utf-8").write(creds.to_json())
print("token.json written. Copy its content into the GOOGLE_TOKEN_JSON secret.")
