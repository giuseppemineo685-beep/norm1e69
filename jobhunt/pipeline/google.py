"""Google Drive, Sheets and Gmail through one OAuth user token.

Run `python auth_google.py` once to produce the token JSON, then store it in the
GOOGLE_TOKEN_JSON secret. A user token (not a service account) is used on purpose:
service accounts have no Drive storage quota and cannot create files in My Drive.
"""
from __future__ import annotations

import io
import json
import os
import re
from datetime import date
from typing import Optional

import markdown as md
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

GOOGLE_DOC = "application/vnd.google-apps.document"
FOLDER = "application/vnd.google-apps.folder"


def credentials() -> Credentials:
    raw = os.environ.get("GOOGLE_TOKEN_JSON")
    if not raw:
        path = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
        raw = open(path, "r", encoding="utf-8").read()
    return Credentials.from_authorized_user_info(json.loads(raw), SCOPES)


class Google:
    def __init__(self, creds: Optional[Credentials] = None):
        creds = creds or credentials()
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ---- Drive -------------------------------------------------------------
    def create_folder(self, name: str, parent_id: str) -> tuple[str, str]:
        meta = {"name": name, "mimeType": FOLDER, "parents": [parent_id]}
        f = self.drive.files().create(body=meta, fields="id,webViewLink", supportsAllDrives=True).execute()
        return f["id"], f["webViewLink"]

    def upload_markdown_as_doc(self, name: str, markdown_text: str, folder_id: str) -> str:
        """Upload markdown converted to HTML; Drive turns it into an editable Google Doc."""
        html = md.markdown(markdown_text, extensions=["extra"])
        media = MediaIoBaseUpload(io.BytesIO(html.encode("utf-8")), mimetype="text/html", resumable=False)
        f = self.drive.files().create(
            body={"name": name, "mimeType": GOOGLE_DOC, "parents": [folder_id]},
            media_body=media, fields="id,webViewLink", supportsAllDrives=True).execute()
        return f["webViewLink"]

    def upload_text(self, name: str, text: str, folder_id: str, mimetype: str = "text/plain") -> str:
        media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype=mimetype, resumable=False)
        f = self.drive.files().create(
            body={"name": name, "parents": [folder_id]},
            media_body=media, fields="id,webViewLink", supportsAllDrives=True).execute()
        return f["webViewLink"]

    # ---- Sheets ------------------------------------------------------------
    def append_row(self, sheet_id: str, row: list, tab: str = "Applications") -> None:
        self.sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id, range=f"{tab}!A1", valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS", body={"values": [row]}).execute()

    def ensure_header(self, sheet_id: str, header: list[str], tab: str = "Applications") -> None:
        meta = self.sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if tab not in titles:
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]}).execute()
        got = self.sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=f"{tab}!1:1").execute()
        if not got.get("values"):
            self.sheets.spreadsheets().values().update(
                spreadsheetId=sheet_id, range=f"{tab}!A1", valueInputOption="RAW",
                body={"values": [header]}).execute()


def folder_name(company: str, when: Optional[date] = None) -> str:
    when = when or date.today()
    safe = re.sub(r"[^A-Za-z0-9]+", "_", company).strip("_") or "Company"
    return f"{safe}_{when.isoformat()}"


TRACKER_HEADER = ["Date", "Company", "Title", "Location", "Score", "Status",
                  "Posting URL", "Drive folder", "CV", "Cover letter", "Source", "Notes"]
