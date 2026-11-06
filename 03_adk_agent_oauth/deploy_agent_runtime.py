"""
Step 1: Deploy `EnterpriseRouterAgent` to Vertex AI Agent Runtime with `AGENT_IDENTITY`.

Once deployed, the agent receives its own cryptographic SPIFFE Agent Identity:
  principal://agents.global.org-{ORG_ID}.system.id.goog/resources/aiplatform/projects/{PROJECT_NUMBER}/locations/{LOCATION}/reasoningEngines/{ENGINE_ID}

and automatically appears in Google Cloud Console -> Agent Registry, where you can
attach the 3LO Auth Provider (`google-workspace-provider`) under the "Identity" tab.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Ensure package imports resolve cleanly
CURRENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = CURRENT_DIR.parent
sys.path.insert(0, str(CURRENT_DIR))

load_dotenv(REPO_ROOT / ".env")

import vertexai
from vertexai import types
from vertexai.agent_engines import AdkApp

from enterprise_router_agent.agent import root_agent

PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "customersprototype")
LOCATION = os.environ.get("AUTH_PROVIDER_LOCATION", "us-central1")
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION
os.environ["GOOGLE_CLOUD_QUOTA_PROJECT"] = PROJECT_ID
STAGING_BUCKET = os.environ.get(
    "AGENT_STAGING_BUCKET", f"gs://{PROJECT_ID}-agent-staging"
)
EXISTING_ENGINE_ID = os.environ.get("REASONING_ENGINE_ID", "9204924705641857024")


def deploy_agent_with_identity() -> None:
    print("==================================================================")
    print(" Deploying EnterpriseRouterAgent to Vertex AI Agent Runtime")
    print(f" Project:  {PROJECT_ID}")
    print(f" Location: {LOCATION}")
    print(" Identity: AGENT_IDENTITY (SPIFFE principal)")
    print("==================================================================")

    # Initialize global Vertex AI config so AdkApp binds to PROJECT_ID (not gcloud default)
    vertexai.init(project=PROJECT_ID, location=LOCATION, staging_bucket=STAGING_BUCKET)

    # Initialize Vertex AI client with v1beta1 for Agent Identity support
    client = vertexai.Client(
        project=PROJECT_ID,
        location=LOCATION,
        http_options=dict(api_version="v1beta1"),
    )

    adk_app = AdkApp(agent=root_agent, enable_tracing=True)

    # Ensure GCS staging bucket exists
    from google.cloud import storage

    bucket_name = STAGING_BUCKET.replace("gs://", "").strip("/")
    storage_client = storage.Client(project=PROJECT_ID)
    bucket = storage_client.bucket(bucket_name)
    if not bucket.exists():
        print(f"Creating staging bucket gs://{bucket_name} in {LOCATION}...")
        storage_client.create_bucket(bucket, location=LOCATION)

    # Run from 03_adk_agent_oauth/ so extra_packages unpacks enterprise_router_agent at top level
    os.chdir(CURRENT_DIR)

    agent_config = {
        "display_name": "enterprise-router-on-demand-3lo",
        "description": (
            "Single Root Agent with PolicySubAgent (no auth), "
            "GoogleWorkspaceSubAgent (Google 3LO), and SharePointSubAgent (SharePoint 3LO)"
        ),
        "identity_type": types.IdentityType.AGENT_IDENTITY,
        "staging_bucket": STAGING_BUCKET,
        "requirements": [
            "google-cloud-aiplatform[agent_engines]==2.7.0",
            "google-adk==2.13.0",
            "google-cloud-agentidentitycredentials==0.1.2",
            "google-cloud-iamconnectorcredentials==0.1.2",
            "cloudpickle==3.1.2",
            "pydantic==2.14.1",
            "httpx==0.28.1",
        ],
        "extra_packages": ["enterprise_router_agent"],
        "env_vars": {
            "GCP_PROJECT_ID": PROJECT_ID,
            "AUTH_PROVIDER_LOCATION": LOCATION,
        },
    }

    if EXISTING_ENGINE_ID:
        engine_name = f"projects/{PROJECT_ID}/locations/{LOCATION}/reasoningEngines/{EXISTING_ENGINE_ID}"
        print(f"Updating existing Reasoning Engine in-place: {engine_name}")
        remote_agent = client.agent_engines.update(
            name=engine_name,
            agent=adk_app,
            config=agent_config,
        )
    else:
        remote_agent = client.agent_engines.create(
            agent=adk_app,
            config=agent_config,
        )

    resource_name = remote_agent.api_resource.name
    print("\n✓ Agent deployed successfully!")
    print(f"  Reasoning Engine Resource Name: {resource_name}")
    effective_identity = getattr(
        remote_agent.api_resource.spec, "effective_identity", None
    )
    if effective_identity:
        print(f"  Agent Identity (SPIFFE ID):     {effective_identity}")

    print("\nNext Step (Console UI):")
    print(
        f"  1. Open Agent Registry: https://console.cloud.google.com/agent-platform/agent-registry?project={PROJECT_ID}"
    )
    print(
        "  2. Click 'enterprise-router-on-demand-3lo' -> 'Identity' tab -> '+ Add auth provider'"
    )
    print("  3. Provider 1: google-workspace-provider | Type: OAuth (3 legged)")
    print("  4. Provider 2: sharepoint-provider       | Type: OAuth (3 legged)")


if __name__ == "__main__":
    deploy_agent_with_identity()
