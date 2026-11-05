"""Pattern 1C: Direct Workload Identity Federation (Entra ID -> Google STS -> :streamAssist, Zero Service Account).

When to use:
  A backend service, bot, or workload needs to call Gemini Enterprise `:streamAssist`
  using a project-scoped Workload Identity Pool (`workloadIdentityPools`) directly—
  with ZERO Service Accounts and ZERO Service Account keys.

How it works:
  1. Your workload obtains a JWT from Microsoft Entra ID (using OAuth 2.0 `client_credentials`).
  2. Exchanges that Entra JWT at Google Security Token Service (`https://sts.googleapis.com/v1/token`)
     against your project's Workload Identity Pool (`projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/{POOL_ID}/providers/{PROVIDER_ID}`).
  3. Calls `:streamAssist` directly using the returned Google STS federated access token.

Usage:
  python 01_user_auth_streamassist/pattern_c_workload_identity.py --query "What are our Grid Operations SLA policies?"
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


def get_entra_workload_jwt(
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> str:
  """Obtains a JWT from Microsoft Entra ID using OAuth 2.0 client_credentials."""
  if not tenant_id or not client_id or not client_secret:
    raise RuntimeError(
        "Missing ENTRA_TENANT_ID, WORKLOAD_ENTRA_CLIENT_ID, or WORKLOAD_ENTRA_CLIENT_SECRET in .env."
    )
  token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
  resp = requests.post(
      token_url,
      data={
          "client_id": client_id,
          "client_secret": client_secret,
          "scope": f"{client_id}/.default",
          "grant_type": "client_credentials",
      },
      timeout=20,
  )
  if not resp.ok:
    raise RuntimeError(
        f"Entra ID token request failed (HTTP {resp.status_code}): {resp.text}"
    )
  token = resp.json().get("access_token", "")
  if not token:
    raise RuntimeError(f"No access_token returned from Entra ID: {resp.text}")
  return token


def exchange_workload_token_for_sts_token(
    project_number: str,
    workload_pool_id: str,
    workload_provider_id: str,
    workload_token: str,
) -> Dict[str, Any]:
  """Exchanges an external workload JWT at Google STS for a federated access token."""
  audience = (
      f"//iam.googleapis.com/projects/{project_number}/locations/global/"
      f"workloadIdentityPools/{workload_pool_id}/providers/{workload_provider_id}"
  )
  payload = {
      "audience": audience,
      "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
      "requestedTokenType": "urn:ietf:params:oauth:token-type:access_token",
      "scope": "https://www.googleapis.com/auth/cloud-platform",
      "subjectTokenType": "urn:ietf:params:oauth:token-type:jwt",
      "subjectToken": workload_token.strip(),
  }
  resp = requests.post(STS_TOKEN_URL, json=payload, timeout=20)
  if not resp.ok:
    raise RuntimeError(
        f"Google STS Workload Identity exchange failed (HTTP {resp.status_code}): {resp.text}"
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
    sts_access_token: str,
    query_text: str,
    agent_id: str = "",
) -> Dict[str, Any]:
  """Calls Gemini Enterprise :streamAssist directly with the Workload Identity STS token."""
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
      "Authorization": f"Bearer {sts_access_token}",
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
      description="Pattern 1C: Direct Workload Identity Federation (Entra -> Google STS -> :streamAssist)."
  )
  parser.add_argument("--project-id", default=os.environ.get("GCP_PROJECT_ID", ""))
  parser.add_argument("--project-number", default=os.environ.get("GCP_PROJECT_NUMBER", ""))
  parser.add_argument("--workload-pool-id", default=os.environ.get("WORKLOAD_POOL_ID", "entra-workload-pool"))
  parser.add_argument("--workload-provider-id", default=os.environ.get("WORKLOAD_PROVIDER_ID", "entra-workload-provider"))
  parser.add_argument("--engine-id", default=os.environ.get("GE_GOOGLE_ID_ENGINE_ID", ""))
  parser.add_argument("--location", default=os.environ.get("GE_GOOGLE_ID_LOCATION", "global"))
  parser.add_argument("--agent-id", default="")
  parser.add_argument("--query", default=os.environ.get("QUERY", "What are our SLA policies?"))
  parser.add_argument("--raw", action="store_true", help="Print full raw JSON response")
  args = parser.parse_args()

  print("=== Pattern 1C: Direct Workload Identity Federation (STS) -> :streamAssist ===")
  print(f"Workload Pool : projects/{args.project_number}/locations/global/workloadIdentityPools/{args.workload_pool_id}")
  print(f"Provider      : {args.workload_provider_id}")
  print(f"Engine ID     : {args.engine_id} ({args.location})")
  print(f"Target        : {'Agent ID ' + args.agent_id if args.agent_id else 'Default Assistant'}")
  print(f"Query         : {args.query}\n")

  print("1. Fetching JWT from Microsoft Entra ID ...")
  workload_jwt = get_entra_workload_jwt(
      tenant_id=os.environ.get("ENTRA_TENANT_ID", ""),
      client_id=os.environ.get("WORKLOAD_ENTRA_CLIENT_ID", ""),
      client_secret=os.environ.get("WORKLOAD_ENTRA_CLIENT_SECRET", ""),
  )
  print("   ✓ Received Entra JWT")

  print("2. Exchanging Entra JWT at https://sts.googleapis.com/v1/token ...")
  sts_data = exchange_workload_token_for_sts_token(
      project_number=args.project_number,
      workload_pool_id=args.workload_pool_id,
      workload_provider_id=args.workload_provider_id,
      workload_token=workload_jwt,
  )
  sts_token = sts_data["access_token"]
  print(f"   ✓ Received Google STS access token (expires_in={sts_data.get('expires_in')}s)")

  print("3. Calling Gemini Enterprise :streamAssist ...")
  result = call_stream_assist(
      project_id=args.project_id,
      location=args.location,
      engine_id=args.engine_id,
      sts_access_token=sts_token,
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
