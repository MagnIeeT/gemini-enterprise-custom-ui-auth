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
  python 01_user_auth_streamassist/pattern_b_workforce_sts.py --idp-token "<ENTRA_OR_OKTA_JWT>"
  python 01_user_auth_streamassist/pattern_b_workforce_sts.py --idp-token "<JWT>" --agent-id "$GE_WIF_AGENT_ID" --query "Get my open ServiceNow incidents"
"""

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List
from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"


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
  parser.add_argument("--idp-token", default=os.environ.get("EXTERNAL_IDP_TOKEN", ""))
  parser.add_argument("--query", default=os.environ.get("QUERY", "What are our SLA policies?"))
  parser.add_argument("--raw", action="store_true", help="Print full raw JSON response")
  args = parser.parse_args()

  if not args.idp_token:
    sys.exit(
        "Error: Provide an external IdP OIDC token via --idp-token '<JWT>' "
        "or set EXTERNAL_IDP_TOKEN in .env."
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
      idp_token=args.idp_token,
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
