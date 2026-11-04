"""Part 2: Authenticating Federated Connectors through a Custom UI.

When a user queries a Federated Connector (Jira, Confluence, ServiceNow, Salesforce,
Google Drive) through a custom UI calling `:streamAssist`, Gemini Enterprise checks its
managed `Keystore` for that user's 3-legged OAuth (3LO) token.

This script implements the two Discovery Engine `dataConnector` REST endpoints your
custom UI backend uses to check and store per-user connector credentials:

  - Step 1 (`--step check`):
      Calls `GET dataConnector:acquireAccessToken`.
      If the user already has a valid token in the Gemini Enterprise Keystore, it returns
      `accessToken`. Otherwise, it returns `authUri` for your custom UI to open in a popup.
  - Step 2 (Browser Popup in Custom UI):
      The user signs in to the third-party app. Google's hosted callback page
      (`https://vertexaisearch.cloud.google.com/oauth-redirect`) sends the `code` and
      `state` back to your custom UI window via `window.opener.postMessage`.
  - Step 3 (`--step store`):
      Calls `POST dataConnector:acquireAndStoreRefreshToken` with `fullRedirectUri`
      so Gemini Enterprise exchanges the code and saves the refresh token in its Keystore.

Usage:
  # Step 1: Check if user has a valid token in GE Keystore (or get authUri)
  python 02_federated_connector_oauth/connector_oauth_flow.py --step check

  # Step 3: Store the authorization code returned by the popup into GE Keystore
  python 02_federated_connector_oauth/connector_oauth_flow.py --step store --code "<OAUTH_CODE>" --state "<STATE>"
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict
from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEFAULT_REDIRECT_URI = "https://vertexaisearch.cloud.google.com/oauth-redirect"


def get_user_bearer_token(explicit_token: str = "") -> str:
  """Returns the user's Google/STS bearer token from CLI/env or local gcloud CLI."""
  token = (explicit_token or os.environ.get("GOOGLE_USER_ACCESS_TOKEN", "")).strip()
  if token:
    return token
  return subprocess.check_output(
      ["gcloud", "auth", "print-access-token"],
      text=True,
      stdin=subprocess.DEVNULL,
      timeout=15,
  ).strip()


def connector_base_url(project_id: str, location: str, collection_id: str) -> str:
  host = (
      "https://discoveryengine.googleapis.com"
      if location == "global"
      else f"https://{location}-discoveryengine.googleapis.com"
  )
  return (
      f"{host}/v1alpha/projects/{project_id}/locations/{location}"
      f"/collections/{collection_id}/dataConnector"
  )


def check_connector_access_token(
    project_id: str,
    location: str,
    collection_id: str,
    user_bearer_token: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> Dict[str, Any]:
  """Step 1: Calls GET dataConnector:acquireAccessToken."""
  url = f"{connector_base_url(project_id, location, collection_id)}:acquireAccessToken"
  headers = {
      "Authorization": f"Bearer {user_bearer_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": project_id,
  }
  resp = requests.get(url, headers=headers, params={"redirectUri": redirect_uri}, timeout=20)
  try:
    data = resp.json()
  except ValueError:
    data = {"raw": resp.text}
  return {"status_code": resp.status_code, "url": url, "data": data}


def store_connector_refresh_token(
    project_id: str,
    location: str,
    collection_id: str,
    user_bearer_token: str,
    full_redirect_uri: str,
) -> Dict[str, Any]:
  """Step 3: Calls POST dataConnector:acquireAndStoreRefreshToken."""
  url = f"{connector_base_url(project_id, location, collection_id)}:acquireAndStoreRefreshToken"
  headers = {
      "Authorization": f"Bearer {user_bearer_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": project_id,
  }
  payload = {"fullRedirectUri": full_redirect_uri}
  resp = requests.post(url, headers=headers, json=payload, timeout=20)
  try:
    data = resp.json()
  except ValueError:
    data = {"raw": resp.text}
  return {"status_code": resp.status_code, "url": url, "data": data}


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Part 2: Federated Connector 3LO token check and Keystore save."
  )
  parser.add_argument(
      "--step",
      choices=["check", "store"],
      default="check",
      help="'check' calls :acquireAccessToken; 'store' calls :acquireAndStoreRefreshToken",
  )
  parser.add_argument("--project-id", default=os.environ.get("GCP_PROJECT_ID", ""))
  parser.add_argument(
      "--collection-id", default=os.environ.get("CONNECTOR_COLLECTION_ID", "")
  )
  parser.add_argument("--location", default=os.environ.get("CONNECTOR_LOCATION", "global"))
  parser.add_argument(
      "--redirect-uri",
      default=os.environ.get("CONNECTOR_REDIRECT_URI", DEFAULT_REDIRECT_URI),
  )
  parser.add_argument("--access-token", default="", help="User bearer token")
  parser.add_argument("--code", default="", help="OAuth authorization code from popup")
  parser.add_argument("--state", default="", help="Optional OAuth state from popup")
  parser.add_argument(
      "--full-redirect-uri",
      default="",
      help="Full redirect URI containing ?code=...&state=...",
  )
  args = parser.parse_args()

  if not args.project_id or not args.collection_id:
    sys.exit("Error: Set GCP_PROJECT_ID and CONNECTOR_COLLECTION_ID in .env or via CLI flags.")

  user_token = get_user_bearer_token(args.access_token)

  if args.step == "check":
    print("=== Part 2 (Step 1): GET dataConnector:acquireAccessToken ===")
    print(f"Project ID    : {args.project_id}")
    print(f"Collection ID : {args.collection_id} ({args.location})\n")
    res = check_connector_access_token(
        project_id=args.project_id,
        location=args.location,
        collection_id=args.collection_id,
        user_bearer_token=user_token,
        redirect_uri=args.redirect_uri,
    )
    print(f"HTTP Status: {res['status_code']}")
    print(json.dumps(res["data"], indent=2))
    if isinstance(res["data"], dict) and res["data"].get("authUri"):
      print("\n-> Action required: Open this authUri in a browser popup for user consent:")
      print(f"   {res['data']['authUri']}")
    return

  # Step 3: Store token in Keystore
  full_redirect = args.full_redirect_uri
  if not full_redirect:
    if not args.code:
      sys.exit("Error: For --step store, provide --code '<OAUTH_CODE>' or --full-redirect-uri.")
    full_redirect = f"{args.redirect_uri}?code={args.code}"
    if args.state:
      full_redirect += f"&state={args.state}"

  print("=== Part 2 (Step 3): POST dataConnector:acquireAndStoreRefreshToken ===")
  res = store_connector_refresh_token(
      project_id=args.project_id,
      location=args.location,
      collection_id=args.collection_id,
      user_bearer_token=user_token,
      full_redirect_uri=full_redirect,
  )
  print(f"HTTP Status: {res['status_code']}")
  print(json.dumps(res["data"], indent=2))


if __name__ == "__main__":
  main()
