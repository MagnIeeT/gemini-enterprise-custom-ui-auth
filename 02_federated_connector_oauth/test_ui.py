"""Local Web Server & Test UI for Part 2: Federated Connector 3LO Auth & :streamAssist Chat.

Serves `02_federated_connector_oauth/test_ui.html` at http://localhost:8502 and exposes
the three Customer Backend endpoints shown in the Google architecture flow (Steps 1-11):

  1. GET  /api/list  -> Calls `GetWidgetConfig` + `dataConnector:acquireAccessToken`
  2. POST /api/store -> Calls `dataConnector:acquireAndStoreRefreshToken` with `fullRedirectUri`
  3. POST /api/chat  -> Calls `assistants/default_assistant:streamAssist`

Usage:
  python3 02_federated_connector_oauth/test_ui.py
"""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

CURRENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CURRENT_DIR.parent
sys.path.insert(0, str(CURRENT_DIR))
sys.path.insert(0, str(REPO_ROOT / "01_user_auth_streamassist"))

from connector_oauth_flow import (  # pylint: disable=wrong-import-position
    acquire_connector_access_token,
    extract_Action_collection_id,
    get_engine_data_store_specs,
    get_engine_widget_config,
    store_connector_refresh_token,
)
from pattern_a_google_identity import (  # pylint: disable=wrong-import-position
    call_stream_assist,
    extract_reply_text,
    get_google_user_token,
)
from pattern_b_workforce_sts import (  # pylint: disable=wrong-import-position
    exchange_idp_token_for_sts_token,
    get_entra_id_token_via_browser_login,
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


def resolve_engine_and_location(auth_mode: str) -> tuple[str, str, str]:
  project_id = os.environ.get("GCP_PROJECT_ID", "")
  if auth_mode == "google":
    engine_id = os.environ.get("GE_GOOGLE_ID_ENGINE_ID", "")
    location = os.environ.get(
        "CONNECTOR_LOCATION", os.environ.get("GE_GOOGLE_ID_LOCATION", "global")
    )
  else:
    engine_id = os.environ.get("GE_WIF_ENGINE_ID", "")
    location = os.environ.get("GE_WIF_LOCATION", "us")
  return project_id, engine_id, location


class TestUIHandler(BaseHTTPRequestHandler):
  """HTTP handler serving test_ui.html and /api/list, /api/store, /api/chat."""

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

    if parsed.path == "/api/list":
      qs = parse_qs(parsed.query)
      auth_mode = qs.get("auth_mode", ["google"])[0]
      try:
        project_id, engine_id, location = resolve_engine_and_location(auth_mode)
        user_token = get_cached_user_token(auth_mode)
        widget_res = get_engine_widget_config(
            project_id=project_id,
            location=location,
            engine_id=engine_id,
            user_bearer_token=user_token,
            custom_domain=os.environ.get("CUSTOM_DOMAIN", "http://localhost:8502"),
        )
        if widget_res["status_code"] != 200:
          self._send_json(
              widget_res["status_code"],
              {
                  "project_id": project_id,
                  "engine_id": engine_id,
                  "location": location,
                  "auth_mode": auth_mode,
                  "error": widget_res["data"],
              },
          )
          return

        components = widget_res["data"].get("collectionComponents", [])
        connectors = []
        for comp in components:
          collection_id = comp.get("id", "")
          display_name = (
              comp.get("displayName")
              or comp.get("dataSourceEndUserDisplayName")
              or comp.get("dataSourceDisplayName")
              or collection_id
          )
          data_source = comp.get("dataSource", "first_party_or_custom")
          auth_state_obj = comp.get("connectorAuthState", {})
          auth_uri = auth_state_obj.get("authorizationUri", "")
          widget_auth_state = auth_state_obj.get(
              "authState",
              "AUTH_STATE_UNSPECIFIED (Needs 3LO)" if auth_uri else "NO_AUTH",
          )
          action_connector = comp.get("actionConnector", "")
          target_collection_id = (
              extract_Action_collection_id(action_connector) or collection_id
          )

          keystore_authorized = False
          keystore_status_text = "Not checked"
          if target_collection_id and target_collection_id != "default_collection":
            token_check = acquire_connector_access_token(
                project_id=project_id,
                location=location,
                collection_id=target_collection_id,
                user_bearer_token=user_token,
            )
            sc = token_check["status_code"]
            td = token_check["data"]
            if sc == 200 and isinstance(td, dict) and td.get("accessToken"):
              keystore_authorized = True
              keystore_status_text = "HTTP 200 OK"
            else:
              err_status = ""
              if isinstance(td, dict) and "error" in td:
                err_status = td["error"].get("status", "")
              keystore_status_text = f"HTTP {sc} {err_status}".strip()

          connectors.append({
              "collection_id": collection_id,
              "target_collection_id": target_collection_id,
              "display_name": display_name,
              "data_source": data_source,
              "widget_auth_state": widget_auth_state,
              "keystore_authorized": keystore_authorized,
              "keystore_status_text": keystore_status_text,
              "auth_uri": auth_uri,
          })

        self._send_json(
            200,
            {
                "project_id": project_id,
                "engine_id": engine_id,
                "location": location,
                "auth_mode": auth_mode,
                "connectors": connectors,
            },
        )
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(500, {"error": str(exc)})
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

    if parsed.path == "/api/store":
      auth_mode = body.get("auth_mode", "google")
      collection_id = body.get("collection_id", "")
      full_redirect_uri = body.get("full_redirect_uri", "")
      try:
        project_id, _, location = resolve_engine_and_location(auth_mode)
        user_token = get_cached_user_token(auth_mode)
        store_res = store_connector_refresh_token(
            project_id=project_id,
            location=location,
            collection_id=collection_id,
            user_bearer_token=user_token,
            full_redirect_uri=full_redirect_uri,
        )
        verify_res = None
        if store_res["status_code"] == 200:
          verify_res = acquire_connector_access_token(
              project_id=project_id,
              location=location,
              collection_id=collection_id,
              user_bearer_token=user_token,
          )
        self._send_json(
            200,
            {
                "store_status": store_res["status_code"],
                "store_response": store_res["data"],
                "verify_status": verify_res["status_code"] if verify_res else None,
            },
        )
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(500, {"error": str(exc)})
      return

    if parsed.path == "/api/chat":
      auth_mode = body.get("auth_mode", "google")
      query_text = body.get("query", "")
      try:
        project_id, engine_id, location = resolve_engine_and_location(auth_mode)
        user_token = get_cached_user_token(auth_mode)
        data_store_specs = get_engine_data_store_specs(
            project_id=project_id,
            location=location,
            engine_id=engine_id,
            user_bearer_token=user_token,
        )
        chat_res = call_stream_assist(
            project_id=project_id,
            location=location,
            engine_id=engine_id,
            user_access_token=user_token,
            query_text=query_text,
            data_store_specs=data_store_specs,
        )
        reply_text = (
            extract_reply_text(chat_res["response"])
            if chat_res["status_code"] == 200
            else ""
        )
        self._send_json(
            200,
            {
                "status_code": chat_res["status_code"],
                "data_store_specs": data_store_specs,
                "reply_text": reply_text,
                "raw_response": chat_res["response"],
            },
        )
      except Exception as exc:  # pylint: disable=broad-except
        self._send_json(500, {"error": str(exc)})
      return

    self._send_json(404, {"error": "Not found"})


def main() -> None:
  parser = argparse.ArgumentParser(description="Run local Test UI for Part 2 Connector Auth & Chat.")
  parser.add_argument("--port", type=int, default=8502, help="Local port (default: 8502)")
  args = parser.parse_args()

  server = HTTPServer(("127.0.0.1", args.port), TestUIHandler)
  print("======================================================================")
  print(" Gemini Enterprise — Federated Connector 3LO & Chat Test UI")
  print(f" Open in your browser: http://localhost:{args.port}")
  print("======================================================================")
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\nShutting down Test UI server.")
    server.server_close()


if __name__ == "__main__":
  main()
