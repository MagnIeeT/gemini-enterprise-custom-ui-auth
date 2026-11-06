"""Local Web Server & Test UI for Part 3: ADK Agent On-Demand 3LO via :streamAssist & :validateUserAuthorization.

Serves `03_adk_agent_oauth/test_ui.html` at http://localhost:8502 and exposes:
  1. POST /api/chat     -> Calls `assistants/default_assistant:streamAssist` targeting `agentId`
  2. POST /api/validate -> Calls `{session}:validateUserAuthorization` with `userIdValidationState`
                           and `downstreamConnector`, finalizing the credential in Auth Manager Vault.

Usage:
  .venv/bin/python 03_adk_agent_oauth/test_ui.py
"""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict
from urllib.parse import urlparse

from dotenv import load_dotenv

CURRENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CURRENT_DIR.parent
sys.path.insert(0, str(CURRENT_DIR))
sys.path.insert(0, str(REPO_ROOT / "01_user_auth_streamassist"))

from pattern_a_google_identity import get_google_user_token  # pylint: disable=wrong-import-position
from pattern_b_workforce_sts import (  # pylint: disable=wrong-import-position
    exchange_idp_token_for_sts_token,
    get_entra_id_token_via_browser_login,
)
from query_registered_agent import (  # pylint: disable=wrong-import-position
    parse_validation_redirect_uri,
    query_adk_agent_stream_assist,
    revoke_stored_authorizations,
    validate_user_authorization,
)

load_dotenv(REPO_ROOT / ".env")

HTML_FILE = CURRENT_DIR / "test_ui.html"
_TOKEN_CACHE: Dict[str, str] = {}


def get_cached_user_token(auth_mode: str) -> str:
  """Returns a Bearer token for the selected auth_mode ('google' or 'wif')."""
  if auth_mode in _TOKEN_CACHE and _TOKEN_CACHE[auth_mode]:
    return _TOKEN_CACHE[auth_mode]

  if auth_mode == "google":
    token = get_google_user_token(browser_login=False)
  else:
    pool_id = os.environ.get("WIF_POOL_ID", "")
    provider_id = os.environ.get("WIF_PROVIDER_ID", "")
    idp_token = os.environ.get("IDP_ID_TOKEN", "").strip()
    if not idp_token:
      idp_token = get_entra_id_token_via_browser_login(
          tenant_id=os.environ.get("ENTRA_TENANT_ID", ""),
          client_id=os.environ.get("ENTRA_CLIENT_ID", ""),
      )
    sts_data = exchange_idp_token_for_sts_token(
        workforce_pool_id=pool_id,
        provider_id=provider_id,
        idp_token=idp_token,
    )
    token = sts_data["access_token"]

  _TOKEN_CACHE[auth_mode] = token
  return token


def resolve_engine_location_agent(auth_mode: str) -> tuple[str, str, str, str]:
  project_id = os.environ.get("GCP_PROJECT_ID", "")
  if auth_mode == "wif":
    engine_id = os.environ.get("GE_WIF_ENGINE_ID", "")
    location = os.environ.get("GE_WIF_LOCATION", "us")
    agent_id = os.environ.get("GE_WIF_AGENT_ID", "")
  else:
    engine_id = os.environ.get("GE_GOOGLE_ID_ENGINE_ID", "")
    location = os.environ.get("GE_GOOGLE_ID_LOCATION", "global")
    agent_id = os.environ.get("GE_GOOGLE_ID_AGENT_ID", "")
  return project_id, engine_id, location, agent_id


class Part3TestUIHandler(BaseHTTPRequestHandler):
  """HTTP handler serving 03_adk_agent_oauth/test_ui.html, /api/chat, and /api/validate."""

  def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    self.send_response(status_code)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def do_GET(self) -> None:  # pylint: disable=invalid-name
    parsed = urlparse(self.path)
    if parsed.path in ("/", "/index.html", "/test_ui.html"):
      content = HTML_FILE.read_bytes()
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.send_header("Content-Length", str(len(content)))
      self.end_headers()
      self.wfile.write(content)
      return

    if parsed.path == "/validateUserId":
      # Optional direct callback handler if continue_uri points to http://localhost:8502/validateUserId
      callback_html = f"""<!DOCTYPE html>
<html><body>
<p>Completing authorization...</p>
<script>
  if (window.opener) {{
    window.opener.postMessage({{ fullRedirectUrl: window.location.href }}, "*");
    window.close();
  }}
</script>
</body></html>""".encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.send_header("Content-Length", str(len(callback_html)))
      self.end_headers()
      self.wfile.write(callback_html)
      return

    self._send_json(404, {"error": "Not found"})

  def do_POST(self) -> None:  # pylint: disable=invalid-name
    parsed = urlparse(self.path)
    length = int(self.headers.get("Content-Length", "0"))
    raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
    try:
      body = json.loads(raw_body)
    except ValueError:
      body = {}

    if parsed.path == "/api/chat":
      auth_mode = body.get("auth_mode", "google")
      query_text = body.get("query", "")
      session_name = body.get("session", "") or None
      try:
        project_id, engine_id, location, agent_id = resolve_engine_location_agent(auth_mode)
        user_token = get_cached_user_token(auth_mode)
        res = query_adk_agent_stream_assist(
            project_id=project_id,
            location=location,
            engine_id=engine_id,
            agent_id=agent_id,
            user_bearer_token=user_token,
            query_text=query_text,
            session_name=session_name,
        )
        self._send_json(
            200,
            {
                "project_id": project_id,
                "engine_id": engine_id,
                "location": location,
                "agent_id": agent_id,
                **res,
            },
        )
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(500, {"error": str(exc)})
      return

    if parsed.path == "/api/validate":
      auth_mode = body.get("auth_mode", "google")
      session_name = body.get("session", "")
      downstream_connector = body.get("downstream_connector", "")
      full_redirect_uri = body.get("full_redirect_uri", "")
      user_id_validation_state = body.get("user_id_validation_state", "")
      try:
        project_id, _, location, _ = resolve_engine_location_agent(auth_mode)
        user_token = get_cached_user_token(auth_mode)
        if full_redirect_uri:
          parsed_redir = parse_validation_redirect_uri(
              full_redirect_uri, expected_display_name=downstream_connector
          )
          user_id_validation_state = (
              user_id_validation_state or parsed_redir["user_id_validation_state"]
          )
          downstream_connector = (
              downstream_connector or parsed_redir["downstream_connector"]
          )

        val_res = validate_user_authorization(
            project_id=project_id,
            location=location,
            session_name=session_name,
            user_id_validation_state=user_id_validation_state,
            downstream_connector=downstream_connector,
            user_bearer_token=user_token,
        )
        self._send_json(
            200,
            {
                "validate_status": val_res["status_code"],
                "validate_response": val_res["data"],
                "session": session_name,
                "downstream_connector": downstream_connector,
            },
        )
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(500, {"error": str(exc)})
      return

    if parsed.path == "/api/revoke":
      try:
        project_id = os.environ.get("GCP_PROJECT_ID", "")
        provider_loc = os.environ.get("AUTH_PROVIDER_LOCATION", "us-central1")
        user_token = get_google_user_token(browser_login=False)
        providers = body.get("providers") or [
            os.environ.get("GOOGLE_AUTH_PROVIDER_ID", "google-workspace-provider"),
            os.environ.get("SHAREPOINT_AUTH_PROVIDER_ID", "sharepoint-provider"),
        ]
        revoked = revoke_stored_authorizations(
            project_id=project_id,
            auth_provider_location=provider_loc,
            user_bearer_token=user_token,
            providers=providers,
        )
        self._send_json(200, {"revoked": revoked})
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(500, {"error": str(exc)})
      return

    self._send_json(404, {"error": "Not found"})


def main() -> None:
  parser = argparse.ArgumentParser(description="Run local Test UI for Part 3 ADK Agent 3LO & Chat.")
  parser.add_argument("--port", type=int, default=8503, help="Local port (default: 8503)")
  args = parser.parse_args()

  server = HTTPServer(("127.0.0.1", args.port), Part3TestUIHandler)
  print("======================================================================")
  print(" Gemini Enterprise — Part 3: ADK Agent 3LO & :streamAssist Test UI")
  print(f" Open in your browser: http://localhost:{args.port}")
  print("======================================================================")
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\nShutting down Test UI server.")
    server.server_close()


if __name__ == "__main__":
  main()
