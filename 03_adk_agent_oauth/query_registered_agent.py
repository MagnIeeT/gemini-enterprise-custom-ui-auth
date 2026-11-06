"""Part 3: Querying & Authorizing a Registered ADK Agent with On-Demand Tool 3LO via :streamAssist.

How the Gemini Enterprise + ADK + Google Cloud Auth Manager 3LO flow works from a Custom UI:

  1. User sends a query to the registered ADK agent via `:streamAssist`:
       POST https://discoveryengine.googleapis.com/v1alpha/projects/{project}/locations/{loc}/collections/default_collection/engines/{engine}/assistants/default_assistant:streamAssist
       {
         "query": {"text": "..."},
         "session": "<optional_session_name>",
         "answerGenerationMode": "AGENT",
         "agentsSpec": {"agentSpecs": [{"agentId": "<AGENT_ID>"}]}
       }

  2. Inside the ADK agent on Vertex AI Agent Runtime (`AGENT_IDENTITY`):
     - Non-3LO tools (`PolicySubAgent` -> `get_company_sla_policy`) execute immediately
       and return the answer with zero OAuth prompts.
     - 3LO-protected tools (`SharePointSubAgent` / `GoogleWorkspaceSubAgent`) use ADK's
       built-in `AuthenticatedFunctionTool` + `GcpAuthProvider` + `GcpAuthProviderScheme`.
     - If the user has not yet authorized the tool's Auth Provider in Google Cloud Auth
       Manager Vault, ADK emits the native `adk_request_credential` event.

  3. Gemini Enterprise intercepts `adk_request_credential`, persists `(consent_nonce, agent_user_id)`
     in Spanner keyed by `(session, displayName)`, and returns `answer.requiredAuthorizations[]`
     plus `sessionInfo.session` in the `:streamAssist` response:
       "requiredAuthorizations": [
         {
           "authorization": "projects/.../locations/us-central1/authProviders/sharepoint-provider",
           "displayName": "projects/.../locations/us-central1/authProviders/sharepoint-provider",
           "authorizationUri": "https://login.microsoftonline.com/.../oauth2/v2.0/authorize?..."
         }
       ]

  4. The Custom UI opens `authorizationUri` in a browser popup. After the user signs in at
     the 3P IdP, `agentidentitycredentials.googleapis.com/.../oauthcallback` redirects to
     `continue_uri` with:
       `?user_id_validation_state=...&auth_provider_name=projects/.../authProviders/...&connector_name=...`

  5. The Custom Backend finalizes the 3LO flow by calling Gemini Enterprise's
     `AuthorizationService.ValidateUserAuthorization` endpoint:
       POST https://discoveryengine.googleapis.com/v1alpha/{session}:validateUserAuthorization
       {
         "session": "projects/.../sessions/{session_id}",
         "userIdValidationState": "<user_id_validation_state_from_redirect>",
         "downstreamConnector": "<displayName_from_requiredAuthorization>"
       }
     Gemini Enterprise recovers `(consent_nonce, agent_user_id)` from Spanner and calls
     Auth Manager's `FinalizeCredentials`, storing the user's OAuth token in Auth Manager Vault.

  6. The Custom UI automatically re-sends the original query on the same `session`, and the
     ADK agent executes the tool using the newly vaulted token!

Usage:
  # Step 1: Query a non-3LO sub-agent (answers immediately, no popup)
  .venv/bin/python 03_adk_agent_oauth/query_registered_agent.py --step query \\
    --query "What are our Grid Operations SLA policies?"

  # Step 2: Query a 3LO-protected sub-agent (returns answer if authorized, or requiredAuthorizations)
  .venv/bin/python 03_adk_agent_oauth/query_registered_agent.py --step query \\
    --query "What is my Google profile and email?"

  # Step 3: Finalize 3LO authorization using the redirect URL from the popup
  .venv/bin/python 03_adk_agent_oauth/query_registered_agent.py --step validate \\
    --session "projects/.../sessions/..." \\
    --downstream-connector "projects/customersprototype/locations/us-central1/authProviders/google-workspace-provider" \\
    --full-redirect-uri "https://vertexaisearch.cloud.google.com/validateUserId?user_id_validation_state=...&auth_provider_name=..."
"""

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

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


def discovery_host(location: str) -> str:
  return (
      "https://discoveryengine.googleapis.com"
      if location == "global"
      else f"https://{location}-discoveryengine.googleapis.com"
  )


def extract_agent_reply_and_oauth(chunks: Any) -> Dict[str, Any]:
  """Extracts assistant reply text, session name, and requiredAuthorizations from :streamAssist."""
  if not isinstance(chunks, list):
    return {
        "reply_text": json.dumps(chunks, indent=2),
        "session": "",
        "required_authorizations": [],
        "requires_3lo": False,
    }

  texts: List[str] = []
  session_name = ""
  required_auths: List[Dict[str, Any]] = []

  for chunk in chunks:
    sess = chunk.get("sessionInfo", {}).get("session", "")
    if sess:
      session_name = sess

    answer = chunk.get("answer", {})
    for reply in answer.get("replies", []):
      content = reply.get("groundedContent", {}).get("content", {})
      if content.get("thought"):
        continue
      text = content.get("text")
      if text:
        texts.append(text)

    for req_auth in answer.get("requiredAuthorizations", []):
      if req_auth not in required_auths:
        required_auths.append(req_auth)

  full_text = "".join(texts).strip()
  return {
      "reply_text": full_text,
      "session": session_name,
      "required_authorizations": required_auths,
      "requires_3lo": bool(required_auths),
  }


def query_adk_agent_stream_assist(
    project_id: str,
    location: str,
    engine_id: str,
    agent_id: str,
    user_bearer_token: str,
    query_text: str,
    session_name: Optional[str] = None,
) -> Dict[str, Any]:
  """Calls :streamAssist targeting a registered ADK agent (`agentId`)."""
  endpoint = (
      f"{discovery_host(location)}/v1alpha/projects/{project_id}/locations/{location}/"
      f"collections/default_collection/engines/{engine_id}/"
      f"assistants/default_assistant:streamAssist"
  )
  payload: Dict[str, Any] = {
      "query": {"text": query_text},
      "answerGenerationMode": "AGENT",
      "agentsSpec": {"agentSpecs": [{"agentId": agent_id}]},
  }
  if session_name:
    payload["session"] = session_name

  headers = {
      "Authorization": f"Bearer {user_bearer_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": project_id,
  }
  resp = requests.post(endpoint, headers=headers, json=payload, timeout=90)
  try:
    body = resp.json()
  except ValueError:
    body = {"raw": resp.text}

  parsed = extract_agent_reply_and_oauth(body) if resp.status_code == 200 else {
      "reply_text": "",
      "session": session_name or "",
      "required_authorizations": [],
      "requires_3lo": False,
  }
  return {
      "status_code": resp.status_code,
      "endpoint": endpoint,
      "raw_response": body,
      **parsed,
  }


def parse_validation_redirect_uri(
    full_redirect_uri: str,
    expected_display_name: str = "",
) -> Dict[str, str]:
  """Extracts `user_id_validation_state` and `downstream_connector` from the popup redirect URL."""
  parsed = urlparse(full_redirect_uri.strip())
  qs = parse_qs(parsed.query)
  validation_state = qs.get("user_id_validation_state", [""])[0]
  auth_provider_name = qs.get("auth_provider_name", [""])[0]
  connector_name = qs.get("connector_name", [""])[0]

  # Match whichever form (`authProviders` or `connectors`) was persisted as `displayName`
  candidates = [c for c in (connector_name, auth_provider_name) if c]
  downstream_connector = expected_display_name
  if expected_display_name and expected_display_name in candidates:
    downstream_connector = expected_display_name
  elif auth_provider_name:
    downstream_connector = auth_provider_name
  elif connector_name:
    downstream_connector = connector_name

  return {
      "user_id_validation_state": validation_state,
      "downstream_connector": downstream_connector,
      "auth_provider_name": auth_provider_name,
      "connector_name": connector_name,
  }


def validate_user_authorization(
    project_id: str,
    location: str,
    session_name: str,
    user_id_validation_state: str,
    downstream_connector: str,
    user_bearer_token: str,
) -> Dict[str, Any]:
  """Calls POST /v1alpha/{session}:validateUserAuthorization.

  Gemini Enterprise looks up the server-stored `(consent_nonce, agent_user_id)`
  for `(session, downstream_connector)` and calls Auth Manager's `FinalizeCredentials`
  to persist the user's 3LO token in Auth Manager Vault.
  """
  url = f"{discovery_host(location)}/v1alpha/{session_name}:validateUserAuthorization"
  headers = {
      "Authorization": f"Bearer {user_bearer_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": project_id,
  }
  payload = {
      "session": session_name,
      "userIdValidationState": user_id_validation_state,
      "downstreamConnector": downstream_connector,
  }
  resp = requests.post(url, headers=headers, json=payload, timeout=30)
  try:
    data = resp.json()
  except ValueError:
    data = {"raw": resp.text}
  return {"status_code": resp.status_code, "url": url, "data": data}


def revoke_stored_authorizations(
    project_id: str,
    auth_provider_location: str,
    user_bearer_token: str,
    providers: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
  """Lists and deletes stored user authorizations on the specified Auth Providers."""
  target_providers = providers or ["google-workspace-provider", "sharepoint-provider"]
  headers = {
      "Authorization": f"Bearer {user_bearer_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": project_id,
  }
  deleted: List[Dict[str, Any]] = []
  for provider in target_providers:
    list_url = (
        f"https://agentidentity.googleapis.com/v1beta/projects/{project_id}"
        f"/locations/{auth_provider_location}/authProviders/{provider}/authorizations"
    )
    list_res = requests.get(list_url, headers=headers, timeout=15)
    if list_res.status_code != 200:
      deleted.append({"provider": provider, "list_status": list_res.status_code})
      continue
    for auth_obj in list_res.json().get("authorizations", []):
      auth_name = auth_obj.get("name", "")
      if not auth_name:
        continue
      del_url = f"https://agentidentity.googleapis.com/v1beta/{auth_name}"
      del_res = requests.delete(del_url, headers=headers, timeout=15)
      deleted.append({
          "provider": provider,
          "authorization": auth_name,
          "client_user_id": auth_obj.get("clientUserId", ""),
          "delete_status": del_res.status_code,
      })
  return deleted


def resolve_context_and_token(args: argparse.Namespace) -> tuple[str, str, str, str, str]:
  """Returns (project_id, engine_id, location, agent_id, user_token)."""
  project_id = args.project_id or os.environ.get("GCP_PROJECT_ID", "")
  if args.auth_mode == "wif":
    engine_id = args.engine_id or os.environ.get("GE_WIF_ENGINE_ID", "")
    location = args.location or os.environ.get("GE_WIF_LOCATION", "us")
    agent_id = args.agent_id or os.environ.get("GE_WIF_AGENT_ID", "")
    idp_token = os.environ.get("IDP_ID_TOKEN", "").strip()
    if args.browser_login or not idp_token:
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

  return project_id, engine_id, location, agent_id, user_token


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Part 3: Query & Authorize a Registered ADK Agent via :streamAssist and :validateUserAuthorization."
  )
  parser.add_argument(
      "--step",
      choices=["query", "validate", "revoke"],
      default="query",
      help="'query' calls :streamAssist; 'validate' calls :validateUserAuthorization; 'revoke' clears stored 3LO tokens",
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
  parser.add_argument("--session", default="", help="Assist session resource name")
  parser.add_argument(
      "--downstream-connector",
      default="",
      help="Auth provider / connector resource name from requiredAuthorizations[].displayName",
  )
  parser.add_argument(
      "--full-redirect-uri",
      default="",
      help="Full redirect URI from popup (https://vertexaisearch.cloud.google.com/validateUserId?user_id_validation_state=...)",
  )
  parser.add_argument(
      "--user-id-validation-state",
      default="",
      help="Explicit user_id_validation_state query parameter",
  )
  parser.add_argument(
      "--query",
      default=os.environ.get("QUERY", "What are our Grid Operations SLA policies?"),
  )
  parser.add_argument("--raw", action="store_true", help="Print full raw JSON response")
  args = parser.parse_args()

  project_id, engine_id, location, agent_id, user_token = resolve_context_and_token(args)
  if not project_id or not engine_id:
    sys.exit("Error: Ensure GCP_PROJECT_ID and Engine ID are configured in .env or passed via CLI.")

  if args.step == "revoke":
    auth_loc = os.environ.get("AUTH_PROVIDER_LOCATION", "us-central1")
    deleted = revoke_stored_authorizations(
        project_id=project_id,
        auth_provider_location=auth_loc,
        user_bearer_token=user_token,
    )
    print("=== Part 3 (Revoke): Cleared Stored 3LO Authorizations in Auth Manager ===")
    print(json.dumps(deleted, indent=2))
    return

  if args.step == "validate":
    if not args.session:
      sys.exit("Error: Pass --session '<SESSION_RESOURCE_NAME>' from the :streamAssist response.")
    validation_state = args.user_id_validation_state.strip()
    downstream_connector = args.downstream_connector.strip()
    if args.full_redirect_uri:
      parsed_redir = parse_validation_redirect_uri(
          args.full_redirect_uri, expected_display_name=downstream_connector
      )
      validation_state = validation_state or parsed_redir["user_id_validation_state"]
      downstream_connector = downstream_connector or parsed_redir["downstream_connector"]

    if not validation_state or not downstream_connector:
      sys.exit(
          "Error: Provide --full-redirect-uri (or both --user-id-validation-state and --downstream-connector)."
      )

    print("=== Part 3 (Validate): POST {session}:validateUserAuthorization ===")
    print(f"Auth Mode            : {args.auth_mode.upper()}")
    print(f"Session              : {args.session}")
    print(f"Downstream Connector : {downstream_connector}\n")

    val_res = validate_user_authorization(
        project_id=project_id,
        location=location,
        session_name=args.session,
        user_id_validation_state=validation_state,
        downstream_connector=downstream_connector,
        user_bearer_token=user_token,
    )
    print(f"HTTP Status: {val_res['status_code']}")
    print(json.dumps(val_res["data"], indent=2))
    if val_res["status_code"] == 200:
      print(
          "\n✓ 3LO credential finalized in Google Cloud Auth Manager Vault!\n"
          "Re-run your query on the same session:\n"
          f'  .venv/bin/python 03_adk_agent_oauth/query_registered_agent.py --step query '
          f'--session "{args.session}" --query "{args.query}"'
      )
    return

  if not agent_id:
    sys.exit("Error: Set GE_GOOGLE_ID_AGENT_ID / GE_WIF_AGENT_ID in .env or pass --agent-id.")

  print("=== Part 3 (Query): Querying Registered High-Code ADK Agent via :streamAssist ===")
  print(f"Auth Mode         : {args.auth_mode.upper()}")
  print(f"Engine ID         : {engine_id} ({location})")
  print(f"Registered Agent  : {agent_id}")
  if args.session:
    print(f"Session           : {args.session}")
  print(f"Query             : {args.query}\n")

  res = query_adk_agent_stream_assist(
      project_id=project_id,
      location=location,
      engine_id=engine_id,
      agent_id=agent_id,
      user_bearer_token=user_token,
      query_text=args.query,
      session_name=args.session or None,
  )
  print(f"HTTP Status       : {res['status_code']}")
  if res["status_code"] != 200:
    print(json.dumps(res["raw_response"], indent=2))
    sys.exit(1)

  if res["session"]:
    print(f"Session           : {res['session']}")

  print("\n--- High-Code ADK Agent Reply ---")
  print(res["reply_text"] or "(Paused awaiting 3LO user authorization)")

  if res["requires_3lo"]:
    print("\n[!] On-Demand 3P Tool OAuth Required (`requiredAuthorizations`):")
    for req_auth in res["required_authorizations"]:
      disp = req_auth.get("displayName") or req_auth.get("authorization", "")
      uri = req_auth.get("authorizationUri", "")
      print(f"  - Auth Provider : {disp}")
      print(f"    Auth URL      : {uri}")
      print(
          f"\n    1. Open the Auth URL above in your browser and approve access.\n"
          f"    2. Copy the redirect URL (`https://vertexaisearch.cloud.google.com/validateUserId?user_id_validation_state=...`) and run:\n"
          f'       .venv/bin/python 03_adk_agent_oauth/query_registered_agent.py --step validate \\\n'
          f'         --session "{res["session"]}" \\\n'
          f'         --downstream-connector "{disp}" \\\n'
          f'         --full-redirect-uri "<PASTE_REDIRECT_URL_HERE>"'
      )

  if args.raw:
    print("\n--- Full Raw JSON Response ---")
    print(json.dumps(res["raw_response"], indent=2))


if __name__ == "__main__":
  main()
