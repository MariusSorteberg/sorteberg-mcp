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
- `get_message` / `get_thread` — full content + context. Support `include_full_body` / `include_full_bodies=True` to retrieve long untruncated posts containing detailed specifications and procedures.
- `get_attachment` — with PDF text extraction (via pypdf). Images return base64 + metadata for vision use.
- `get_thread_attachments(thread_id)` — new dedicated tool that harvests *all* attachments (photos, diagrams, PDF scans) across an entire thread with full author/message attribution. Critical for producing illustrated professional documentation.
- `fetch_link` — follow URLs mentioned in messages. Now also extracts readable text from linked PDFs (factory manuals, torque charts, etc.).
- `get_expert_guidance` — high-level tool that performs smart multi-query search + thread fetching (now with fuller bodies), optimized for generating how-to documents.
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

### 4. Flow Diagrams

#### 4.1 Initial Gmail Owner Authentication Flow (Token Storage in Firestore)

This flow is performed once (or when the owner needs to re-authorize). The goal is for the server to obtain a long-lived `refresh_token` from Google and persist it securely.

```mermaid
sequenceDiagram
    participant Owner as Owner (Browser, logged in as marius@sorteberg.no)
    participant MCP as MCP Server (Cloud Run)
    participant Google as accounts.google.com (OAuth)
    participant FS as Firestore (mcp_config/gmail_owner)

    Owner->>MCP: GET /oauth/google/start<br/>(Authorization: Bearer <AGENT_BEARER_TOKEN> or via Cloud Run IAM)
    Note over MCP: require_agent() passes (bearer or IAM)
    MCP->>Google: Redirect to /o/oauth2/auth<br/>(client_id, scope=gmail.readonly, access_type=offline, prompt=consent, redirect_uri)
    Google-->>Owner: Google Sign-in + Consent screen
    Owner->>Google: Approve access
    Google->>MCP: GET /oauth/google/callback?code=...&state=...
    MCP->>Google: POST /token (code, client_id, client_secret, grant_type=authorization_code)
    Google-->>MCP: {access_token, refresh_token, id_token, ...}
    MCP->>MCP: Parse email from id_token (or profile)
    MCP->>FS: set({email, access_token, refresh_token, ...}, merge=true)
    FS-->>MCP: Stored
    MCP-->>Owner: HTML Success page<br/>"Gmail connected successfully"
```

Key points:
- The `refresh_token` is only returned on the first consent (or when `prompt=consent` + `access_type=offline`).
- Tokens are stored server-side only.
- The `AGENT_BEARER_TOKEN` (or Cloud Run IAM) protects this endpoint so random visitors can't trigger owner re-auth.

#### 4.2 Regular Tool Usage Flow (in Terminal / Grok)

This is the everyday flow when you (or Grok in the terminal/web) use the MCP tools.

```mermaid
sequenceDiagram
    participant Client as Grok / Terminal (MCP Client)
    participant MCP as MCP Server (Cloud Run)
    participant FS as Firestore
    participant Gmail as Gmail API (gmail.googleapis.com)

    Client->>MCP: POST /mcp<br/>{ "method": "tools/call", "params": { "name": "search_mailing_list" or "get_expert_guidance", ... } }<br/>Authorization: Bearer <AGENT_BEARER_TOKEN>
    Note over MCP: Middleware mcp_auth_middleware runs require_agent()
    MCP->>MCP: if auth == bearer → proceed (or valid Google ID token from IAM)
    MCP->>FS: get_owner_record() → load {refresh_token, access_token, ...}
    FS-->>MCP: Owner record
    alt access_token expired or missing
        MCP->>Gmail: POST /token (refresh_token, grant_type=refresh_token)
        Gmail-->>MCP: New access_token
        MCP->>FS: Update access_token + updated_at
    end
    MCP->>Gmail: API call (messages.list / threads.get / attachments.get)<br/>with Authorization: Bearer <access_token> + label filter
    Gmail-->>MCP: Message data (headers, body, attachments, threadId, ...)
    MCP->>MCP: Enrich result (parse author name, extract links, attachment metadata, full thread if requested)
    MCP-->>Client: JSON-RPC result (structured data with author attribution, message IDs, etc.)
    Client->>Client: Grok uses the data to generate how-to / writeup with proper sourcing
```

Key points:
- The client (Grok) never sees Gmail tokens — only the results of tool calls.
- Token refresh is fully automatic and transparent.
- All results include author name/email and source identifiers so Grok can attribute advice correctly.
- The same bearer token (obtained via the PKCE "none" flow during Custom Connector setup) is used for every call.

## Using the MCP from the Grok Web Client

The Sorteberg MCP is designed to integrate directly with the Grok web client (at grok.x.ai or via the X platform) using the built-in Custom Connectors feature. This allows Grok to discover and call your mailing list search tools natively during conversations, without any manual copying of emails or tokens.

### Adding the Custom Connector

1. In the Grok web interface, go to the settings or connectors section and select the option to add a new Custom Connector (sometimes labeled as "MCP server" or "Custom tool provider").

2. Enter a descriptive name for the connector, such as "Sorteberg Merak Gmail MCP".

3. Set the MCP server URL to the primary endpoint that serves the official streamable-http transport:
   ```
   https://<your-deployed-service-domain>/mcp
   ```
   (This is the path where the FastMCP tools are exposed for modern clients like Grok.)

4. Since tool access requires authentication, the connector will prompt for OAuth credentials. Configure it using the "none (PKCE only, recommended)" token auth method:
   - Provide the Authorization Endpoint and Token Endpoint from your MCP server (these endpoints are hosted on the same service and support public client flows).
   - Client Secret can be left empty.
   - Scopes can typically be left blank or set to a minimal value like "mcp" if required by the form.

5. Save the connector and complete the OAuth authorization flow when prompted. Grok will handle obtaining the necessary access credentials through this flow.

Once added, Grok will automatically probe the connector (using standard MCP discovery methods like `tools/list`) and make the available tools visible in your conversations.

### Available Tools and How Grok Uses Them

After a successful connection, the following tools from your MCP become available for Grok to use automatically or on request:

- `search_mailing_list` — for querying emails in your allowed labels with flexible filters.
- `get_expert_guidance` — a specialized tool that retrieves enriched, expert-sourced discussions suitable for building how-tos (pulls fuller bodies for specs).
- Supporting tools such as `get_message` (with full-body option), `get_thread` (with full-bodies option), `list_attachments`, `get_attachment`, `get_thread_attachments` (new: all visuals from a thread), `extract_links`, `fetch_link` (now PDF-aware), `search_by_author`, and `list_labels`.

Grok can call these tools in the background when you reference your connected data. For example, it might use `search_mailing_list` with a query focused on your "Merak Group" label, then follow up with `get_thread` or `get_expert_guidance` to gather full context and author-attributed advice.

### Example Usage in Grok Conversations

You can prompt Grok directly in the web client to leverage the MCP:

- "Using my Sorteberg MCP (Merak Group label), search for expert discussions on overhauling the engine and create a detailed, step-by-step how-to guide with tips, warnings, and attributions to specific posts or authors from the list."
- "From the connected mailing list tools, pull the best advice on gearbox issues and format it as a troubleshooting guide with sources."
- "Help me generate a comprehensive writeup on headlight hydraulics by querying the MCP tools for relevant threads and attachments."

Grok will handle tool selection, parameter construction (including label restrictions), and synthesis of the results into a coherent response. You don't need to know the exact tool names or query syntax — natural language is usually sufficient, especially when you mention "my MCP", "Sorteberg connector", or the specific label.

### Best Practices and Notes

- Be specific in your prompts about the label (e.g., "Merak Group") and the type of output you want (structured steps, expert quotes, warnings, etc.). This helps Grok choose the right tools and parameters.
- The MCP connection gives Grok read-only access only to the results of the tools — your actual email content and Gmail credentials remain on the server side and are never exposed.
- If tools don't appear immediately after adding the connector, try starting a new conversation or refreshing the page. You can also ask Grok explicitly: "List the tools available from my Sorteberg MCP connector."
- The connector uses the modern MCP transport, so Grok gets full support for the rich tool schemas (including descriptions that help it decide when and how to call each one).
- For ongoing use, the connection persists across sessions. You can manage or remove the connector from Grok's settings at any time.
- If you need to re-authorize the connector (e.g., after token expiration), simply re-run the OAuth step in the connector configuration.

This setup turns your unstructured email archives into a live, queryable knowledge source that Grok can consult directly in the web interface for tasks like research, troubleshooting, or creating detailed technical documentation based on real expert input from the mailing lists.

### 5. Web Layer (FastAPI)
- Health check, root info page, debug endpoints.
- Owner OAuth flow for Gmail.
- Minimal OAuth2 endpoints to satisfy Grok’s “Custom Connector” OAuth (PKCE none) wizard so it can obtain the bearer token.
- Middleware that applies bearer checks to MCP paths.

### 6. Deployment
- Dockerfile based on Python slim image.
- Deployed via `gcloud run deploy --source=.` (Cloud Run source deployer builds the image).
- Environment managed via `env-vars-file` (contains non-sensitive values + the agent bearer).
- Secrets (Gmail client ID/secret) are currently in the env file — in a real production setup they should move to Secret Manager.

## Data Flow for a Typical "Create How-To" Request

1. User (in Grok web) asks something like: "Using the Merak Group list, create a complete guide for overhauling the engine with tips from the experts."
2. Grok decides to call `get_expert_guidance` (or a combination of `search_mailing_list` + `get_thread`).
3. Request goes to the MCP server (authenticated with the bearer token obtained during connector setup).
4. Server calls Gmail API (using the stored owner refresh token).
5. Results are returned with full attribution. For visual-rich or spec-heavy work, Grok will also call `get_thread_attachments`, `get_attachment` (for images/PDFs), and use full-body options.
6. Grok synthesizes a high-quality, sourced document (with tables for every torque/clearance and guidance for incorporating real diagrams/photos) and presents it to the user.

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

## 7. Vector Search Layer with Vertex AI (Starting Small)

To overcome the limitations of pure keyword search and lack of persistent indexing over large archives of mailing list emails and Drive documents (manuals, PDFs with specs/tables), we are adding a semantic search layer using **Google Cloud Vertex AI Vector Search**.

This starts small, as suggested:
- Manual or scheduled (Cloud Scheduler) indexing trigger.
- Basic chunking of email threads and Drive files (with attention to preserving table structures for torque/geometry specs).
- Embeddings generated via Vertex AI.
- One or more indexes (e.g., separate for emails vs documents, or combined with metadata filtering).
- New MCP tools for semantic retrieval, integrated into the expert skill for better context gathering before guide generation.
- Hybrid approach: vector results + existing keyword tools + full content fetch for attribution.

### Why Vertex AI Vector Search?
- Fully managed, high-scale nearest-neighbor search.
- Native integration with Vertex AI embedding models.
- Metadata filtering (e.g., by label="Merak Group", model="Merak", date range, author).
- Incremental updates (no full re-index every time).
- Fits the existing Google Cloud + org setup (Cloud Run, Firestore for metadata/sync state).
- Supports the goal of accurate, sourced technical documentation from unstructured expert data + official manuals.

### High-Level Architecture Schematic

```mermaid
flowchart TD
    subgraph Client
        Grok[Grok Client<br/>Terminal or Web via MCP Connector]
    end

    subgraph MCP["MCP Server (Cloud Run)"]
        direction TB
        Tools[FastMCP Tool Layer<br/>search_mailing_list, get_expert_guidance, ...]
        VectorTools[New Vector Tools<br/>semantic_search, hybrid_search]
        Auth[Auth: Bearer + Cloud Run IAM]
    end

    subgraph GoogleAPIs
        Gmail[Gmail API<br/>Labels: Merak Group, Citroen SM]
        Drive[Google Drive API<br/>Input Folder: Car Manuals by Model<br/>Output Folder: Generated Guides]
    end

    subgraph Persistence
        FS[Firestore<br/>Gmail/Drive tokens + Sync metadata]
    end

    subgraph VectorLayer["Vector Search Layer (Vertex AI)"]
        Embed[Vertex AI Embeddings]
        VS[Vertex AI Vector Search Index<br/>Chunks + Metadata]
    end

    subgraph Ingestion["Ingestion Pipeline (Cloud Run Job)"]
        direction LR
        Trigger[Manual Trigger Endpoint<br/>or Cloud Scheduler]
    end

    Grok -->|MCP calls e.g. get_expert_guidance| Tools
    Tools --> VectorTools
    Tools --> Gmail
    Tools --> Drive
    VectorTools --> VS
    Auth --> FS

    Trigger --> Ingestion
    Ingestion --> Gmail
    Ingestion --> Drive
    Ingestion --> Embed
    Embed --> VS
    Ingestion -->|metadata| FS

    VS -.->|enriched chunks| Tools
    Gmail -.->|full threads/attachments| Tools
    Drive -.->|full PDFs| Tools
```

Key data flows:
- **Ingestion**: Fetches from Gmail/Drive → chunks → embeds → upserts to VS with rich metadata (author, date, thread/file ID, label, car model, etc.).
- **Query**: Grok calls tool → MCP does vector search (optionally hybrid with keyword) → fetches full context via existing tools → returns attributed results to skill for guide synthesis → optional save to Drive output.

### 7.1 Ingestion Flow (Manual or Scheduled Trigger)

```mermaid
sequenceDiagram
    actor User as User / Scheduler
    participant Ing as Ingestion Job (Cloud Run)
    participant Gmail as Gmail API
    participant Drive as Drive API
    participant Embed as Vertex AI Embeddings
    participant VS as Vertex AI Vector Search
    participant FS as Firestore

    User->>Ing: Trigger /ingest (manual or scheduled)
    Ing->>Gmail: List + fetch new/updated messages in allowed labels
    Gmail-->>Ing: Threads, bodies, attachments metadata
    Ing->>Drive: List + fetch updated files in input folder (by model subfolders)
    Drive-->>Ing: File content (PDF text via pypdf or better)
    Ing->>Ing: Intelligent chunking (by message/thread or PDF section; preserve tables)
    Ing->>Embed: Batch generate embeddings
    Embed-->>Ing: Vectors + metadata
    Ing->>VS: Upsert chunks (with filters: label, model, author, date)
    VS-->>Ing: Success
    Ing->>FS: Update last_sync timestamp / state
    Ing-->>User: Status report (items indexed, errors)
```

Notes for small start:
- Manual trigger first (protected endpoint using bearer).
- Simple chunking (e.g., per email message or per PDF page).
- One index initially.
- Full re-index option for corrections.
- Later: Pub/Sub for Gmail incremental updates, Document AI for better PDF structure.

### 7.2 Typical Query Flow with Vector Search

```mermaid
sequenceDiagram
    participant Grok as Grok Client (via skill)
    participant MCP as MCP Server
    participant VS as Vertex AI Vector Search
    participant Gmail as Gmail/Drive APIs

    Grok->>MCP: tools/call get_expert_guidance<br/>(topic="suspension geometry front/rear", label="Merak Group")
    MCP->>VS: semantic_search(topic, top_k=8, filter={label: "Merak Group"})
    VS-->>MCP: Ranked chunks + metadata (thread_ids, scores, source)
    MCP->>Gmail: get_thread / get_drive_file for top metadata IDs
    Gmail-->>MCP: Full content, attachments, exact text (for specs/tables)
    MCP->>MCP: Dedup, enrich with author/date/attribution, combine with any keyword results
    MCP-->>Grok: Structured context (relevant excerpts + full sources)
    Grok->>Grok: Apply skill rules: tables for specs, step-by-step, pitfalls, diagrams guidance, separate private vs public sources
    Grok->>MCP: Optional save_to_guides(guide_md)
    MCP->>Drive: Write PDF to output folder
    Drive-->>MCP: Confirmation
    MCP-->>Grok: Success + link
```

This enhances `get_expert_guidance` (still the primary for the skill) while keeping all existing attribution and full-content tools.

### 7.3 New/Updated Tools (First Phase)

- `semantic_search(query, label=None, top_k=8)`: Semantic retrieval using Vertex AI. Returns chunk references (email_*/drive_* ids + scores). Use with get_thread/get_drive_file for full attributed content.
- `hybrid_search(query, label=None, top_k=8)`: Combines a few vector hits + keyword search_mailing_list results (deduped).
- Enhanced `get_expert_guidance`: Starts with vector semantic recall, then falls back/enriches with keyword + full thread fetch.
- `trigger_ingest(manual=True, days_back=7)` + protected `POST /ingest`: Manual indexing trigger. Chunks with bias toward keeping tables/paragraphs together.
- Ingestion also writes `vector_last_ingest` to Firestore owner doc.

The skill.md will be updated to instruct starting with semantic tools for relevant context before full synthesis.

### Integration with Existing Components

- Reuses Firestore for sync state + tokens.
- Reuses existing auth (bearer/IAM).
- Drive input subfolders (by car model) and email labels remain the source of truth.
- Output folder for published guides unchanged.
- No change to Gmail/Drive OAuth or read-only nature.

This is additive: keyword tools remain available for exact matches or when vector confidence is low.

## Current Limitations & Future Ideas (Updated)

- Full email bodies and threads can get large — truncation is now controllable per call via `include_full_bodies=True` / `include_full_body=True` (and get_expert_guidance pulls fuller content). Very large threads may still need selective follow-up.
- ~~No semantic/vector search yet (keyword + Gmail search only).~~ **First phase implemented** — Vertex AI Vector Search layer (manual/scheduled trigger, chunking, semantic_search + hybrid_search, wired into get_expert_guidance).
- Image attachments return full base64 + metadata (vision works well client-side; no server-side description yet).
- No write access to Gmail (by design).
- ~~No persistent indexing of the archive (every search hits Gmail API).~~ **First phase** — Vector index with metadata filters support (restricts) + last_sync state in Firestore.

Possible future enhancements:
- Full Pub/Sub incremental indexing for Gmail.
- Advanced chunking + Document AI for PDF structure (better table extraction for specs).
- Hybrid search as default in `get_expert_guidance`.
- Image description tool using a vision model.
- Export generated guides as Markdown/PDF and attach them back to Gmail or a wiki.
- Multi-user support (multiple owners, per-user labels).
- Monitoring / eval for retrieval quality on technical queries.

## Security Considerations

- Least privilege on Gmail (readonly scope only).
- Tokens never leave the Cloud Run service.
- App-level auth on top of platform auth.
- Owner can revoke access at any time via Google Account settings.

This architecture gives you the convenience of a powerful personal knowledge base while keeping strong boundaries between your private email and the AI.