"""Pattern 1A: Authenticate to Gemini Enterprise (:streamAssist) with Google Identity.

When to use:
  Both your custom UI and your Gemini Enterprise engine use Google Identity
  (Google Workspace or Cloud Identity).

How it works:
  1. Your custom UI signs the employee in via Google OAuth 2.0 (or locally via
     `gcloud auth print-access-token` when testing as a Google user).
  2. The resulting Google OAuth user access token is sent directly to
     `assistants/default_assistant:streamAssist`.

Usage:
  python 01_user_auth_streamassist/pattern_a_google_identity.py
  python 01_user_auth_streamassist/pattern_a_google_identity.py --browser-login
  python 01_user_auth_streamassist/pattern_a_google_identity.py --agent-id "$GE_GOOGLE_ID_AGENT_ID" --query "What is our grid operations SLA policy?"
"""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional
import urllib.parse
import webbrowser
from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_REDIRECT_URI = "http://localhost:8085/callback"
DEFAULT_SCOPES = "openid email profile https://www.googleapis.com/auth/cloud-platform"


def login_with_google_browser(
    client_id: str,
    client_secret: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> str:
  """Runs an interactive 'Sign in with Google' browser OAuth 2.0 flow and returns the user access token."""
  if not client_id or not client_secret:
    raise RuntimeError(
        "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env to use --browser-login."
    )

  parsed_redirect = urllib.parse.urlparse(redirect_uri)
  port = parsed_redirect.port or 8085
  captured_code: Dict[str, Optional[str]] = {"code": None, "error": None}

  class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # pylint: disable=invalid-name
      qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
      if "code" in qs:
        captured_code["code"] = qs["code"][0]
        body = b"<h2>Google Sign-In successful! You can close this tab and return to your terminal.</h2>"
      else:
        captured_code["error"] = qs.get("error", ["unknown_error"])[0]
        body = b"<h2>Google Sign-In failed. Check terminal output.</h2>"
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.end_headers()
      self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
      return

  params = {
      "client_id": client_id,
      "redirect_uri": redirect_uri,
      "response_type": "code",
      "scope": DEFAULT_SCOPES,
      "access_type": "offline",
      "prompt": "consent",
  }
  consent_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
  print("\n1. Open this URL in your browser to Sign in with Google:")
  print(f"   {consent_url}\n")
  try:
    webbrowser.open(consent_url)
  except Exception:
    pass

  print(f"2. Waiting for OAuth redirect on {redirect_uri} ...")
  server = HTTPServer(("localhost", port), OAuthCallbackHandler)
  server.handle_request()
  server.server_close()

  if captured_code["error"] or not captured_code["code"]:
    raise RuntimeError(f"Google OAuth login failed: {captured_code['error']}")

  print("3. Exchanging authorization code for Google user access token ...")
  token_resp = requests.post(
      GOOGLE_TOKEN_URL,
      data={
          "code": captured_code["code"],
          "client_id": client_id,
          "client_secret": client_secret,
          "redirect_uri": redirect_uri,
          "grant_type": "authorization_code",
      },
      timeout=20,
  )
  if not token_resp.ok:
    raise RuntimeError(
        f"Token exchange failed (HTTP {token_resp.status_code}): {token_resp.text}"
    )
  token_data = token_resp.json()
  print("   ✓ Received Google OAuth user access token!\n")
  return token_data["access_token"]


def get_google_user_token(
    explicit_token: str = "",
    browser_login: bool = False,
) -> str:
  """Returns a Google OAuth user access token via browser login, CLI flag, or gcloud."""
  if browser_login:
    return login_with_google_browser(
        client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
        redirect_uri=os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip(),
    )
  token = explicit_token.strip()
  if token:
    return token
  try:
    return subprocess.check_output(
        ["gcloud", "auth", "print-access-token"],
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
    ).strip()
  except Exception as exc:
    raise RuntimeError(
        "Failed to obtain a Google user token via `gcloud auth print-access-token`. "
        "Run with `--browser-login` or run `gcloud auth login`."
    ) from exc


def extract_reply_text(chunks: Any) -> str:
  """Extracts assistant text replies from the :streamAssist JSON response array."""
  if not isinstance(chunks, list):
    return json.dumps(chunks, indent=2)
  texts: List[str] = []
  for chunk in chunks:
    for reply in chunk.get("answer", {}).get("replies", []):
      content = reply.get("groundedContent", {}).get("content", {})
      if content.get("thought"):
        continue
      text = content.get("text")
      if text:
        texts.append(text)
    summary = (
        chunk.get("reply", {})
        .get("reply", {})
        .get("summary", {})
        .get("summaryText")
    )
    if summary:
      texts.append(summary)
  return "".join(texts).strip()


def call_stream_assist(
    project_id: str,
    location: str,
    engine_id: str,
    user_access_token: str,
    query_text: str,
    agent_id: str = "",
) -> Dict[str, Any]:
  """Calls Gemini Enterprise :streamAssist with a Google Identity user bearer token."""
  host = (
      "discoveryengine.googleapis.com"
      if location == "global"
      else f"{location}-discoveryengine.googleapis.com"
  )
  endpoint = (
      f"https://{host}/v1alpha/projects/{project_id}/locations/{location}/"
      f"collections/default_collection/engines/{engine_id}/"
      f"assistants/default_assistant:streamAssist"
  )

  payload: Dict[str, Any] = {"query": {"text": query_text}}
  if agent_id.strip():
    payload["answerGenerationMode"] = "AGENT"
    payload["agentsSpec"] = {"agentSpecs": [{"agentId": agent_id.strip()}]}

  headers = {
      "Authorization": f"Bearer {user_access_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": project_id,
  }

  resp = requests.post(endpoint, headers=headers, json=payload, timeout=90)
  try:
    body = resp.json()
  except ValueError:
    body = resp.text

  return {
      "status_code": resp.status_code,
      "endpoint": endpoint,
      "payload": payload,
      "response": body,
  }


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Pattern 1A: Call Gemini Enterprise :streamAssist using Google Identity."
  )
  parser.add_argument("--project-id", default=os.environ.get("GCP_PROJECT_ID", ""))
  parser.add_argument("--engine-id", default=os.environ.get("GE_GOOGLE_ID_ENGINE_ID", ""))
  parser.add_argument("--location", default=os.environ.get("GE_GOOGLE_ID_LOCATION", "global"))
  parser.add_argument("--agent-id", default="")
  parser.add_argument("--query", default=os.environ.get("QUERY", "What are our SLA policies?"))
  parser.add_argument("--access-token", default="", help="Optional Google OAuth user access token")
  parser.add_argument(
      "--browser-login",
      action="store_true",
      help="Run interactive 'Sign in with Google' browser OAuth 2.0 flow to obtain user access token",
  )
  parser.add_argument("--raw", action="store_true", help="Print full raw JSON response")
  args = parser.parse_args()

  if not args.project_id or not args.engine_id:
    sys.exit("Error: Set GCP_PROJECT_ID and GE_GOOGLE_ID_ENGINE_ID in .env or via CLI flags.")

  token = get_google_user_token(args.access_token, browser_login=args.browser_login)
  print("=== Pattern 1A: Pure Google Identity -> :streamAssist ===")
  print(f"Project ID : {args.project_id}")
  print(f"Engine ID  : {args.engine_id} ({args.location})")
  print(f"Target     : {'Agent ID ' + args.agent_id if args.agent_id else 'Default Assistant'}")
  print(f"Query      : {args.query}\n")

  result = call_stream_assist(
      project_id=args.project_id,
      location=args.location,
      engine_id=args.engine_id,
      user_access_token=token,
      query_text=args.query,
      agent_id=args.agent_id,
  )

  print(f"HTTP Status: {result['status_code']}")
  if result["status_code"] != 200:
    print(json.dumps(result["response"], indent=2))
    sys.exit(1)

  reply_text = extract_reply_text(result["response"])
  print("\n--- Assistant Reply ---")
  print(reply_text or "(Empty text reply — pass --raw to inspect full streamAssist chunks)")

  if args.raw:
    print("\n--- Full Raw JSON Response ---")
    print(json.dumps(result["response"], indent=2))


if __name__ == "__main__":
  main()
