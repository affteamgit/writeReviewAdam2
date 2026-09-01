"""
config.py - credential and setting resolution shared by the CLI and the Streamlit app.

Exists so writeReviewAgent.py can run both ways without branching on its environment:
under Streamlit the values come from st.secrets, from a shell they come from env vars
and a key file on disk. Resolution order is st.secrets -> environment -> local file,
first match wins, so a deployed app never silently falls back to a developer's laptop
credentials and a local run never needs Streamlit installed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

# Defaults; every one can be overridden by a secret or an env var of the same name.
DEFAULTS = {
    "SPREADSHEET_ID": "1ZneRUz90Ne06pr8CCax8vp30tOtPpKJQCw5ikE-uB_0",
    "GAMBLINEERS_SITE": "Gamblineers",
    "GOOGLE_SERVICE_ACCOUNT_FILE":
        "/Users/gorandelic/Desktop/Work/reviewChecker/service_accountNew.json",
    "SITEMAP_URL": "https://gamblineers.com/post-sitemap.xml",
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]


def _secrets() -> Optional[Any]:
    """st.secrets if we're inside a Streamlit run, else None.

    Guarded because importing streamlit outside a Streamlit process works but touching
    st.secrets without a secrets file raises, and because the CLI should not require
    streamlit to be installed at all.
    """
    try:
        import streamlit as st  # noqa: PLC0415
    except Exception:
        return None
    try:
        _ = st.secrets  # raises if no secrets are configured
        return st.secrets
    except Exception:
        return None


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    """One setting, resolved st.secrets -> env -> DEFAULTS -> supplied default."""
    secrets = _secrets()
    if secrets is not None:
        try:
            if name in secrets:
                return str(secrets[name])
        except Exception:
            pass
    if os.environ.get(name):
        return os.environ[name]
    if name in DEFAULTS:
        return DEFAULTS[name]
    return default


def require(name: str) -> str:
    value = get(name)
    if not value:
        raise RuntimeError(
            f"Missing required setting {name!r}. Set it in Streamlit secrets or as an "
            f"environment variable."
        )
    return value


def anthropic_api_key() -> str:
    return require("ANTHROPIC_API_KEY")


def google_credentials():
    """Service-account credentials with Sheets + Docs + Drive scope.

    Under Streamlit the key is a `[service_account]` table in secrets (same shape the
    existing writeReviewAdam.py app already uses, so no new secret needs creating).
    Locally it is a JSON key file on disk.
    """
    from google.oauth2.service_account import Credentials  # noqa: PLC0415

    secrets = _secrets()
    if secrets is not None:
        try:
            if "service_account" in secrets:
                info = dict(secrets["service_account"])
                return Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception:
            pass

    path = Path(require("GOOGLE_SERVICE_ACCOUNT_FILE"))
    if not path.exists():
        raise RuntimeError(
            f"Google service-account key not found at {path}. Either add a "
            f"[service_account] table to Streamlit secrets or point "
            f"GOOGLE_SERVICE_ACCOUNT_FILE at a readable key file."
        )
    return Credentials.from_service_account_file(str(path), scopes=SCOPES)


def sheets_service():
    from googleapiclient.discovery import build  # noqa: PLC0415
    return build("sheets", "v4", credentials=google_credentials(), cache_discovery=False)


def docs_service():
    from googleapiclient.discovery import build  # noqa: PLC0415
    return build("docs", "v1", credentials=google_credentials(), cache_discovery=False)


def drive_service():
    from googleapiclient.discovery import build  # noqa: PLC0415
    return build("drive", "v3", credentials=google_credentials(), cache_discovery=False)
