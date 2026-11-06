"""Part 2: Authenticating Federated Connectors through a Custom UI (GE Keystore Flow).

When a user queries a Federated or Action Connector (SharePoint, Jira, Confluence,
ServiceNow, Salesforce, Google Drive) through a custom UI calling `:streamAssist`,
Gemini Enterprise checks its managed **Keystore** for that user's 3-legged OAuth (3LO)
credentials.

This script implements the official Google-recommended 4-step Custom UI + Backend flow:

  1. `--step list` (List Engine Connectors & Check Authorization Status):
       Calls `GET .../engines/{engine_id}/widgetConfigs/default_search_widget_config`
       (`WidgetConfigService.GetWidgetConfig`) to list all connectors attached to the
       Gemini Enterprise engine (`collectionComponents`), check each connector's
       per-user authorization state (`connectorAuthState.authState`), and retrieve the
       3LO `authorizationUri` if the user has not authorized the connector yet.
       Also calls `POST .../collections/{collection_id}/dataConnector:acquireAccessToken`
       to verify whether a live access/refresh token can be minted from the GE Keystore.

  2. `--step check` (Check a Single Connector & Print 3LO Authorization URL):
       Checks a specific connector (`--collection-id`) in the GE Keystore via
       `POST dataConnector:acquireAccessToken` and `GetWidgetConfig`. If unauthorized,
       prints the `authorizationUri` for the user to open in a browser popup.

  3. `--step store` (Exchange 3LO Auth Code & Store Refresh Token in GE Keystore):
       After the user signs in at the 3P Auth Server and is redirected to
       `https://vertexaisearch.cloud.google.com/oauth-redirect?code=...&state=...`,
       calls `POST .../collections/{collection_id}/dataConnector:acquireAndStoreRefreshToken`
       with `fullRedirectUri` so Gemini Enterprise exchanges the code and stores the
       `refresh_token` and `access_token` in its managed Keystore.

  4. `--step query` (Run `:streamAssist` Query with Authorized Connector):
       Calls `POST .../engines/{engine_id}/assistants/default_assistant:streamAssist`
       using the user's Bearer token so Gemini Enterprise retrieves live results from
       the authorized federated connector using the credentials in its Keystore.

Usage:
  # Step 1: List all connectors on the engine, check if authorized, and get auth URLs
  python 02_federated_connector_oauth/connector_oauth_flow.py --step list

  # Step 2: Check a specific connector (or auto-select from engine)
  python 02_federated_connector_oauth/connector_oauth_flow.py --step check --collection-id "<COLLECTION_ID>"

  # Step 3: Paste the redirect URL (or --code and --state) to store tokens in GE Keystore
  python 02_federated_connector_oauth/connector_oauth_flow.py --step store \\
    --collection-id "<COLLECTION_ID>" \\
    --full-redirect-uri "https://vertexaisearch.cloud.google.com/oauth-redirect?code=...&state=..."

  # Step 4: Run a :streamAssist query using the authorized connector
  python 02_federated_connector_oauth/connector_oauth_flow.py --step query \\
    --query "Summarize my latest documents in SharePoint"
"""

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
import webbrowser
from dotenv import load_dotenv
import requests

# Allow importing user authentication helpers from Part 1 (01_user_auth_streamassist)
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "01_user_auth_streamassist"))

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

DEFAULT_REDIRECT_URI = "https://vertexaisearch.cloud.google.com/oauth-redirect"


def discovery_host(location: str) -> str:
  return (
      "https://discoveryengine.googleapis.com"
      if location == "global"
      else f"https://{location}-discoveryengine.googleapis.com"
  )


def connector_base_url(project_id: str, location: str, collection_id: str) -> str:
  return (
      f"{discovery_host(location)}/v1alpha/projects/{project_id}/locations/{location}"
      f"/collections/{collection_id}/dataConnector"
  )


def get_engine_widget_config(
    project_id: str,
    location: str,
    engine_id: str,
    user_bearer_token: str,
    custom_domain: str = "",
) -> Dict[str, Any]:
  """Calls GET .../engines/{engine_id}/widgetConfigs/default_search_widget_config.

  Returns the engine's `collectionComponents[]`, where each connector includes:
    - `id`: collection_id
    - `name`: projects/{project_number}/locations/{location}/collections/{collection_id}
    - `displayName`, `dataSource`, `dataSourceDisplayName`
    - `connectorAuthState`:
        - `authState`: AUTHORIZED | AUTH_STATE_UNSPECIFIED | EXPIRED | ACTIONS_DISABLED | NO_AUTH
        - `authorizationUri`: 3LO OAuth URL to open in a browser popup if not authorized

  When `custom_domain` is provided (and the project is enrolled in `GE_CUSTOM_UI_USER`
  API visibility + the domain is in `accessSettings.allowlistedDomains`), Google
  encrypts `custom_domain` into `&state=...` on `authorizationUri` so
  `https://vertexaisearch.cloud.google.com/oauth-redirect` automatically posts
  `{code, state, fullRedirectUrl}` back to `window.opener` and closes the popup.
  """
  base_url = (
      f"{discovery_host(location)}/v1alpha/projects/{project_id}/locations/{location}"
      f"/collections/default_collection/engines/{engine_id}"
      f"/widgetConfigs/default_search_widget_config"
  )
  headers = {
      "Authorization": f"Bearer {user_bearer_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": project_id,
  }
  if custom_domain:
    resp = requests.get(
        base_url,
        headers=headers,
        params={"getWidgetConfigRequestOption.customDomain": custom_domain},
        timeout=30,
    )
    if resp.status_code == 200:
      return {"status_code": resp.status_code, "url": resp.url, "data": resp.json()}

  resp = requests.get(base_url, headers=headers, timeout=30)
  try:
    data = resp.json()
  except ValueError:
    data = {"raw": resp.text}
  return {"status_code": resp.status_code, "url": base_url, "data": data}


def get_engine_data_store_specs(
    project_id: str,
    location: str,
    engine_id: str,
    user_bearer_token: str,
) -> List[Dict[str, str]]:
  """Extracts all `dataStoreComponents[].name` from `GetWidgetConfig`.

  Returns a list of `{"dataStore": "<full_datastore_resource_name>"}` dicts ready
  to pass into `:streamAssist` as `toolsSpec.vertexAiSearchSpec.dataStoreSpecs`.
  """
  widget_res = get_engine_widget_config(
      project_id=project_id,
      location=location,
      engine_id=engine_id,
      user_bearer_token=user_bearer_token,
  )
  if widget_res["status_code"] != 200 or not isinstance(widget_res["data"], dict):
    return []
  specs: List[Dict[str, str]] = []
  seen = set()
  for comp in widget_res["data"].get("collectionComponents", []):
    for ds in comp.get("dataStoreComponents", []):
      ds_name = ds.get("name", "")
      if ds_name and ds_name not in seen:
        seen.add(ds_name)
        specs.append({"dataStore": ds_name})
  return specs


def acquire_connector_access_token(
    project_id: str,
    location: str,
    collection_id: str,
    user_bearer_token: str,
) -> Dict[str, Any]:
  """Calls POST .../collections/{collection_id}/dataConnector:acquireAccessToken.

  Checks whether the calling user has a valid refresh token stored in the Gemini
  Enterprise Keystore for this connector and returns a live `accessToken` +
  `refreshTokenInfo` (or HTTP 404 NOT_FOUND if not yet authorized).
  """
  url = f"{connector_base_url(project_id, location, collection_id)}:acquireAccessToken"
  headers = {
      "Authorization": f"Bearer {user_bearer_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": project_id,
  }
  resp = requests.post(url, headers=headers, json={}, timeout=20)
  try:
    data = resp.json()
  except ValueError:
    data = {"raw": resp.text}
  return {"status_code": resp.status_code, "url": url, "data": data}


def store_connector_refresh_token(
    project_id: str,
    location: str,
    collection_id: str,
    user_bearer_token: str,
    full_redirect_uri: str,
) -> Dict[str, Any]:
  """Calls POST .../collections/{collection_id}/dataConnector:acquireAndStoreRefreshToken.

  Exchanges the 3LO authorization code (from `fullRedirectUri`) with the 3P OAuth
  server and stores the resulting `refresh_token` and `access_token` in the Gemini
  Enterprise Keystore for this user.
  """
  url = (
      f"{connector_base_url(project_id, location, collection_id)}"
      ":acquireAndStoreRefreshToken"
  )
  headers = {
      "Authorization": f"Bearer {user_bearer_token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": project_id,
  }
  payload = {"fullRedirectUri": full_redirect_uri}
  resp = requests.post(url, headers=headers, json=payload, timeout=30)
  try:
    data = resp.json()
  except ValueError:
    data = {"raw": resp.text}
  return {"status_code": resp.status_code, "url": url, "data": data}


def resolve_user_token(args: argparse.Namespace) -> str:
  """Resolves the user Bearer token via Google Identity or Workforce Identity (STS)."""
  if args.access_token:
    return args.access_token.strip()

  if args.auth_mode == "google":
    return get_google_user_token(browser_login=args.browser_login)

  # Workforce Identity Federation (Entra ID -> Google STS)
  pool_id = os.environ.get("WIF_POOL_ID", "")
  provider_id = os.environ.get("WIF_PROVIDER_ID", "")
  idp_token = os.environ.get("IDP_ID_TOKEN", "").strip()
  if args.browser_login or not idp_token:
    idp_token = get_entra_id_token_via_browser_login(
        tenant_id=os.environ.get("ENTRA_TENANT_ID", ""),
        client_id=os.environ.get("ENTRA_CLIENT_ID", ""),
    )
  sts_data = exchange_idp_token_for_sts_token(
      workforce_pool_id=pool_id,
      provider_id=provider_id,
      idp_token=idp_token,
  )
  return sts_data["access_token"]


def extract_Action_collection_id(action_connector_name: str) -> str:
  """Extracts the collection ID from a full actionConnector resource name if present."""
  parts = action_connector_name.split("/")
  if "collections" in parts:
    idx = parts.index("collections")
    if idx + 1 < len(parts):
      return parts[idx + 1]
  return ""


def print_connector_summary(
    idx: int,
    comp: Dict[str, Any],
    project_id: str,
    location: str,
    user_token: str,
) -> Dict[str, Any]:
  """Prints the status of a single connector from collectionComponents and Keystore."""
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

  print(f"[{idx}] {display_name}")
  print(f"    Collection ID     : {collection_id}")
  if target_collection_id != collection_id:
    print(f"    Action Collection : {target_collection_id} ({action_connector})")
  print(f"    Data Source       : {data_source}")
  print(f"    Engine Auth State : {widget_auth_state}")

  # Also check live Keystore status via POST dataConnector:acquireAccessToken
  # if this is a connector collection (skip non-connector default_collection)
  keystore_authorized = False
  if target_collection_id and target_collection_id != "default_collection":
    token_check = acquire_connector_access_token(
        project_id=project_id,
        location=location,
        collection_id=target_collection_id,
        user_bearer_token=user_token,
    )
    status_code = token_check["status_code"]
    token_data = token_check["data"]
    if status_code == 200 and isinstance(token_data, dict) and token_data.get("accessToken"):
      keystore_authorized = True
      scopes = token_data.get("refreshTokenInfo", {}).get("scopes", [])
      print(
          f"    Keystore Status   : AUTHORIZED (HTTP 200 - valid access token minted; scopes={scopes})"
      )
    else:
      err_msg = ""
      if isinstance(token_data, dict) and "error" in token_data:
        err_msg = token_data["error"].get("status") or token_data["error"].get("message", "")
      print(f"    Keystore Status   : NOT AUTHORIZED (HTTP {status_code} {err_msg})")

  is_authorized = (
      widget_auth_state in ("AUTHORIZED", "NO_AUTH") or keystore_authorized
  )
  if auth_uri and not keystore_authorized:
    print(f"    3LO Auth URL      : {auth_uri}")
    print(
        f"    -> To authorize, open the URL above in your browser, then run:\n"
        f'       python 02_federated_connector_oauth/connector_oauth_flow.py --step store '
        f'--auth-mode {os.environ.get("_CURRENT_AUTH_MODE", "google")} '
        f'--collection-id "{target_collection_id}" '
        f'--full-redirect-uri "<PASTE_REDIRECT_URL_HERE>"'
    )
  print()
  return {
      "collection_id": collection_id,
      "target_collection_id": target_collection_id,
      "display_name": display_name,
      "data_source": data_source,
      "widget_auth_state": widget_auth_state,
      "keystore_authorized": keystore_authorized,
      "is_authorized": is_authorized,
      "auth_uri": auth_uri,
  }


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Part 2: Federated Connector 3LO Discovery, Keystore Auth, and :streamAssist Query."
  )
  parser.add_argument(
      "--step",
      choices=["list", "check", "store", "query"],
      default="list",
      help=(
          "'list' lists engine connectors & auth state; "
          "'check' checks one connector; "
          "'store' saves 3LO code to Keystore; "
          "'query' runs :streamAssist"
      ),
  )
  parser.add_argument(
      "--auth-mode",
      choices=["google", "wif"],
      default="google",
      help="User authentication mode ('google' for Pattern 1A, 'wif' for Pattern 1B)",
  )
  parser.add_argument(
      "--browser-login",
      action="store_true",
      help="Use interactive browser login for Google OAuth or Entra ID OIDC",
  )
  parser.add_argument("--project-id", default=os.environ.get("GCP_PROJECT_ID", ""))
  parser.add_argument("--engine-id", default="")
  parser.add_argument("--location", default="")
  parser.add_argument(
      "--collection-id",
      default=os.environ.get("CONNECTOR_COLLECTION_ID", ""),
      help="Connector collection ID (e.g., from --step list)",
  )
  parser.add_argument(
      "--redirect-uri",
      default=os.environ.get("CONNECTOR_REDIRECT_URI", DEFAULT_REDIRECT_URI),
  )
  parser.add_argument("--access-token", default="", help="Explicit user Bearer token")
  parser.add_argument("--code", default="", help="OAuth authorization code from redirect")
  parser.add_argument("--state", default="", help="OAuth state parameter from redirect")
  parser.add_argument(
      "--full-redirect-uri",
      default="",
      help="Full redirect URI from browser (https://vertexaisearch.cloud.google.com/oauth-redirect?code=...&state=...)",
  )
  parser.add_argument(
      "--open-browser",
      action="store_true",
      help="Automatically open the 3LO authorizationUri in your browser when unauthorized",
  )
  parser.add_argument(
      "--query",
      default=os.environ.get("QUERY", "What are the key points in Cymbal_Sigma_Notes.docx?"),
      help="Query to send to :streamAssist in --step query",
  )
  parser.add_argument("--raw", action="store_true", help="Print full raw JSON response")
  args = parser.parse_args()

  os.environ["_CURRENT_AUTH_MODE"] = args.auth_mode

  if args.auth_mode == "google":
    engine_id = args.engine_id or os.environ.get("GE_GOOGLE_ID_ENGINE_ID", "")
    location = args.location or os.environ.get(
        "CONNECTOR_LOCATION", os.environ.get("GE_GOOGLE_ID_LOCATION", "global")
    )
  else:
    engine_id = args.engine_id or os.environ.get("GE_WIF_ENGINE_ID", "")
    location = args.location or os.environ.get("GE_WIF_LOCATION", "us")

  if not args.project_id:
    sys.exit("Error: Set GCP_PROJECT_ID in .env or pass --project-id.")

  user_token = resolve_user_token(args)

  # ---------------------------------------------------------------------------
  # STEP 1: LIST ALL CONNECTORS FOR THE ENGINE & CHECK AUTHORIZATION STATUS
  # ---------------------------------------------------------------------------
  if args.step == "list":
    if not engine_id:
      sys.exit("Error: Set GE_GOOGLE_ID_ENGINE_ID / GE_WIF_ENGINE_ID in .env or pass --engine-id.")
    print("=== Part 2 (Step 1): List Connectors & Check Keystore Auth Status ===")
    print(f"Auth Mode  : {args.auth_mode}")
    print(f"Project ID : {args.project_id}")
    print(f"Engine ID  : {engine_id} ({location})\n")

    widget_res = get_engine_widget_config(
        project_id=args.project_id,
        location=location,
        engine_id=engine_id,
        user_bearer_token=user_token,
    )
    print(f"GET widgetConfigs/default_search_widget_config -> HTTP {widget_res['status_code']}")
    if widget_res["status_code"] != 200:
      print(json.dumps(widget_res["data"], indent=2))
      return

    components: List[Dict[str, Any]] = widget_res["data"].get("collectionComponents", [])
    if not components:
      print("\nNo connectors/collections are currently linked to this engine.")
      return

    print(f"Found {len(components)} collection(s)/connector(s) attached to engine '{engine_id}':\n")
    for idx, comp in enumerate(components, start=1):
      print_connector_summary(idx, comp, args.project_id, location, user_token)

    if args.raw:
      print("--- Raw collectionComponents JSON ---")
      print(json.dumps(components, indent=2))
    return

  # ---------------------------------------------------------------------------
  # STEP 2: CHECK A SPECIFIC CONNECTOR & PRINT 3LO AUTHORIZATION URL
  # ---------------------------------------------------------------------------
  if args.step == "check":
    print("=== Part 2 (Step 2): Check Connector Keystore Status & Get 3LO Auth URL ===")
    print(f"Auth Mode     : {args.auth_mode}")
    print(f"Project ID    : {args.project_id}")
    print(f"Location      : {location}")

    target_collection = args.collection_id
    matched_comp: Optional[Dict[str, Any]] = None

    if engine_id:
      widget_res = get_engine_widget_config(
          project_id=args.project_id,
          location=location,
          engine_id=engine_id,
          user_bearer_token=user_token,
      )
      if widget_res["status_code"] == 200:
        components = widget_res["data"].get("collectionComponents", [])
        for comp in components:
          cid = comp.get("id", "")
          act_cid = extract_Action_collection_id(comp.get("actionConnector", ""))
          if target_collection and target_collection != "default_collection":
            if target_collection in (cid, act_cid):
              matched_comp = comp
              break
          elif comp.get("connectorAuthState", {}).get("authorizationUri") or (
              cid and cid != "default_collection"
          ):
            matched_comp = comp
            target_collection = act_cid or cid
            break

    if matched_comp:
      print(f"Engine ID     : {engine_id}\n")
      summary = print_connector_summary(1, matched_comp, args.project_id, location, user_token)
      if args.open_browser and summary.get("auth_uri") and not summary.get("keystore_authorized"):
        print("Opening 3LO authorizationUri in your browser...")
        webbrowser.open(summary["auth_uri"])
      return

    if not target_collection or target_collection == "default_collection":
      sys.exit(
          "No federated connector specified. Run with `--step list` first to see "
          "available connector Collection IDs on your engine, then pass `--collection-id`."
      )

    print(f"Collection ID : {target_collection}\n")
    res = acquire_connector_access_token(
        project_id=args.project_id,
        location=location,
        collection_id=target_collection,
        user_bearer_token=user_token,
    )
    print(f"POST dataConnector:acquireAccessToken -> HTTP {res['status_code']}")
    print(json.dumps(res["data"], indent=2))
    return

  # ---------------------------------------------------------------------------
  # STEP 3: STORE 3LO AUTHORIZATION CODE IN GE KEYSTORE
  # ---------------------------------------------------------------------------
  if args.step == "store":
    if not args.collection_id or args.collection_id == "default_collection":
      sys.exit(
          "Error: Pass --collection-id '<CONNECTOR_COLLECTION_ID>' (from `--step list`) "
          "when running `--step store`."
      )

    full_redirect = args.full_redirect_uri.strip()
    if not full_redirect:
      if not args.code:
        sys.exit(
            "Error: For --step store, provide either:\n"
            '  --full-redirect-uri "https://vertexaisearch.cloud.google.com/oauth-redirect?code=...&state=..."\n'
            "or:\n"
            '  --code "<OAUTH_CODE>" --state "<STATE>"'
        )
      full_redirect = f"{args.redirect_uri}?code={args.code}"
      if args.state:
        full_redirect += f"&state={args.state}"

    print("=== Part 2 (Step 3): POST dataConnector:acquireAndStoreRefreshToken ===")
    print(f"Auth Mode     : {args.auth_mode}")
    print(f"Project ID    : {args.project_id}")
    print(f"Collection ID : {args.collection_id} ({location})\n")

    res = store_connector_refresh_token(
        project_id=args.project_id,
        location=location,
        collection_id=args.collection_id,
        user_bearer_token=user_token,
        full_redirect_uri=full_redirect,
    )
    print(f"HTTP Status: {res['status_code']}")
    print(json.dumps(res["data"], indent=2))

    if res["status_code"] == 200:
      print("\n✓ Refresh token stored in Gemini Enterprise Keystore!")
      print("Verifying token retrieval via POST dataConnector:acquireAccessToken ...")
      verify_res = acquire_connector_access_token(
          project_id=args.project_id,
          location=location,
          collection_id=args.collection_id,
          user_bearer_token=user_token,
      )
      print(f"Verify HTTP Status: {verify_res['status_code']}")
      if verify_res["status_code"] == 200:
        scopes = verify_res["data"].get("refreshTokenInfo", {}).get("scopes", [])
        print(f"✓ Confirmed live access token minted from Keystore (scopes={scopes})!")
        print(
            "\nNext step — run a :streamAssist query against your authorized connector:\n"
            f'  python 02_federated_connector_oauth/connector_oauth_flow.py --step query '
            f'--auth-mode {args.auth_mode} --query "Your query here"'
        )
      else:
        print(json.dumps(verify_res["data"], indent=2))
    return

  # ---------------------------------------------------------------------------
  # STEP 4: RUN A :streamAssist QUERY USING THE AUTHORIZED CONNECTOR
  # ---------------------------------------------------------------------------
  if args.step == "query":
    if not engine_id:
      sys.exit("Error: Set GE_GOOGLE_ID_ENGINE_ID / GE_WIF_ENGINE_ID in .env or pass --engine-id.")
    print("=== Part 2 (Step 4): Querying :streamAssist with Authorized Connector ===")
    print(f"Auth Mode  : {args.auth_mode}")
    print(f"Project ID : {args.project_id}")
    print(f"Engine ID  : {engine_id} ({location})")
    data_store_specs = get_engine_data_store_specs(
        project_id=args.project_id,
        location=location,
        engine_id=engine_id,
        user_bearer_token=user_token,
    )
    print(f"DataStores : {[s['dataStore'].split('/')[-1] for s in data_store_specs]}")
    print(f"Query      : {args.query}\n")

    result = call_stream_assist(
        project_id=args.project_id,
        location=location,
        engine_id=engine_id,
        user_access_token=user_token,
        query_text=args.query,
        data_store_specs=data_store_specs,
    )
    print(f"HTTP Status: {result['status_code']}\n")
    if args.raw or result["status_code"] != 200:
      print(json.dumps(result["response"], indent=2))
    else:
      print("--- Assistant Answer ---")
      print(extract_reply_text(result["response"]))


if __name__ == "__main__":
  main()
