"""Reference ADK Pattern: Root Router Agent + Specialist Subagent with On-Demand Tool OAuth.

This file is a reference blueprint showing how to structure a multi-tool / multi-agent
Google ADK application so that:
  1. You do NOT attach `authorizationConfig` at the top-level Gemini Enterprise agent
     registration (which would require Gemini Enterprise's native UI popup and cause
     `:streamAssist` calls from a custom UI to fail).
  2. Internal knowledge tools (`get_sla_policy`) run immediately with zero OAuth popups.
  3. Protected third-party tools (`list_servicenow_incidents`) use ADK's built-in
     `tool_context.request_credential(auth_config)` and `tool_context.get_auth_response(auth_config)`
     to request 3-legged OAuth (3LO) on demand only when that specific tool is called.
"""

import os
from typing import Any, Dict
from google.adk.agents import Agent
from google.adk.tools import ToolContext
import requests

SERVICENOW_INSTANCE_URL = os.environ.get(
    "SERVICENOW_INSTANCE_URL", "https://your-instance.service-now.com"
)


def get_sla_policy(topic: str = "outage_sla") -> Dict[str, Any]:
  """Internal knowledge tool — requires NO third-party OAuth login."""
  return {
      "topic": topic,
      "policy": "Priority 1 incidents require dispatch within 15 minutes; Priority 2 within 2 hours.",
  }


def list_servicenow_incidents(
    tool_context: ToolContext,
    auth_config: Any,
) -> Dict[str, Any]:
  """Protected ServiceNow tool — requests 3LO on demand via ADK ToolContext."""
  # 1. Check if the user has already completed the 3LO flow in this session
  auth_response = tool_context.get_auth_response(auth_config)
  access_token = getattr(auth_response, "access_token", None) if auth_response else None

  # 2. If no token is present, emit `adk_request_credential` back to the custom UI
  if not access_token:
    tool_context.request_credential(auth_config)
    return {
        "status": "PENDING_USER_OAUTH",
        "message": "Prompting user in custom UI to authorize ServiceNow access.",
    }

  # 3. Call ServiceNow REST API using the user's 3LO access token
  resp = requests.get(
      f"{SERVICENOW_INSTANCE_URL}/api/now/table/incident",
      headers={"Authorization": f"Bearer {access_token}"},
      timeout=20,
  )
  resp.raise_for_status()
  return resp.json()


# Specialist Subagent for ServiceNow (only invoked when user asks about tickets/incidents)
servicenow_subagent = Agent(
    name="servicenow_specialist",
    model="gemini-2.5-flash",
    description="Handles ServiceNow incident lookup and ticket creation using on-demand 3LO.",
    instruction="Use `list_servicenow_incidents` to fetch the user's ServiceNow tickets.",
    tools=[list_servicenow_incidents],
)

# Root Router Agent registered in Gemini Enterprise (NO top-level authorizationConfig)
root_agent = Agent(
    name="enterprise_assistant_router",
    model="gemini-2.5-flash",
    description="Routes general policy questions directly and delegates ServiceNow tasks to servicenow_specialist.",
    instruction=(
        "Use `get_sla_policy` for general SLA policy questions. "
        "Delegate ServiceNow ticket or incident requests to `servicenow_specialist`."
    ),
    tools=[get_sla_policy],
    sub_agents=[servicenow_subagent],
)
