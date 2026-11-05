"""Pattern 1B: Authenticate to Gemini Enterprise (:streamAssist) via Workforce Identity Federation (STS).

When to use:
  Both your custom UI and your Gemini Enterprise engine use an external identity
  provider (Okta, Microsoft Entra ID, or Ping Identity) via a Google Cloud
  Workforce Identity Pool (`workforcePools`).

How it works:
  1. Your custom UI signs the employee in with your external IdP and receives an
     OIDC ID token (or SAML assertion).
  2. This script exchanges that external IdP token at Google Security Token Service
     (`https://sts.googleapis.com/v1/token`) for a federated Google access token.
  3. The federated Google access token is sent to `:streamAssist`.

Usage:
  # Option 1: Interactive "Sign in with Microsoft Entra ID" browser login (PKCE)
  python 01_user_auth_streamassist/pattern_b_workforce_sts.py --browser-login

  # Option 2: Pass an existing external IdP OIDC JWT directly
  python 01_user_auth_streamassist/pattern_b_workforce_sts.py --idp-token "<ENTRA_OR_OKTA_JWT>"

  # Option 3: Route the query directly to a registered ADK agent (--agent-id)
  python 01_user_auth_streamassist/pattern_b_workforce_sts.py --browser-login --agent-id "$GE_WIF_AGENT_ID" --query "What is our grid operations SLA policy?"
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any, Dict, List, Optional
import urllib.parse
import webbrowser
from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"


def get_entra_id_token_via_browser_login(
    tenant_id: str,
    client_id: str,
    redirect_uri: str = "http://localhost:8000",
    client_secret: str = "",
) -> str:
  """Runs an interactive 'Sign in with Microsoft Entra ID' OAuth 2.0 PKCE flow in the browser."""
  if not tenant_id or not client_id:
    raise RuntimeError(
        "Missing ENTRA_TENANT_ID or ENTRA_CLIENT_ID in .env (required for --browser-login)."
    )

  parsed_redirect = urllib.parse.urlparse(redirect_uri)
  port = parsed_redirect.port or 8000
  state = secrets.token_urlsafe(16)
  code_verifier = secrets.token_urlsafe(64)
  code_challenge = (
      base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
      .decode("ascii")
      .rstrip("=")
  )
  captured: Dict[str, Optional[str]] = {"code": None, "error": None}

  class _CallbackHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self) -> None:  # pylint: disable=invalid-name
      parsed = urllib.parse.urlparse(self.path)
      params = urllib.parse.parse_qs(parsed.query)
      if "error" in params:
        captured["error"] = (
            f"{params['error'][0]}: {params.get('error_description', [''])[0]}"
        )
      elif "code" in params:
        if params.get("state", [""])[0] != state:
          captured["error"] = "state_mismatch"
        else:
          captured["code"] = params["code"][0]
      else:
        self.send_response(204)
        self.end_headers()
        return

      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.end_headers()
      msg = (
          "<h2>&#10003; Signed in with Microsoft Entra ID!</h2>"
          "<p>You can close this browser tab and return to your terminal.</p>"
          if captured["code"]
          else f"<h2>Sign-in failed: {captured['error']}</h2>"
      )
      self.wfile.write(
          f"<html><body style='font-family:sans-serif;padding:2rem;'>{msg}</body></html>".encode(
              "utf-8"
          )
      )

    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
      return

  auth_params = {
      "client_id": client_id,
      "redirect_uri": redirect_uri,
      "response_type": "code",
      "scope": "openid profile email",
      "response_mode": "query",
      "state": state,
      "code_challenge": code_challenge,
      "code_challenge_method": "S256",
      "prompt": "select_account",
  }
  auth_url = (
      f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?"
      f"{urllib.parse.urlencode(auth_params)}"
  )

  print(f"Opening browser for 'Sign in with Microsoft Entra ID' (listening on {redirect_uri})...")
  print(f"If the browser does not open automatically, visit:\n  {auth_url}\n")
  webbrowser.open(auth_url)

  with http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler) as httpd:
    while captured["code"] is None and captured["error"] is None:
      httpd.handle_request()

  if captured["error"] or not captured["code"]:
    raise RuntimeError(f"Microsoft Entra ID OAuth login failed: {captured['error']}")

  token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
  token_data = {
      "client_id": client_id,
      "code": captured["code"],
      "redirect_uri": redirect_uri,
      "grant_type": "authorization_code",
      "code_verifier": code_verifier,
      "scope": "openid profile email",
  }
  if client_secret:
    token_data["client_secret"] = client_secret

  origin = f"{parsed_redirect.scheme}://{parsed_redirect.netloc}"
  token_resp = requests.post(
      token_url,
      data=token_data,
      headers={"Origin": origin},
      timeout=20,
  )
  if not token_resp.ok:
    # Retry without Origin header in case the Entra app platform is Web/PublicClient instead of SPA
    token_resp = requests.post(token_url, data=token_data, timeout=20)
  if not token_resp.ok:
    raise RuntimeError(
        f"Entra ID token exchange failed (HTTP {token_resp.status_code}): {token_resp.text}"
    )

  tokens = token_resp.json()
  id_token = tokens.get("id_token", "")
  if not id_token:
    raise RuntimeError(f"No id_token returned from Entra ID: {tokens}")
  print("✓ Successfully obtained Entra ID OIDC id_token via browser login.\n")
  return id_token


def exchange_idp_token_for_sts_token(
    workforce_pool_id: str,
    provider_id: str,
    idp_token: str,
    subject_token_type: str = "urn:ietf:params:oauth:token-type:id_token",
) -> Dict[str, Any]:
  """Exchanges an external IdP OIDC/SAML token for a Google STS federated access token."""
  audience = (
      f"//iam.googleapis.com/locations/global/workforcePools/{workforce_pool_id}/"
      f"providers/{provider_id}"
  )
  payload = {
      "audience": audience,
      "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
      "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token",
      "scope": "https://www.googleapis.com/auth/cloud-platform",
      "subjectTokenType": subject_token_type,
      "subjectToken": idp_token.strip(),
  }
  resp = requests.post(STS_TOKEN_URL, json=payload, timeout=20)
  if not resp.ok:
    raise RuntimeError(
        f"Google STS token exchange failed (HTTP {resp.status_code}): {resp.text}"
    )
  return resp.json()


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
    federated_access_token: str,
    query_text: str,
    agent_id: str = "",
) -> Dict[str, Any]:
  """Calls Gemini Enterprise :streamAssist with a Workforce Identity STS bearer token."""
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
      "Authorization": f"Bearer {federated_access_token}",
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
      "response": body,
  }


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Pattern 1B: Exchange external IdP token at Google STS and call :streamAssist."
  )
  parser.add_argument("--project-id", default=os.environ.get("GCP_PROJECT_ID", ""))
  parser.add_argument("--pool-id", default=os.environ.get("WIF_POOL_ID", ""))
  parser.add_argument("--provider-id", default=os.environ.get("WIF_PROVIDER_ID", ""))
  parser.add_argument("--engine-id", default=os.environ.get("GE_WIF_ENGINE_ID", ""))
  parser.add_argument("--location", default=os.environ.get("GE_WIF_LOCATION", "us"))
  parser.add_argument("--agent-id", default="")
  parser.add_argument("--idp-token", default="", help="External IdP OIDC ID token (JWT)")
  parser.add_argument(
      "--browser-login",
      action="store_true",
      help="Open browser for interactive 'Sign in with Microsoft Entra ID' OIDC PKCE flow",
  )
  parser.add_argument(
      "--redirect-uri",
      default=os.environ.get("ENTRA_REDIRECT_URI", "http://localhost:8000"),
      help="Local redirect URI registered in your Entra ID App Registration (default: http://localhost:8000)",
  )
  parser.add_argument("--query", default=os.environ.get("QUERY", "What are our SLA policies?"))
  parser.add_argument("--raw", action="store_true", help="Print full raw JSON response")
  args = parser.parse_args()

  idp_token = args.idp_token.strip()
  if args.browser_login:
    idp_token = get_entra_id_token_via_browser_login(
        tenant_id=os.environ.get("ENTRA_TENANT_ID", ""),
        client_id=os.environ.get("ENTRA_CLIENT_ID", ""),
        redirect_uri=args.redirect_uri,
        client_secret=os.environ.get("ENTRA_CLIENT_SECRET", ""),
    )

  if not idp_token:
    sys.exit(
        "Error: Provide --browser-login to sign in with Entra ID in your browser, "
        "or pass an external IdP OIDC token via --idp-token '<JWT>'."
    )

  print("=== Pattern 1B: Workforce Identity Federation (STS) -> :streamAssist ===")
  print(f"Workforce Pool : {args.pool_id} (Provider: {args.provider_id})")
  print(f"Engine ID      : {args.engine_id} ({args.location})")
  print(f"Target         : {'Agent ID ' + args.agent_id if args.agent_id else 'Default Assistant'}")
  print(f"Query          : {args.query}\n")

  print("1. Exchanging external IdP token at https://sts.googleapis.com/v1/token ...")
  sts_data = exchange_idp_token_for_sts_token(
      workforce_pool_id=args.pool_id,
      provider_id=args.provider_id,
      idp_token=idp_token,
  )
  federated_token = sts_data["access_token"]
  print(f"   ✓ Received federated STS access token (expires_in={sts_data.get('expires_in')}s)")

  print("2. Calling Gemini Enterprise :streamAssist ...")
  result = call_stream_assist(
      project_id=args.project_id,
      location=args.location,
      engine_id=args.engine_id,
      federated_access_token=federated_token,
      query_text=args.query,
      agent_id=args.agent_id,
  )

  print(f"   HTTP Status: {result['status_code']}")
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
