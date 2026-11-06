# How to Handle User, Connector, and Agent Auth in Gemini Enterprise with a Custom UI

Companion code, local Test UIs, and CLI reference for authenticating a custom enterprise web UI against the Gemini Enterprise **`:streamAssist`** REST API across all three authentication layers:

1. **Part 1 (`01_user_auth_streamassist/`): Authenticating to `:streamAssist`**
   - **Pattern 1A:** Pure Google Identity (`Google Login UI` $\rightarrow$ `:streamAssist`)
   - **Pattern 1B:** Pure Workforce Identity Federation (`External IdP UI` $\rightarrow$ `Google STS (workforcePools)` $\rightarrow$ `:streamAssist`)
   - **Pattern 1C:** Direct Workload Identity Federation for Bots / Backend Services (`Workload JWT` $\rightarrow$ `Google STS (workloadIdentityPools)` $\rightarrow$ `:streamAssist`)
2. **Part 2 (`02_federated_connector_oauth/`): Authenticating Federated Connectors through a Custom UI**
   - Discovering engine connectors via `widgetConfigs/default_search_widget_config`, checking and storing per-user 3-legged OAuth (3LO) tokens in Gemini Enterprise's managed **Keystore** via `dataConnector:acquireAccessToken` and `dataConnector:acquireAndStoreRefreshToken`, and running grounded `:streamAssist` queries (`toolsSpec.vertexAiSearchSpec.dataStoreSpecs`).
3. **Part 3 (`03_adk_agent_oauth/`): Authenticating Custom ADK Agents with Individual Tool OAuth**
   - Deploying a high-code ADK agent (`EnterpriseRouterAgent`) to Vertex AI Agent Runtime with cryptographic **`AGENT_IDENTITY`** (SPIFFE ID), wrapping protected sub-agent tools with ADK's built-in **`AuthenticatedFunctionTool`** + **Google Cloud Auth Manager (`GcpAuthProvider`)**, and completing on-demand 3LO from a Custom UI via `:streamAssist` (`requiredAuthorizations`) and **`{session}:validateUserAuthorization`**.

![End-to-End Identity Architecture](docs/diagrams/fig1-end-to-end-identity-architecture.png)

---

## Repository Structure

```text
gemini-enterprise-custom-ui-auth/
├── .env.example                        # Template for project, engine, WIF, connector, and agent IDs
├── requirements.txt                    # Minimal Python dependencies
├── README.md                           # Step-by-step Python, Test UI & curl reference
├── docs/diagrams/                      # Architecture & sequence diagrams (PNG + SVG)
├── 01_user_auth_streamassist/
│   ├── pattern_a_google_identity.py    # Pattern 1A: Google Identity -> :streamAssist
│   ├── pattern_b_workforce_sts.py      # Pattern 1B: Workforce Identity (Entra/Okta) -> STS -> :streamAssist
│   └── pattern_c_workload_identity.py  # Pattern 1C: Direct Workload Identity (Bots/Services) -> :streamAssist
├── 02_federated_connector_oauth/
│   ├── connector_oauth_flow.py         # CLI & library for widgetConfigs, acquireAccessToken, acquireAndStoreRefreshToken
│   ├── test_ui.py                      # Local Custom UI server (http://localhost:8502) for Federated Connector 3LO
│   └── test_ui.html                    # Frontend UI for Federated Connector 3LO & grounded chat
└── 03_adk_agent_oauth/
    ├── enterprise_router_agent/        # ADK Root Router + PolicySubAgent (no auth) + Google/SharePoint 3LO Sub-Agents
    │   ├── __init__.py
    │   ├── agent.py
    │   └── tools.py                    # GcpAuthenticatedFunctionTool + GcpAuthProviderScheme
    ├── deploy_agent_runtime.py         # Deploys EnterpriseRouterAgent to Vertex AI Agent Runtime with AGENT_IDENTITY
    ├── query_registered_agent.py       # CLI & library for :streamAssist, :validateUserAuthorization, and token revoke
    ├── test_ui.py                      # Local Custom UI server (http://localhost:8503) with /validateUserId auto-callback
    └── test_ui.html                    # Frontend UI for ADK Agent On-Demand 3LO & auto query re-run
```

---

## Prerequisites & Setup

1. **Existing Gemini Enterprise Setup:**
   - A Gemini Enterprise Engine (`GE_GOOGLE_ID_ENGINE_ID` or `GE_WIF_ENGINE_ID`) configured in your Google Cloud project.
   - Your identity foundation already configured (Google Identity or a Workforce Identity Pool for human users).
   - **User IAM Role & Gemini Enterprise License (Required for All Human User Patterns):**
     Every end user (or group) calling `:streamAssist` must have **both**:
     1. **Cloud IAM Role:** **Discovery Engine User** (`roles/discoveryengine.user`) granted in **GCP Console $\rightarrow$ IAM & Admin $\rightarrow$ IAM** (provides the `discoveryengine.assistants.assist` permission).
     2. **Gemini Enterprise License:** An active seat assigned in **Gemini Enterprise $\rightarrow$ Settings $\rightarrow$ License** (assigned directly to the user/group or via **Automatic license assignment** on first access).
   - *(Optional for Part 2)* One or more Federated Connectors (e.g., Microsoft OneDrive, SharePoint, Jira, ServiceNow) attached to your Gemini Enterprise Engine.
   - *(Optional for Part 3)* A custom high-code ADK agent deployed to Vertex AI Agent Runtime (`AGENT_IDENTITY`), connected to Google Cloud Auth Manager `authProviders`, and registered in Gemini Enterprise (`GE_GOOGLE_ID_AGENT_ID` or `GE_WIF_AGENT_ID`).

2. **Install Dependencies & Configure `.env`:**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your GCP_PROJECT_ID, Engine IDs, and Agent IDs
set -a && source .env && set +a
```

---

## Part 1: Authenticating Users to `:streamAssist`

![Two Patterns to Authenticate Users to :streamAssist](docs/diagrams/fig2-inbound-streamassist-scenarios.png)

### Pattern 1A: Pure Google Identity (`pattern_a_google_identity.py`)

Use when both your Custom UI and Gemini Enterprise use **Google Identity** (Google Workspace or Cloud Identity).

#### Prerequisites for Custom UI Sign-In (`--browser-login`)
When building a Custom UI outside `vertexaisearch.cloud.google.com`, your web application needs a **Google OAuth 2.0 Client ID** to sign the user in and obtain a user-scoped access token (`ya29...`):
1. In **GCP Console $\rightarrow$ APIs & Services $\rightarrow$ Credentials $\rightarrow$ + Create Credentials $\rightarrow$ OAuth client ID**:
   - **Application type:** `Web application`
   - **Authorized redirect URIs:** `http://localhost:8085/callback` (for local testing) or your production Custom UI callback URL
   - **OAuth Scope:** `openid email profile https://www.googleapis.com/auth/cloud-platform`
2. Set `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in `.env`.
3. **IAM & License:** Ensure the signed-in Google user (or Google Group) has the **Discovery Engine User** (`roles/discoveryengine.user`) IAM role on the project **and** an active **Gemini Enterprise license**.

**Run with Python:**
```bash
# Option 1: Interactive "Sign in with Google" browser OAuth 2.0 flow (real Custom UI flow)
python 01_user_auth_streamassist/pattern_a_google_identity.py \
  --browser-login \
  --query "What are our Grid Operations SLA policies?"

# Option 2: Quick CLI test using gcloud user credentials
python 01_user_auth_streamassist/pattern_a_google_identity.py \
  --query "What are our Grid Operations SLA policies?"

# Option 3: Route the query directly to a registered ADK agent (--agent-id)
python 01_user_auth_streamassist/pattern_a_google_identity.py \
  --agent-id "$GE_GOOGLE_ID_AGENT_ID" \
  --query "What are our Grid Operations SLA policies?"
```

**Equivalent `curl` command:**
```bash
curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${GE_GOOGLE_ID_LOCATION}/collections/default_collection/engines/${GE_GOOGLE_ID_ENGINE_ID}/assistants/default_assistant:streamAssist" \
  -d "{
    \"query\": {\"text\": \"${QUERY}\"}
  }"
```

---

### Pattern 1B: Pure Workforce Identity Federation (`pattern_b_workforce_sts.py`)

Use when both your Custom UI and Gemini Enterprise use an **external IdP** (Microsoft Entra ID, Okta, Ping Identity) mapped to a Google Cloud **Workforce Identity Pool** (`workforcePools`).

> **Note:** Always use **Workforce Identity Federation (`workforcePools`)** for human users—not Workload Identity Federation (`workloadIdentityPools`). Gemini Enterprise user licenses, per-user document ACLs, and Agent `User permissions` require Google Identity or `workforcePools` principals.

#### Prerequisites
1. **External IdP App Registration (e.g., Microsoft Entra ID):**
   - Register an OIDC application in your IdP (matching the `clientId` configured on your Workforce Identity Pool OIDC provider).
   - Add `http://localhost:8000` (or your Custom UI origin) under **Authentication $\rightarrow$ Single-page application (SPA)** redirect URIs.
   - Set `ENTRA_TENANT_ID` and `ENTRA_CLIENT_ID` in `.env`.
2. **IAM Role:** Grant the **Discovery Engine User** (`roles/discoveryengine.user`) IAM role to your Workforce Pool user (`principal://iam.googleapis.com/locations/global/workforcePools/${WIF_POOL_ID}/subject/${USER_SUBJECT}`) or all pool users (`principalSet://iam.googleapis.com/locations/global/workforcePools/${WIF_POOL_ID}/*`).
3. **Gemini Enterprise License:** Assign a Gemini Enterprise license to the federated user/group (or enable **Automatic license assignment**) in **Gemini Enterprise $\rightarrow$ Settings $\rightarrow$ License**.

**Run with Python:**
```bash
# Option 1: Interactive "Sign in with Microsoft Entra ID" browser login (PKCE -> STS -> :streamAssist)
python 01_user_auth_streamassist/pattern_b_workforce_sts.py \
  --browser-login \
  --query "What are our Grid Operations SLA policies?"

# Option 2: Pass an existing external IdP OIDC JWT directly
python 01_user_auth_streamassist/pattern_b_workforce_sts.py \
  --idp-token "<ENTRA_OR_OKTA_JWT>" \
  --query "What are our Grid Operations SLA policies?"
```

**Equivalent `curl` commands (Step 1: STS Exchange $\rightarrow$ Step 2: `:streamAssist`):**
```bash
# 1. Exchange the external IdP OIDC token at Google Security Token Service (STS)
STS_TOKEN=$(curl -s -X POST "https://sts.googleapis.com/v1/token" \
  -H "Content-Type: application/json" \
  -d "{
    \"audience\": \"//iam.googleapis.com/locations/global/workforcePools/${WIF_POOL_ID}/providers/${WIF_PROVIDER_ID}\",
    \"grantType\": \"urn:ietf:params:oauth:grant-type:token-exchange\",
    \"requestedTokenType\": \"urn:ietf:params:oauth:token-type:access_token\",
    \"scope\": \"https://www.googleapis.com/auth/cloud-platform\",
    \"subjectTokenType\": \"urn:ietf:params:oauth:token-type:id_token\",
    \"subjectToken\": \"${EXTERNAL_IDP_TOKEN}\"
  }" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 2. Call :streamAssist using the federated STS access token
curl -s -X POST \
  -H "Authorization: Bearer ${STS_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
  "https://${GE_WIF_LOCATION}-discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${GE_WIF_LOCATION}/collections/default_collection/engines/${GE_WIF_ENGINE_ID}/assistants/default_assistant:streamAssist" \
  -d "{
    \"query\": {\"text\": \"${QUERY}\"}
  }"
```

---

### Pattern 1C (Code Bonus): Direct Workload Identity Federation for Non-Human Bots (`pattern_c_workload_identity.py`)

Use when a **non-human backend service, microservice, or bot** needs to call `:streamAssist` using a project-scoped **Workload Identity Pool** (`workloadIdentityPools`) directly via Google STS (**zero Service Account required**).

```bash
python 01_user_auth_streamassist/pattern_c_workload_identity.py \
  --query "What are our Grid Operations SLA policies?"
```

---

## Part 2: Authenticating Federated Connectors through a Custom UI

![Federated Connector 3LO Sequence](docs/diagrams/fig3-federated-connector-3lo-flow.png)

When your Custom UI queries a Federated or Action Connector (OneDrive, SharePoint, Jira, Confluence, ServiceNow, Google Drive), Gemini Enterprise uses its managed **Keystore** to store and mint per-user 3-legged OAuth (3LO) tokens:

1. **Discover Engine Connectors (`GET .../widgetConfigs/default_search_widget_config`):** Returns `collectionComponents[]` attached to the engine, each connector's `dataStoreComponents[]`, and the 3LO `authorizationUri`.
2. **Check Live Keystore Token Status (`POST .../collections/{collection_id}/dataConnector:acquireAccessToken`):** Verifies whether Gemini Enterprise's Keystore can mint a live access token for the signed-in user (`USER_AUTHORIZED`) or requires 3LO consent (`NOT_AUTHORIZED`).
3. **Open 3LO Consent Popup (`https://vertexaisearch.cloud.google.com/oauth-redirect`):** The user signs in to the 3P Auth Server and grants consent; Google's redirect relay receives `?code=...&state=...`.
4. **Exchange Code & Store Refresh Token in GE Keystore (`POST .../collections/{collection_id}/dataConnector:acquireAndStoreRefreshToken`):** Your backend passes `fullRedirectUri` to Gemini Enterprise, which exchanges the code and saves the encrypted `refresh_token` and `access_token` in its managed **Keystore**.
5. **Query `:streamAssist` (`POST .../assistants/default_assistant:streamAssist`):** Passing `toolsSpec.vertexAiSearchSpec.dataStoreSpecs` grounds the response on live documents from the authorized connector.

> **Note on Custom UI Cross-Origin `postMessage` (`/oauth-redirect`) & `dataStoreSpecs`:**
> - By default, `https://vertexaisearch.cloud.google.com/oauth-redirect` restricts `window.opener.postMessage` to the `https://vertexaisearch.cloud.google.com` origin. Contact your Google Cloud account team to enable cross-origin `postMessage` support for your Custom UI origin (or paste the `/oauth-redirect?code=...&state=...` URL into the Test UI fallback box during local testing).
> - Passing `toolsSpec.vertexAiSearchSpec.dataStoreSpecs` in `:streamAssist` ensures Federated Connectors are queried explicitly alongside enterprise search data stores.

### Option A: Run the Interactive Local Test UI (`http://localhost:8502`)
```bash
python 02_federated_connector_oauth/test_ui.py
# Open http://localhost:8502 in your browser
```

### Option B: Run Step-by-Step via CLI (`connector_oauth_flow.py`)

```bash
# Step 1: List all connectors on the engine and check Keystore auth status
python 02_federated_connector_oauth/connector_oauth_flow.py --step list

# Step 2: Check a specific connector collection and print its authorizationUri
python 02_federated_connector_oauth/connector_oauth_flow.py \
  --step check \
  --collection-id "${CONNECTOR_COLLECTION_ID}"

# Step 3: Exchange the OAuth redirect URL and store refresh_token in GE Keystore
python 02_federated_connector_oauth/connector_oauth_flow.py \
  --step store \
  --collection-id "${CONNECTOR_COLLECTION_ID}" \
  --full-redirect-uri "https://vertexaisearch.cloud.google.com/oauth-redirect?code=...&state=..."

# Step 4: Run a grounded :streamAssist query across the authorized connectors
python 02_federated_connector_oauth/connector_oauth_flow.py \
  --step query \
  --query "Can you check my OneDrive for Cymbal Sigma notes?"
```

**Equivalent `curl` commands:**
```bash
# 1. Discover connectors and dataStoreSpecs attached to the engine
curl -s -X GET \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${GE_GOOGLE_ID_LOCATION}/collections/default_collection/engines/${GE_GOOGLE_ID_ENGINE_ID}/widgetConfigs/default_search_widget_config"

# 2. Check if a live access token exists in the GE Keystore for a connector
curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${CONNECTOR_LOCATION}/collections/${CONNECTOR_COLLECTION_ID}/dataConnector:acquireAccessToken" \
  -d "{}"

# 3. Exchange OAuth code and store refresh token in the GE Keystore
curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${CONNECTOR_LOCATION}/collections/${CONNECTOR_COLLECTION_ID}/dataConnector:acquireAndStoreRefreshToken" \
  -d "{
    \"fullRedirectUri\": \"https://vertexaisearch.cloud.google.com/oauth-redirect?code=...&state=...\"
  }"
```

---

## Part 3: Authenticating Custom ADK Agents with Individual Tool OAuth

![Custom ADK Agent On-Demand 3LO Sequence](docs/diagrams/fig4-adk-subagent-on-demand-3lo.png)

When a custom ADK agent has multiple tools or sub-agents (e.g., an internal policy tool that needs no user auth, a Google Workspace tool that needs Google 3LO, and a SharePoint tool that needs Microsoft 3LO), attaching top-level `authorizationConfig` at agent registration blocks the entire agent behind upfront OAuth.

Instead, we combine **four building blocks**:
1. **Deploy with `AGENT_IDENTITY` (`03_adk_agent_oauth/deploy_agent_runtime.py`):** Deploys `EnterpriseRouterAgent` to Vertex AI Agent Runtime with a cryptographic SPIFFE identity (`principal://agents.global.org-...`).
2. **Configure 3LO Auth Providers in Google Cloud Auth Manager (`authProviders`):** Store your 3P OAuth Client ID, Client Secret, and `defaultContinueUri` (`http://localhost:8503/validateUserId` for local Custom UI or `https://your-custom-ui.example.com/validateUserId` in production) inside GCP Auth Manager (`projects/${GCP_PROJECT_ID}/locations/us-central1/authProviders/{provider_id}`)—no OAuth secrets in `.env` or agent code.
3. **Wrap Protected Tools with `AuthenticatedFunctionTool` + `GcpAuthProviderScheme` (`03_adk_agent_oauth/enterprise_router_agent/tools.py`):**
   - Non-3LO tools (`get_company_sla_policy`) run immediately with zero OAuth prompts.
   - 3LO tools (`get_google_user_info`, `get_sharepoint_sites`) automatically query Auth Manager Vault (`RetrieveCredentials`) and emit `adk_request_credential` only when the user has not yet authorized that specific tool.
4. **Finalize User Authorization via `{session}:validateUserAuthorization` (`03_adk_agent_oauth/query_registered_agent.py`):**
   - Gemini Enterprise intercepts `adk_request_credential` and returns `answer.requiredAuthorizations[]` (`authorizationUri`, `displayName`) and `sessionInfo.session` in `:streamAssist`.
   - After the user approves the popup, Auth Manager exchanges the code at `/oauthcallback` and redirects the popup to your Custom UI's `/validateUserId?user_id_validation_state=...` endpoint.
   - Your Custom UI backend calls `POST /v1alpha/{session}:validateUserAuthorization` with `userIdValidationState` and `downstreamConnector` to bind and store the token in Auth Manager Vault (`FinalizeCredentials`), then automatically re-runs the query on the same `session`.

> **Note on Project Allowlisting (`adk_tool_auth_enabled`):**
> On-demand ADK tool-level 3LO via `:streamAssist` (`requiredAuthorizations` and `{session}:validateUserAuthorization`) currently requires enabling the `adk_tool_auth_enabled` feature flag on your Google Cloud project. Contact your Google Cloud account team to allowlist your project ID/number before testing Part 3.

---

### Step 1: Deploy `EnterpriseRouterAgent` to Vertex AI Agent Runtime
```bash
python 03_adk_agent_oauth/deploy_agent_runtime.py
```

### Step 2: Run the Interactive Local Custom UI (`http://localhost:8503`)
```bash
python 03_adk_agent_oauth/test_ui.py
# Open http://localhost:8503 in your browser
```
- Click **"1. PolicySubAgent (No Auth)"** $\rightarrow$ Returns internal SLA policy immediately without any OAuth prompt.
- Click **"2. GoogleWorkspaceSubAgent (Google 3LO)"** or **"3. SharePointSubAgent (Microsoft 3LO)"** $\rightarrow$ Renders the **On-Demand 3LO Authorization Required** card.
- Click **"Authorize in Popup (3LO)"** $\rightarrow$ Approving access in the popup redirects to `http://localhost:8503/validateUserId`, automatically closes the popup, calls `{session}:validateUserAuthorization`, and **automatically re-runs your query**!
- Click **"Clear Stored 3LO Tokens (Reset Auth Vault)"** anytime to delete your cached user tokens from Google Cloud Auth Manager Vault and re-test the 3LO popup from scratch.

---

### Step 3: Run via CLI (`query_registered_agent.py`)

```bash
# 1. Query internal policy sub-agent (no 3LO required)
python 03_adk_agent_oauth/query_registered_agent.py \
  --step query \
  --query "What are our Grid Operations SLA policies?"

# 2. Query 3LO-protected sub-agent (returns answer if vaulted, or requiredAuthorizations[])
python 03_adk_agent_oauth/query_registered_agent.py \
  --step query \
  --query "Search my SharePoint sites"

# 3. Validate user authorization using the redirect URL from the 3LO popup
python 03_adk_agent_oauth/query_registered_agent.py \
  --step validate \
  --session "projects/${GCP_PROJECT_ID}/locations/${GE_GOOGLE_ID_LOCATION}/collections/default_collection/engines/${GE_GOOGLE_ID_ENGINE_ID}/sessions/<SESSION_ID>" \
  --downstream-connector "projects/${GCP_PROJECT_ID}/locations/${AUTH_PROVIDER_LOCATION}/authProviders/${SHAREPOINT_AUTH_PROVIDER_ID}" \
  --full-redirect-uri "http://localhost:8503/validateUserId?user_id_validation_state=..."

# 4. Revoke stored 3LO tokens in Auth Manager Vault to re-test the consent popup
python 03_adk_agent_oauth/query_registered_agent.py --step revoke
```

**Equivalent `curl` commands (Step 1: `:streamAssist` $\rightarrow$ Step 2: `{session}:validateUserAuthorization`):**
```bash
# 1. Query the registered ADK agent via :streamAssist
curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${GE_GOOGLE_ID_LOCATION}/collections/default_collection/engines/${GE_GOOGLE_ID_ENGINE_ID}/assistants/default_assistant:streamAssist" \
  -d "{
    \"query\": {\"text\": \"Search my SharePoint sites\"},
    \"answerGenerationMode\": \"AGENT\",
    \"agentsSpec\": {
      \"agentSpecs\": [{\"agentId\": \"${GE_GOOGLE_ID_AGENT_ID}\"}]
    }
  }"

# 2. Finalize 3LO authorization on the session (:validateUserAuthorization)
curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/${SESSION_NAME}:validateUserAuthorization" \
  -d "{
    \"session\": \"${SESSION_NAME}\",
    \"userIdValidationState\": \"${USER_ID_VALIDATION_STATE}\",
    \"downstreamConnector\": \"projects/${GCP_PROJECT_ID}/locations/${AUTH_PROVIDER_LOCATION}/authProviders/${SHAREPOINT_AUTH_PROVIDER_ID}\"
  }"
```
