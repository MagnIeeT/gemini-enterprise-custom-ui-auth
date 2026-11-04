# How to Handle User, Connector, and Agent Auth in Gemini Enterprise with a Custom UI

Companion code and CLI reference for authenticating a custom enterprise web UI against the Gemini Enterprise **`:streamAssist`** REST API across all three authentication layers:

1. **Part 1 (`01_user_auth_streamassist/`): Authenticating users to `:streamAssist`**
   - **Pattern 1A:** Pure Google Identity (`Google Login UI` -> `:streamAssist`)
   - **Pattern 1B:** Pure Workforce Identity Federation (`External IdP UI` -> `Google STS` -> `:streamAssist`)
   - **Pattern 1C:** Hybrid login (`External IdP UI` -> `Keyless Domain-Wide Delegation (:signJwt)` -> `:streamAssist`)
2. **Part 2 (`02_federated_connector_oauth/`): Authenticating Federated Connectors through a custom UI**
   - Checking and storing per-user 3-legged OAuth (3LO) tokens in Gemini Enterprise's managed `Keystore` via `dataConnector:acquireAccessToken` and `dataConnector:acquireAndStoreRefreshToken`.
3. **Part 3 (`03_adk_agent_oauth/`): Authenticating custom ADK agents with individual tool OAuth**
   - Calling a registered high-code ADK agent via `:streamAssist` (`agentsSpec.agentSpecs`) and triggering third-party OAuth on demand per tool (`request_credential`) instead of blocking the entire agent at registration time.

![End-to-End Identity Architecture](docs/diagrams/fig1-end-to-end-identity-architecture.png)

---

## Repository Structure

```text
gemini-enterprise-custom-ui-auth/
├── .env.example                    # Template for project, engine, WIF, DWD, connector, and agent IDs
├── requirements.txt                # Minimal Python dependencies
├── README.md                       # Step-by-step Python & curl commands
├── docs/diagrams/                  # Architecture & sequence diagrams (PNG + SVG)
├── 01_user_auth_streamassist/
│   ├── pattern_a_google_identity.py
│   ├── pattern_b_workforce_sts.py
│   └── pattern_c_hybrid_keyless_dwd.py
├── 02_federated_connector_oauth/
│   └── connector_oauth_flow.py
└── 03_adk_agent_oauth/
    ├── query_registered_agent.py
    └── sample_adk_subagent_oauth.py
```

---

## Prerequisites & Setup

1. **Existing Gemini Enterprise Setup:**
   - A Gemini Enterprise Engine (`GE_GOOGLE_ID_ENGINE_ID` or `GE_WIF_ENGINE_ID`) configured in your Google Cloud project.
   - Your identity foundation already configured (Google Identity, a Workforce Identity Pool, or a service account with Domain-Wide Delegation).
   - *(Optional for Part 2)* A Federated Connector configured on a Discovery Engine collection.
   - *(Optional for Part 3)* A custom high-code ADK agent deployed to Vertex AI Agent Engine and registered in Gemini Enterprise (`GE_GOOGLE_ID_AGENT_ID` or `GE_WIF_AGENT_ID`).

2. **Install Dependencies & Configure `.env`:**

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your GCP_PROJECT_ID, Engine IDs, and Agent IDs
set -a && source .env && set +a
```

---

## Part 1: Three Patterns to Authenticate Users to `:streamAssist`

![Three Patterns to Authenticate Users to :streamAssist](docs/diagrams/fig2-inbound-streamassist-scenarios.png)

### Pattern 1A: Pure Google Identity (`pattern_a_google_identity.py`)

Use when both your custom UI and Gemini Enterprise use **Google Identity** (Google Workspace or Cloud Identity).

**Run with Python:**
```bash
# Query the default Gemini Enterprise assistant
python 01_user_auth_streamassist/pattern_a_google_identity.py \
  --query "What are our Grid Operations SLA policies?"

# Or route the query to a specific registered agent (--agent-id)
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

Use when both your custom UI and Gemini Enterprise use an **external IdP** (Okta, Microsoft Entra ID, Ping Identity) mapped to a Google Cloud **Workforce Identity Pool** (`workforcePools`).

> **Note:** Always use **Workforce Identity Federation (`workforcePools`)** for human users—not Workload Identity Federation (`workloadIdentityPools`). Gemini Enterprise Agent `User permissions` only accept Google Identity or `workforcePools` principals.

**Run with Python:**
```bash
python 01_user_auth_streamassist/pattern_b_workforce_sts.py \
  --idp-token "$EXTERNAL_IDP_TOKEN" \
  --query "What are our Grid Operations SLA policies?"
```

**Equivalent `curl` commands (Step 1: STS Exchange -> Step 2: `:streamAssist`):**
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

### Pattern 1C: Hybrid Login with Keyless Domain-Wide Delegation (`pattern_c_hybrid_keyless_dwd.py`)

Use when your custom UI signs employees in with an external IdP (such as Microsoft Entra ID or Okta), while your Gemini Enterprise engine is configured with **Google Identity** (Google Workspace / Cloud Identity). Uses the IAM Credentials API (`:signJwt`) to mint a short-lived Google OAuth user token with **zero downloaded service account JSON keys**.

**Run with Python:**
```bash
python 01_user_auth_streamassist/pattern_c_hybrid_keyless_dwd.py \
  --user-email "$DWD_TARGET_USER_EMAIL" \
  --query "What are our Grid Operations SLA policies?"
```

---

## Part 2: Authenticating Federated Connectors through a Custom UI

![Federated Connector 3LO Sequence](docs/diagrams/fig3-federated-connector-3lo-flow.png)

When your custom UI queries a Federated Connector, use `dataConnector:acquireAccessToken` to check if the user already has a refresh token stored in Gemini Enterprise's managed `Keystore`, and `dataConnector:acquireAndStoreRefreshToken` to store the authorization code returned after the user approves consent.

### Step 1: Check Keystore or Get `authUri`
```bash
python 02_federated_connector_oauth/connector_oauth_flow.py --step check
```

**Equivalent `curl` command:**
```bash
curl -s -X GET \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${CONNECTOR_LOCATION}/collections/${CONNECTOR_COLLECTION_ID}/dataConnector:acquireAccessToken?redirectUri=https://vertexaisearch.cloud.google.com/oauth-redirect"
```

### Step 2: Open Popup & Listen for `postMessage` in Your Custom UI
When `acquireAccessToken` returns an `authUri`, open it in a browser popup from your frontend and listen for the callback from `https://vertexaisearch.cloud.google.com/oauth-redirect`:

```javascript
// In your custom frontend UI:
const popup = window.open(authUri, "ConnectorOAuth", "width=520,height=680");

window.addEventListener("message", async (event) => {
  if (event.origin !== "https://vertexaisearch.cloud.google.com") return;
  const { code, state } = event.data;
  if (code) {
    // Send code & state to your backend to call :acquireAndStoreRefreshToken
    await fetch("/api/connector/store-token", {
      method: "POST",
      body: JSON.stringify({ code, state }),
    });
  }
});
```

### Step 3: Exchange Code & Store Refresh Token in Gemini Enterprise `Keystore`
```bash
python 02_federated_connector_oauth/connector_oauth_flow.py \
  --step store \
  --code "<OAUTH_CODE_FROM_POPUP>" \
  --state "<STATE_FROM_POPUP>"
```

---

## Part 3: Authenticating Custom ADK Agents with Individual Tool OAuth

![Custom ADK Agent On-Demand 3LO Sequence](docs/diagrams/fig4-adk-subagent-on-demand-3lo.png)

Attaching `authorizationConfig` at the top-level Gemini Enterprise agent registration requires Gemini Enterprise's built-in web UI to render the OAuth popup, causing `:streamAssist` calls from a custom UI to fail. Instead, register the ADK agent **without** top-level `authorizationConfig` and handle OAuth inside the individual ADK tool (`03_adk_agent_oauth/sample_adk_subagent_oauth.py`).

### Test 1: Query an Internal Policy Tool on the Registered Agent (No 3LO Required)
```bash
python 03_adk_agent_oauth/query_registered_agent.py \
  --auth-mode dwd \
  --query "What are our Grid Operations SLA policies?"
```

### Test 2: Query a Protected 3P Tool on the Registered Agent (Triggers On-Demand 3LO Prompt)
```bash
python 03_adk_agent_oauth/query_registered_agent.py \
  --auth-mode dwd \
  --query "List my open ServiceNow incidents"
```

### Equivalent `curl` Command Targeting a Registered Agent via `:streamAssist`
```bash
curl -s -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${GE_GOOGLE_ID_LOCATION}/collections/default_collection/engines/${GE_GOOGLE_ID_ENGINE_ID}/assistants/default_assistant:streamAssist" \
  -d "{
    \"query\": {\"text\": \"List my open ServiceNow incidents\"},
    \"agentsSpec\": {
      \"agentSpecs\": [{\"agentId\": \"${GE_GOOGLE_ID_AGENT_ID}\"}]
    }
  }"
```
