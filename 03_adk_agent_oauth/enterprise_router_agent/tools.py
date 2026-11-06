"""Tools for PolicySubAgent (no auth), GoogleWorkspaceSubAgent (3LO), and SharePointSubAgent (3LO).

Uses ADK's built-in `AuthenticatedFunctionTool` + `GcpAuthProvider` + `GcpAuthProviderScheme`
so that when a user has not yet authorized a 3LO tool, ADK automatically emits the native
`adk_request_credential` event. Gemini Enterprise (`Dolphin`) intercepts `adk_request_credential`
and surfaces a native `RequiredAuthorization` OAuth popup in the Gemini Enterprise UI or
Custom UI `:streamAssist` response.
"""

from __future__ import annotations

import os
from typing import Any

from google.adk.auth.auth_credential import AuthCredential
from google.adk.auth.auth_tool import AuthConfig
from google.adk.auth.credential_manager import CredentialManager
from google.adk.integrations.agent_identity.gcp_auth_provider import GcpAuthProvider
from google.adk.integrations.agent_identity.gcp_auth_provider_scheme import GcpAuthProviderScheme
from google.adk.tools.authenticated_function_tool import AuthenticatedFunctionTool
from google.adk.tools.tool_context import ToolContext
import httpx

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "customersprototype")
AUTH_PROVIDER_LOCATION = os.environ.get("AUTH_PROVIDER_LOCATION", "us-central1")


def _ensure_gcp_auth_provider_registered() -> None:
  """Ensures GcpAuthProvider is registered on CredentialManager in the current worker."""
  if CredentialManager._auth_provider_registry.get_provider(GcpAuthProviderScheme) is None:
    CredentialManager.register_auth_provider(GcpAuthProvider())


_ensure_gcp_auth_provider_registered()


class GcpAuthenticatedFunctionTool(AuthenticatedFunctionTool):
  """AuthenticatedFunctionTool that ensures GcpAuthProvider is registered on every worker."""

  async def run_async(
      self, *, args: dict[str, Any], tool_context: ToolContext
  ) -> Any:
    _ensure_gcp_auth_provider_registered()
    return await super().run_async(args=args, tool_context=tool_context)


def _extract_access_token(credential: AuthCredential | None) -> str:
  """Extracts the Bearer access token from an ADK AuthCredential."""
  if credential is None:
    return ""
  if credential.http and credential.http.credentials and credential.http.credentials.token:
    return credential.http.credentials.token
  if credential.oauth2 and credential.oauth2.access_token:
    return credential.oauth2.access_token
  return ""


def get_company_sla_policy(topic: str) -> dict[str, Any]:
  """Looks up internal company SLA and HR policies (no authentication required).

  Args:
      topic: Policy topic to query (e.g., 'grid operations', 'leave', 'incident sla').
  """
  policies = {
      "grid_operations": (
          "Grid Operations SLA Policy (POL-GRID-01): Critical P1 substation "
          "outages require <15 min acknowledgment and <2 hour restoration."
      ),
      "leave": (
          "Annual Leave Policy (POL-HR-04): Employees receive 24 paid leave days "
          "and 12 sick leave days per calendar year."
      ),
      "incident_sla": (
          "IT Incident SLA (POL-IT-02): Severity-1 tickets must be triaged within "
          "15 minutes; Severity-2 within 2 hours."
      ),
  }
  return {
      "status": "SUCCESS",
      "requested_topic": topic,
      "policies": policies,
  }


def get_google_user_info(
    tool_context: ToolContext,
    credential: AuthCredential | None = None,
) -> dict[str, Any]:
  """Fetches the logged-in user's Google profile (name, email) via on-demand 3LO."""
  user_id = getattr(tool_context, "user_id", None) or "default-user"
  access_token = _extract_access_token(credential)
  if not access_token:
    return {
        "status": "PENDING_USER_OAUTH",
        "message": "Waiting for user to authorize google-workspace-provider.",
    }

  with httpx.Client(timeout=15.0) as client:
    response = client.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    response.raise_for_status()
    return {
        "status": "SUCCESS",
        "user_id": user_id,
        "profile": response.json(),
    }


def get_sharepoint_sites(
    search_query: str = "*",
    tool_context: ToolContext | None = None,
    credential: AuthCredential | None = None,
) -> dict[str, Any]:
  """Searches SharePoint sites and fetches the user's Microsoft 365 profile via on-demand 3LO.

  Args:
      search_query: Keyword to search SharePoint sites (default '*' lists accessible sites).
  """
  user_id = getattr(tool_context, "user_id", None) or "default-user"
  access_token = _extract_access_token(credential)
  if not access_token:
    return {
        "status": "PENDING_USER_OAUTH",
        "message": "Waiting for user to authorize sharepoint-provider.",
    }

  headers = {"Authorization": f"Bearer {access_token}"}
  with httpx.Client(timeout=15.0) as client:
    me_resp = client.get("https://graph.microsoft.com/v1.0/me", headers=headers)
    me_data = (
        me_resp.json()
        if me_resp.status_code == 200
        else {"error": me_resp.text}
    )

    sites_resp = client.get(
        "https://graph.microsoft.com/v1.0/sites",
        headers=headers,
        params={"search": search_query, "$select": "id,name,displayName,webUrl"},
    )
    sites_data = (
        sites_resp.json().get("value", [])
        if sites_resp.status_code == 200
        else [{"error": sites_resp.text}]
    )

    return {
        "status": "SUCCESS",
        "user_id": user_id,
        "microsoft_profile": me_data,
        "sharepoint_sites": sites_data,
    }


google_user_info_tool = GcpAuthenticatedFunctionTool(
    func=get_google_user_info,
    auth_config=AuthConfig(
        auth_scheme=GcpAuthProviderScheme(
            name=(
                f"projects/{PROJECT_ID}/locations/{AUTH_PROVIDER_LOCATION}"
                "/authProviders/google-workspace-provider"
            ),
            scopes=[
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
            ],
        ),
    ),
)

sharepoint_sites_tool = GcpAuthenticatedFunctionTool(
    func=get_sharepoint_sites,
    auth_config=AuthConfig(
        auth_scheme=GcpAuthProviderScheme(
            name=(
                f"projects/{PROJECT_ID}/locations/{AUTH_PROVIDER_LOCATION}"
                "/authProviders/sharepoint-provider"
            ),
            scopes=[
                "openid",
                "offline_access",
                "https://graph.microsoft.com/User.Read",
                "https://graph.microsoft.com/Sites.Read.All",
            ],
        ),
    ),
)

