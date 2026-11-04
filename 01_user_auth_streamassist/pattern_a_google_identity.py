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
  python 01_user_auth_streamassist/pattern_a_google_identity.py --query "What are our SLA policies?"
  python 01_user_auth_streamassist/pattern_a_google_identity.py --agent-id "$GE_GOOGLE_ID_AGENT_ID" --query "Get my open ServiceNow incidents"
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List
from dotenv import load_dotenv
import requests

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def get_google_user_token(explicit_token: str = "") -> str:
  """Returns a Google OAuth user access token from CLI/env or local gcloud CLI."""
  token = (explicit_token or os.environ.get("GOOGLE_USER_ACCESS_TOKEN", "")).strip()
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
        "No GOOGLE_USER_ACCESS_TOKEN set and `gcloud auth print-access-token` failed. "
        "Run `gcloud auth login` or set GOOGLE_USER_ACCESS_TOKEN in .env."
    ) from exc


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
  parser.add_argument("--raw", action="store_true", help="Print full raw JSON response")
  args = parser.parse_args()

  if not args.project_id or not args.engine_id:
    sys.exit("Error: Set GCP_PROJECT_ID and GE_GOOGLE_ID_ENGINE_ID in .env or via CLI flags.")

  token = get_google_user_token(args.access_token)
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
