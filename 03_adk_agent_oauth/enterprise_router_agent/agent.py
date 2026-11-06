"""Enterprise Router Agent definition (only agent routing and sub-agent logic)."""

from google.adk.agents.llm_agent import LlmAgent
from google.genai import types

from .tools import (
    get_company_sla_policy,
    google_user_info_tool,
    sharepoint_sites_tool,
)

policy_sub_agent = LlmAgent(
    name="PolicySubAgent",
    model="gemini-2.5-flash",
    description="Answers internal company SLA and HR policy questions (no auth required).",
    instruction=(
        "You are the Enterprise Policy Specialist. Use `get_company_sla_policy` "
        "to answer questions about Grid Operations SLAs, HR leave policies, or IT SLAs."
    ),
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[get_company_sla_policy],
)

google_sub_agent = LlmAgent(
    name="GoogleWorkspaceSubAgent",
    model="gemini-2.5-flash",
    description="Fetches the logged-in user's Google profile and email using on-demand 3LO.",
    instruction=(
        "You are the Google Workspace Specialist. Use `get_google_user_info` "
        "to retrieve the user's Google profile details (name, email, picture)."
    ),
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[google_user_info_tool],
)

sharepoint_sub_agent = LlmAgent(
    name="SharePointSubAgent",
    model="gemini-2.5-flash",
    description="Searches SharePoint sites and fetches Microsoft 365 profile using on-demand 3LO.",
    instruction=(
        "You are the SharePoint Specialist. Use `get_sharepoint_sites` "
        "to search SharePoint sites or retrieve the user's Microsoft 365 profile."
    ),
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[sharepoint_sites_tool],
)

root_agent = LlmAgent(
    name="EnterpriseRouterAgent",
    model="gemini-2.5-flash",
    description="Routes queries to PolicySubAgent, GoogleWorkspaceSubAgent, or SharePointSubAgent.",
    instruction=(
        "You are the Enterprise Router Agent.\n"
        "1. Route internal company SLA, Grid Operations, HR, or IT policy questions "
        "to `PolicySubAgent`.\n"
        "2. Route questions about the user's Google profile or Google email "
        "to `GoogleWorkspaceSubAgent`.\n"
        "3. Route questions about SharePoint sites or Microsoft 365 profile "
        "to `SharePointSubAgent`."
    ),
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    sub_agents=[policy_sub_agent, google_sub_agent, sharepoint_sub_agent],
)
