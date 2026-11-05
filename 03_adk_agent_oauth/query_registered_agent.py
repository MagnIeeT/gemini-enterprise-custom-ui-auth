"""Part 3: Querying a Registered Custom ADK Agent with Individual Tool OAuth via :streamAssist.

Why this matters:
  When you register a custom high-code ADK agent in Gemini Enterprise and call it
  from a custom UI via `:streamAssist`:
  - Attaching `authorizationConfig` at the top-level Gemini Enterprise agent registration
    requires the native Gemini Enterprise web UI to render the OAuth popup, causing
    `:streamAssist` calls from a custom UI to fail.
  - Instead, register the ADK agent WITHOUT top-level `authorizationConfig`, and manage
    OAuth inside the individual ADK subagent/tool (`request_credential` / on-demand 3LO).
  - General queries (such as SLA policy questions) execute immediately with zero popups,
    while protected third-party tools (such as ServiceNow or Jira) return an on-demand
    authorization prompt only when invoked.

Usage:
  # 1. Query a non-3LO tool on the registered ADK agent (answers immediately, no popup)
  python 03_adk_agent_oauth/query_registered_agent.py --query "What are our Grid Operations SLA policies?"

  # 2. Query a 3LO-protected tool on the registered ADK agent (triggers on-demand 3P OAuth prompt)
  python 03_adk_agent_oauth/query_registered_agent.py --query "List my open ServiceNow incidents"

  # 3. Test using Workforce Identity Federation user authentication (--auth-mode wif)
  python 03_adk_agent_oauth/query_registered_agent.py --auth-mode wif --browser-login --query "List my open ServiceNow incidents"
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List
from dotenv import load_dotenv
import requests

# Allow importing token helpers from 01_user_auth_streamassist
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "01_user_auth_streamassist"))

from pattern_a_google_identity import get_google_user_token  # pylint: disable=wrong-import-position
from pattern_b_workforce_sts import (  # pylint: disable=wrong-import-position
    exchange_idp_token_for_sts_token,
    get_entra_id_token_via_browser_login,
)

load_dotenv(REPO_ROOT / ".env")


def extract_agent_reply_and_oauth(chunks: Any) -> Dict[str, Any]:
  """Extracts assistant reply text and any on-demand 3P OAuth prompt from :streamAssist."""
  if not isinstance(chunks, list):
    return {"reply_text": json.dumps(chunks, indent=2), "oauth_url": None}

  texts: List[str] = []
  oauth_url = None

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

  full_text = "".join(texts).strip()
  raw_dump = json.dumps(chunks)

  # Detect markdown or raw OAuth authorization URL returned by the agent
  url_match = re.search(r"https?://[^\s\)\"']+/oauth/authorize[^\s\)\"']*", full_text or raw_dump)
  if url_match:
    oauth_url = url_match.group(0)

  requires_3lo = (
      "SERVICENOW_3LO_AUTH_REQUIRED" in raw_dump
      or "adk_request_credential" in raw_dump
      or "requiredAuthorizations" in raw_dump
      or bool(oauth_url)
  )

  return {
      "reply_text": full_text,
      "requires_3lo": requires_3lo,
      "oauth_url": oauth_url,
  }


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Part 3: Query a registered High-Code ADK Agent via :streamAssist."
  )
  parser.add_argument(
      "--auth-mode",
      choices=["google", "wif"],
      default="google",
      help="User auth mode for :streamAssist ('google' or 'wif')",
  )
  parser.add_argument(
      "--browser-login",
      action="store_true",
      help="Use interactive browser login for Google OAuth or Entra ID OIDC",
  )
  parser.add_argument("--project-id", default=os.environ.get("GCP_PROJECT_ID", ""))
  parser.add_argument("--engine-id", default="")
  parser.add_argument("--location", default="")
  parser.add_argument("--agent-id", default="")
  parser.add_argument(
      "--query",
      default=os.environ.get("QUERY", "What are our Grid Operations SLA policies?"),
  )
  parser.add_argument("--raw", action="store_true", help="Print full raw JSON response")
  args = parser.parse_args()

  # Resolve engine, location, agent_id, and user token based on selected auth mode
  if args.auth_mode == "wif":
    engine_id = args.engine_id or os.environ.get("GE_WIF_ENGINE_ID", "")
    location = args.location or os.environ.get("GE_WIF_LOCATION", "us")
    agent_id = args.agent_id or os.environ.get("GE_WIF_AGENT_ID", "")
    idp_token = get_entra_id_token_via_browser_login(
        tenant_id=os.environ.get("ENTRA_TENANT_ID", ""),
        client_id=os.environ.get("ENTRA_CLIENT_ID", ""),
    )
    sts_resp = exchange_idp_token_for_sts_token(
        workforce_pool_id=os.environ["WIF_POOL_ID"],
        provider_id=os.environ["WIF_PROVIDER_ID"],
        idp_token=idp_token,
    )
    user_token = sts_resp["access_token"]
  else:
    engine_id = args.engine_id or os.environ.get("GE_GOOGLE_ID_ENGINE_ID", "")
    location = args.location or os.environ.get("GE_GOOGLE_ID_LOCATION", "global")
    agent_id = args.agent_id or os.environ.get("GE_GOOGLE_ID_AGENT_ID", "")
    user_token = get_google_user_token(browser_login=args.browser_login)

  if not args.project_id or not engine_id or not agent_id:
    sys.exit(
        "Error: Ensure GCP_PROJECT_ID, Engine ID, and Agent ID are configured in .env or passed via CLI."
    )

  host = (
      "discoveryengine.googleapis.com"
      if location == "global"
      else f"{location}-discoveryengine.googleapis.com"
  )
  endpoint = (
      f"https://{host}/v1alpha/projects/{args.project_id}/locations/{location}/"
      f"collections/default_collection/engines/{engine_id}/"
      f"assistants/default_assistant:streamAssist"
  )
  payload = {
      "query": {"text": args.query},
      "answerGenerationMode": "AGENT",
      "agentsSpec": {"agentSpecs": [{"agentId": agent_id}]},
  }
  headers = {
      "Authorization": f"Bearer {user_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": args.project_id,
  }

  print("=== Part 3: Querying Registered High-Code ADK Agent via :streamAssist ===")
  print(f"Auth Mode         : {args.auth_mode.upper()}")
  print(f"Engine ID         : {engine_id} ({location})")
  print(f"Registered Agent  : {agent_id}")
  print(f"Query             : {args.query}\n")

  resp = requests.post(endpoint, headers=headers, json=payload, timeout=90)
  print(f"HTTP Status       : {resp.status_code}")
  try:
    body = resp.json()
  except ValueError:
    print(resp.text)
    sys.exit(1)

  if resp.status_code != 200:
    print(json.dumps(body, indent=2))
    sys.exit(1)

  parsed = extract_agent_reply_and_oauth(body)
  print("\n--- High-Code ADK Agent Reply ---")
  print(parsed["reply_text"] or "(Empty text reply — pass --raw to inspect full streamAssist chunks)")

  if parsed["requires_3lo"]:
    print("\n[!] On-Demand 3P Tool OAuth Triggered:")
    print("    The agent requires third-party user consent for this specific tool.")
    if parsed["oauth_url"]:
      print(f"    Popup Authorization URL: {parsed['oauth_url']}")

  if args.raw:
    print("\n--- Full Raw JSON Response ---")
    print(json.dumps(body, indent=2))


if __name__ == "__main__":
  main()
