"""Pattern 1C: Hybrid Login (External IdP UI -> Keyless Domain-Wide Delegation -> :streamAssist).

When to use:
  Your custom UI signs employees in with an external IdP (such as Microsoft Entra ID
  or Okta), while your Gemini Enterprise engine is configured with Google Identity
  (Google Workspace / Cloud Identity).

How it works:
  1. Your backend verifies the employee's external IdP JWT (or takes their mapped
     Google Workspace email `user@company.com`).
  2. Using the IAM Credentials API (`iam.Signer` / `:signJwt`), your backend uses a
     broker service account with Domain-Wide Delegation (DWD) to mint a short-lived
     Google OAuth access token impersonating that specific user (`sub = user@company.com`)
     — with zero downloaded service account JSON keys.
  3. The impersonated Google user token is sent to `:streamAssist`, preserving full
     per-user ACLs and agent RBAC.

Usage:
  python 01_user_auth_streamassist/pattern_c_hybrid_keyless_dwd.py
  python 01_user_auth_streamassist/pattern_c_hybrid_keyless_dwd.py --user-email "user@your-workspace-domain.com" --query "What are our SLA policies?"
  python 01_user_auth_streamassist/pattern_c_hybrid_keyless_dwd.py --agent-id "$GE_GOOGLE_ID_AGENT_ID" --query "Get my open ServiceNow incidents"
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from google.auth import default as google_auth_default
from google.auth import iam
from google.auth.transport import requests as google_requests
from google.oauth2 import credentials as oauth2_credentials
from google.oauth2 import service_account
import jwt
import requests

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def extract_email_from_idp_jwt(idp_jwt: str) -> str:
  """Extracts the employee's email (`preferred_username`, `upn`, or `email`) from an IdP JWT."""
  claims = jwt.decode(idp_jwt, options={"verify_signature": False})
  email = claims.get("preferred_username") or claims.get("upn") or claims.get("email")
  if not email:
    raise ValueError("No 'preferred_username', 'upn', or 'email' claim found in IdP JWT.")
  return str(email).strip()


def mint_keyless_dwd_user_token(
    service_account_email: str,
    target_user_email: str,
) -> str:
  """Mints a Google OAuth user token via Keyless Domain-Wide Delegation (IAM :signJwt)."""
  auth_request = google_requests.Request()
  source_credentials: Optional[Any] = None

  # On a developer workstation, use the active gcloud CLI token if ADC is not set
  if not os.environ.get("K_SERVICE"):
    try:
      gcloud_token = subprocess.check_output(
          ["gcloud", "auth", "print-access-token"],
          text=True,
          stdin=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
          timeout=15,
      ).strip()
      if gcloud_token:
        source_credentials = oauth2_credentials.Credentials(token=gcloud_token)
    except Exception:
      pass

  if not source_credentials:
    source_credentials, _ = google_auth_default(scopes=SCOPES)

  signer = iam.Signer(auth_request, source_credentials, service_account_email)
  dwd_credentials = service_account.Credentials(
      signer=signer,
      service_account_email=service_account_email,
      token_uri=TOKEN_URI,
      scopes=SCOPES,
      subject=target_user_email,
  )
  dwd_credentials.refresh(auth_request)
  return str(dwd_credentials.token)


def extract_reply_text(chunks: Any) -> str:
  """Extracts assistant text replies from the :streamAssist JSON response array."""
  if not isinstance(chunks, list):
    return json.dumps(chunks, indent=2)
  texts: List[str] = []
  for chunk in chunks:
    for reply in chunk.get("answer", {}).get("replies", []):
      text = (
          reply.get("groundedContent", {})
          .get("content", {})
          .get("text")
      )
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
    impersonated_user_token: str,
    query_text: str,
    agent_id: str = "",
) -> Dict[str, Any]:
  """Calls Gemini Enterprise :streamAssist with a DWD-impersonated user token."""
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
    payload["agentsSpec"] = {"agentSpecs": [{"agentId": agent_id.strip()}]}

  headers = {
      "Authorization": f"Bearer {impersonated_user_token}",
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
      description="Pattern 1C: Mint a user token via Keyless DWD (:signJwt) and call :streamAssist."
  )
  parser.add_argument("--project-id", default=os.environ.get("GCP_PROJECT_ID", ""))
  parser.add_argument("--engine-id", default=os.environ.get("GE_GOOGLE_ID_ENGINE_ID", ""))
  parser.add_argument("--location", default=os.environ.get("GE_GOOGLE_ID_LOCATION", "global"))
  parser.add_argument("--agent-id", default="")
  parser.add_argument(
      "--service-account", default=os.environ.get("DWD_SERVICE_ACCOUNT_EMAIL", "")
  )
  parser.add_argument("--user-email", default=os.environ.get("DWD_TARGET_USER_EMAIL", ""))
  parser.add_argument(
      "--idp-token",
      default="",
      help="Optional Entra ID / Okta JWT to extract the user's email from",
  )
  parser.add_argument("--query", default=os.environ.get("QUERY", "What are our SLA policies?"))
  parser.add_argument("--raw", action="store_true", help="Print full raw JSON response")
  args = parser.parse_args()

  target_email = (
      extract_email_from_idp_jwt(args.idp_token) if args.idp_token else args.user_email
  )
  if not args.service_account or not target_email:
    sys.exit(
        "Error: Set DWD_SERVICE_ACCOUNT_EMAIL and DWD_TARGET_USER_EMAIL in .env "
        "or pass --service-account and --user-email."
    )

  print("=== Pattern 1C: Hybrid Login (Keyless DWD :signJwt) -> :streamAssist ===")
  print(f"Broker SA         : {args.service_account}")
  print(f"Impersonated User : {target_email}")
  print(f"Engine ID         : {args.engine_id} ({args.location})")
  print(f"Target            : {'Agent ID ' + args.agent_id if args.agent_id else 'Default Assistant'}")
  print(f"Query             : {args.query}\n")

  print(f"1. Minting Google OAuth token for '{target_email}' via IAM Credentials API (:signJwt) ...")
  user_token = mint_keyless_dwd_user_token(
      service_account_email=args.service_account,
      target_user_email=target_email,
  )
  print(f"   ✓ Minted user token ({user_token[:20]}...)")

  print("2. Calling Gemini Enterprise :streamAssist ...")
  result = call_stream_assist(
      project_id=args.project_id,
      location=args.location,
      engine_id=args.engine_id,
      impersonated_user_token=user_token,
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
