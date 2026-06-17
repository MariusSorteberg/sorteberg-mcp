# Deployment Guide – Sorteberg MCP

This document explains how to deploy, update, and maintain the Sorteberg MCP on Google Cloud Run.

## Prerequisites

- Google Cloud project with billing enabled
- `gcloud` CLI authenticated (`gcloud auth login`)
- The project has the following APIs enabled (or they will be enabled on first deploy):
  - Cloud Run
  - Cloud Build
  - Artifact Registry
  - Firestore (in Native mode)
- A Gmail OAuth 2.0 Client ID + Secret (Web application type) with the redirect URI you will use

## One-Time Setup

### 1. Firestore
The server stores owner refresh tokens in Firestore.

Create a Firestore database in **Native mode** in the same region you plan to deploy (e.g. `europe-west1`).

The collection/document path used is:
- Collection: `mcp_config`
- Document: `gmail_owner`

No indexes are required for basic operation.

### 2. IAM for the Service Account
When you deploy, Cloud Run uses the default Compute Engine service account:

`PROJECT_NUMBER-compute@developer.gserviceaccount.com`

Grant it the following roles (at least):

- `roles/datastore.user` (or `roles/datastore.owner` during development)
- `roles/run.invoker` (if you want to call it from other services)

You can do this via the console or:

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/datastore.user"
```

### 3. Gmail OAuth Credentials
Create (or reuse) an OAuth 2.0 Client ID in the Google Cloud Console:

- Application type: **Web application**
- Authorized redirect URIs: add both the Cloud Run URL(s) you will use + `/oauth/google/callback`

Example:
- `https://sorteberg-mcp-62lr3ybf4a-ew.a.run.app/oauth/google/callback`
- `https://sorteberg-mcp-328104254531.europe-west1.run.app/oauth/google/callback`

Put the Client ID and Secret into your environment (see below).

## Vertex AI Vector Search Setup (First Phase)

This is the **minimal setup** to enable the additive semantic/hybrid search layer using Vertex AI.

You only need this if you want to use `semantic_search`, `hybrid_search`, and the vector-first path in `get_expert_guidance`.

### Exact steps you must perform in Google Console / gcloud

1. **Enable the Vertex AI API** (in the project `vibrant-ring-496211-v5` or your project):
   ```bash
   gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT_ID
   ```
   Or go to Google Cloud Console → APIs & Services → Enable APIs → search "Vertex AI API".

2. **Create a Vector Search Index** (Matching Engine):
   - Go to Vertex AI → Vector Search → Create Index (or use gcloud).
   - Recommended settings for first phase:
     - Display name: `sorteberg-mcp-emails-drive`
     - Dimensions: `768` (text-embedding-004 produces 768-dim vectors)
     - Distance: `Cosine` or `Dot Product`
     - Index update method: **Streaming** (important for incremental upserts)
     - Sharding / other defaults are fine for small start.
   - After creation, copy the full resource name, e.g.:
     `projects/328104254531/locations/us-central1/indexes/1234567890123456789`
     Put it in `VECTOR_INDEX_NAME`.

   gcloud example (approximate):
   ```bash
   gcloud ai indexes create \
     --display-name=sorteberg-mcp \
     --dimensions=768 \
     --distance-measure-type=COSINE_DISTANCE \
     --project=YOUR_PROJECT \
     --region=us-central1
   ```

3. **(Strongly recommended) Create an Index Endpoint and deploy the Index**:
   - In Vertex AI → Vector Search → Index Endpoints → Create Index Endpoint.
   - Then "Deploy Index" to it (choose your newly created index).
   - Copy the Index Endpoint resource name.
   - Set `VECTOR_INDEX_ENDPOINT` to it. Queries are much more reliable through a deployed endpoint.

4. **Grant IAM permissions to the Cloud Run service account**:
   The service account is usually:
   `PROJECT_NUMBER-compute@developer.gserviceaccount.com`

   Give it:
   ```bash
   gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
     --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
     --role="roles/aiplatform.user"
   ```

   For stricter: `roles/aiplatform.viewer` + custom for matching engine if needed, but `aiplatform.user` is usually sufficient for embedding + index read/write in first phase.

5. **Update your env-vars file** (`/tmp/mcp-env-vars.yaml` or equivalent) with the correct `VECTOR_INDEX_NAME` (and optionally `VECTOR_INDEX_ENDPOINT`).

6. Redeploy:
   ```bash
   gcloud run deploy sorteberg-mcp --source=. --region=... --env-vars-file=/tmp/mcp-env-vars.yaml ...
   ```

### Testing the first phase (after deploy + index ready)

- Use the protected ingest endpoint (you need AGENT_BEARER_TOKEN):
  ```bash
  curl -X POST "https://YOUR-SERVICE/ingest?days_back=7" \
    -H "Authorization: Bearer YOUR_AGENT_BEARER_TOKEN"
  ```
- Or call the tool directly via MCP: `trigger_ingest(days_back=7)`
- Then try `semantic_search("cylinder head torque Merak")` or `hybrid_search(...)`.
- Then run a full `get_expert_guidance("suspension geometry Merak")` — it will try vector first.

**Important notes for first phase**:
- Start with small `days_back` (7-14) so you don't index thousands of messages at once.
- The index must be fully created + deployed before queries succeed (can take minutes).
- If vector calls return empty, the fallback keyword path in `get_expert_guidance` still works.
- Metadata filtering via restricts is prepared but first-phase queries do additional client-side handling.
- Full content, tables, and attribution always come from the original Gmail/Drive tools (vector is only for recall).

## Deployment

The project is set up for **Cloud Run source deploys** (no separate image building step required).

### Recommended Command

```bash
gcloud run deploy sorteberg-mcp \
  --source=. \
  --region=europe-west1 \
  --allow-unauthenticated \          # or remove for strict IAM
  --env-vars-file=/tmp/mcp-env-vars.yaml \
  --memory=512Mi \
  --cpu=1 \
  --max-instances=3 \
  --timeout=300s
```

### Environment Variables

Use an `env-vars-file` (YAML) for most values. Example `/tmp/mcp-env-vars.yaml`:

```yaml
ALLOWED_LABELS: "Merak Group,Citroen SM"
AGENT_BEARER_TOKEN: "your-long-random-bearer-token-here"
GOOGLE_CLIENT_ID: "3281...apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET: "GOCSPX-..."
REDIRECT_URI: "https://sorteberg-mcp-62lr3ybf4a-ew.a.run.app/oauth/google/callback"

# Google Drive (optional but recommended for the expert howto workflow)
DRIVE_INPUT_FOLDER_ID: "your-input-folder-id-for-pdfs-and-sources"
DRIVE_OUTPUT_FOLDER_ID: "your-output-folder-id-for-generated-pdfs"

# Vertex AI Vector Search (first phase - start small with manual trigger_ingest)
# See the "Vertex AI Vector Search Setup (First Phase)" section below + ARCHITECTURE.md
VERTEX_PROJECT: "vibrant-ring-496211-v5"
VERTEX_LOCATION: "us-central1"
VECTOR_INDEX_NAME: "projects/vibrant-ring-496211-v5/locations/us-central1/indexes/REPLACE_ME"
# VECTOR_INDEX_ENDPOINT: "..."   # set after you deploy the index to an endpoint (recommended)
```

**Security note**: The Gmail client secret is sensitive. For production you should move it to Secret Manager and use `--set-secrets`.

### Making It Public vs Private

- **Public (easy for Grok web + bearer token)**: Use `--allow-unauthenticated` + the `allUsers` invoker binding (see below).
- **Private (maximum security)**: Omit `--allow-unauthenticated` and only grant `run.invoker` to specific users/service accounts (including yourself for owner management).

To allow anyone with the bearer token:

```bash
gcloud run services add-iam-policy-binding sorteberg-mcp \
  --region=europe-west1 \
  --member="allUsers" \
  --role="roles/run.invoker"
```

## Updating the Service

Just run the same `gcloud run deploy ... --source=.` command again. It will create a new revision and (by default) shift 100% traffic to it.

You can also do canary deploys with `--no-traffic` + traffic splitting if desired.

## Environment & Secrets Management (Recommended Hardening)

Current setup puts secrets in the env-vars-file. Better long-term:

```bash
# Store the secret
echo -n "GOCSPX-..." | gcloud secrets create gmail-client-secret --data-file=-

# Deploy referencing the secret
gcloud run deploy sorteberg-mcp \
  --source=. \
  --region=europe-west1 \
  --allow-unauthenticated \
  --env-vars-file=/tmp/mcp-env-vars.yaml \
  --set-secrets="GOOGLE_CLIENT_SECRET=gmail-client-secret:latest"
```

Then remove `GOOGLE_CLIENT_SECRET` from the env-vars-file.

Do the same for the `AGENT_BEARER_TOKEN` if you consider it highly sensitive.

## Monitoring & Logs

```bash
# Tail logs
gcloud run services logs tail sorteberg-mcp --region=europe-west1

# View recent logs
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=sorteberg-mcp" --limit=50 --format="table(timestamp, severity, textPayload)"
```

Useful metrics to watch:
- Request latency on the `/mcp` path (Grok calls can be chatty)
- Gmail API quota usage (the server is quite efficient but heavy searches can burn quota)

## Re-authenticating the Gmail Owner

If the refresh token is revoked or expires in a weird way:

1. Visit `https://your-service-url/oauth/google/start` (while authenticated as the owner via IAM or while the service is temporarily open).
2. Complete the consent flow again.
3. The new refresh token will be stored and the old one overwritten.

## Rolling Back

Cloud Run keeps previous revisions. You can roll back instantly in the console or with:

```bash
gcloud run services update-traffic sorteberg-mcp \
  --region=europe-west1 \
  --to-revisions=PREVIOUS_REVISION_NAME=100
```

## Local Development vs Production

- Use the same `env-vars-file` locally (or a `.env` file + `python-dotenv`).
- For local testing you can run `python server.py` or `uvicorn server:app --reload`.
- The owner OAuth flow works locally as long as your local `REDIRECT_URI` is registered in the Google OAuth client (add `http://localhost:8080/oauth/google/callback` during development).

## Common Issues & Fixes

**403 on /health or /mcp after deploying with --no-allow-unauthenticated**
- You (or Grok) don't have `run.invoker`. Add the binding for `user:your-email` or `allUsers`.

**"redirect_uri_mismatch" during owner Gmail auth**
- The `REDIRECT_URI` env var does not exactly match what is registered in the Google Cloud Console for the OAuth client.

**Tools not appearing in Grok after connecting**
- Make sure the MCP server URL in the Grok connector ends with `/mcp` (the streamable-http mount point).
- Re-add the connector if the OAuth token expired.
- Check that the service is returning 200 on health and that `tools/list` works with the bearer.

**Gmail searches returning very few or no results**
- The owner may have revoked access, or the labels have been renamed/deleted.
- Re-run the owner OAuth flow.
- Double-check `ALLOWED_LABELS` in the environment.

## Backup & Recovery

- Firestore data can be exported via the console or `gcloud firestore export`.
- The only irreplaceable data is the Gmail refresh token. Keep a backup of the Firestore export if you do major cleanups.

## Cost Considerations

At low usage this is extremely cheap (Cloud Run + Firestore + Gmail API).
Heavy usage (many large thread fetches) will mostly cost in:
- Cloud Run CPU/memory
- Outbound data if you return huge bodies frequently
- Gmail API quota (you get 1B units/day on a standard project — this server is very light)

## Updating After Code Changes

1. Make changes locally and test (at minimum run `python -c "import server"` and test the new tools locally with your bearer).
2. Commit.
3. Run the deploy command above.
4. Verify with `tools/list` and a couple of real tool calls (e.g. `list_labels` and a small `search_mailing_list`).

That's it — Cloud Run handles the rest.

## Further Hardening Ideas

- Move all secrets to Secret Manager.
- Use a dedicated service account instead of the default Compute one.
- Add Cloud Armor or IAP in front if you want extra layers.
- Set up a custom domain + managed SSL.
- Add structured logging + export to BigQuery for usage analytics.

Contact the owner (marius@sorteberg.no) or just tell Grok to implement any of the above.