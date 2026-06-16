# Sorteberg MCP Architecture

## High-Level Overview

The Sorteberg MCP is a remote Model Context Protocol server that gives AI agents (primarily Grok) controlled, read-only access to specific Gmail labels containing valuable technical mailing list archives.

Instead of the AI having direct Gmail access, it gets a curated set of tools that can search, retrieve threads, attachments, and external links — all while the owner’s credentials stay safely on the server.

## Core Components

### 1. Gmail Integration Layer
- Uses `google-api-python-client` + OAuth2 with refresh tokens.
- Owner performs one-time OAuth flow via `/oauth/google/start`.
- Refresh tokens + access tokens are stored in **Google Cloud Firestore** (collection `mcp_config`, document `gmail_owner`).
- Automatic token refresh happens transparently in `get_gmail_service()`.
- All searches are hard-restricted to a whitelist of labels (`ALLOWED_LABELS`).

### 2. MCP Tool Layer (FastMCP)
Tools are defined with the `@mcp.tool()` decorator. The server exposes them through two transports:

- **Primary (recommended)**: Official `streamable-http` transport mounted at `/mcp`.
- **Legacy**: Custom JSON-RPC 2.0 handler at `/mcp-legacy` (and root) for clients that speak the older protocol.

Key tools (as of latest version):
- `search_mailing_list` — rich search with author, date, attachment filters.
- `get_message` / `get_thread` — full content + context.
- `get_attachment` — with PDF text extraction.
- `fetch_link` — follow URLs mentioned in messages.
- `get_expert_guidance` — high-level tool that performs smart multi-query search + thread fetching, optimized for generating how-to documents.
- Plus supporting tools for authors, links, etc.

All results are designed to be rich in attribution (author name/email, date, message/thread IDs, source label).

### 3. Authentication Layers (Defense in Depth)
1. **Cloud Run IAM** (`--no-allow-unauthenticated` + `roles/run.invoker`)
   - Controls who can even reach the service.
   - Owner (`marius@sorteberg.no`) has explicit invoker rights.
   - Can be opened to `allUsers` when you want easy bearer-token access for Grok.

2. **Application-level Bearer Token**
   - `AGENT_BEARER_TOKEN` environment variable.
   - Enforced in middleware for all `/mcp*` paths.
   - The custom OAuth flow (`/oauth/authorize` + `/oauth/token`) using "none" (PKCE) hands this token to Grok clients.

3. **Owner Gmail OAuth**
   - Completely separate flow. Only the server ever sees the Gmail tokens.

### 4. Web Layer (FastAPI)
- Health check, root info page, debug endpoints.
- Owner OAuth flow for Gmail.
- Minimal OAuth2 endpoints to satisfy Grok’s “Custom Connector” OAuth (PKCE none) wizard so it can obtain the bearer token.
- Middleware that applies bearer checks to MCP paths.

### 5. Deployment
- Dockerfile based on Python slim image.
- Deployed via `gcloud run deploy --source=.` (Cloud Run source deployer builds the image).
- Environment managed via `env-vars-file` (contains non-sensitive values + the agent bearer).
- Secrets (Gmail client ID/secret) are currently in the env file — in a real production setup they should move to Secret Manager.

## Data Flow for a Typical "Create How-To" Request

1. User (in Grok web) asks something like: "Using the Merak Group list, create a complete guide for overhauling the engine with tips from the experts."
2. Grok decides to call `get_expert_guidance` (or a combination of `search_mailing_list` + `get_thread`).
3. Request goes to the MCP server (authenticated with the bearer token obtained during connector setup).
4. Server calls Gmail API (using the stored owner refresh token).
5. Results are returned with full attribution.
6. Grok synthesizes a high-quality, sourced document and presents it to the user.

The AI never sees raw Gmail credentials or has unrestricted access.

## Design Decisions & Trade-offs

- **Why Firestore instead of Secret Manager for tokens?**  
  Simpler for a single owner. Easy to implement refresh logic. Can be upgraded later.

- **Why both streamable-http and legacy JSON-RPC?**  
  Maximum compatibility. Grok web prefers the modern transport; older clients or direct testing can use the JSON-RPC path.

- **Why a high-level `get_expert_guidance` tool?**  
  Searching a mailing list for "how to" content is a common pattern. Having a tool that does smart query expansion + thread fetching saves many round-trips and produces better context for the LLM.

- **Why allow-unauthenticated + bearer?**  
  Makes it trivial for Grok (and other external agents) to use the server. The bearer is still a secret. For maximum security you can lock it back to pure IAM and have Grok use a properly authorized Google identity.

## Current Limitations & Future Ideas

- Full email bodies and threads can get large — we truncate aggressively in some places.
- No semantic/vector search yet (keyword + Gmail search only).
- Image attachments are returned as metadata only (vision can be done on the client side or via a future tool).
- No write access to Gmail (by design).
- No persistent indexing of the archive (every search hits Gmail API).

Possible future enhancements:
- Background indexing + vector embeddings of threads.
- Dedicated "knowledge base" tools.
- Image description tool using a vision model.
- Export generated guides as Markdown/PDF and attach them back to Gmail or a wiki.
- Multi-user support (multiple owners, per-user labels).

## Security Considerations

- Least privilege on Gmail (readonly scope only).
- Tokens never leave the Cloud Run service.
- App-level auth on top of platform auth.
- Owner can revoke access at any time via Google Account settings.

This architecture gives you the convenience of a powerful personal knowledge base while keeping strong boundaries between your private email and the AI.