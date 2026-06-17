# Sorteberg MCP

A custom Model Context Protocol (MCP) server that turns your Gmail mailing list labels (e.g. "Merak Group", "Citroen SM") into a high-quality, searchable knowledge base.

Grok (or other MCP clients) can use the tools to search expert discussions and generate accurate, attributed step-by-step guides and how-tos.

## Key Features

- **Powerful mailing list search** with filters for author, date range, attachments, specific labels.
- **Full thread retrieval** — mailing list advice is often spread across multiple messages.
- **Attachment support** — list and retrieve files (with PDF text extraction).
- **Link following** — extract and fetch content from URLs mentioned in emails.
- **Author attribution** — every result includes clear expert name and email.
- **High-level guidance tool** — `get_expert_guidance` is optimized for generating complete how-to writeups.
- Secure owner Gmail OAuth (tokens stored in Firestore, never exposed to the AI client).
- Designed for Google Cloud Run + easy connection to the Grok web client.

## Use Case

Your mailing lists contain thousands of emails with goldmine-level technical knowledge from real experts. Instead of manually digging through archives, connect this MCP to Grok and ask things like:

- "Create a complete step-by-step guide for overhauling the Merak engine, with tips and warnings from the experts on the list. Attribute every piece of advice."
- "Using the Merak Group archive, build a troubleshooting guide for headlight hydraulics including photos and common mistakes mentioned by owners."

Grok will use the tools (`search_mailing_list`, `get_thread`, `get_attachment`, `fetch_link`, `get_expert_guidance`, etc.) to gather the best information and produce a well-sourced document.

## Available Tools

- `list_labels` — See which labels are connected.
- `search_mailing_list` — Advanced search (query, label, author, date range, has_attachments).
- `get_message` — Full details of one email. Supports `include_full_body=True` for long untruncated posts (essential for detailed specs).
- `get_thread` — Entire conversation thread for context. Supports `include_full_bodies=True`.
- `list_attachments` / `get_attachment` — Handle files attached to messages (PDF text extraction + image base64 for vision).
- `get_thread_attachments(thread_id)` — **New**: Collects *all* photos, diagrams, and PDFs from an entire thread with author/message context. Ideal for richly illustrated guides.
- `extract_links` / `fetch_link` — Pull and retrieve content from URLs. `fetch_link` now extracts text from PDF links (factory manuals, torque charts).
- `search_by_author` — Find contributions from specific experts.
- `get_expert_guidance(topic, label, max_threads)` — **Recommended starting point** for generating full how-tos. Smart search + thread enrichment with attribution (now pulls fuller bodies).
- `list_labels` — See which Gmail labels are connected (now "Merak Group", "Citroen SM").
- `search_mailing_list`, `get_message`, `get_thread`, `get_expert_guidance` — Core email archive tools (with full-body and attribution support).
- `list_attachments` / `get_attachment`, `get_thread_attachments` — Email attachments (PDF text + images).
- `list_input_manuals(model=None)` — List the Drive input folder (Car manuals). Subfolders are sorted by car model (Barchetta, Khamsin, etc.). Pass `model="Barchetta"` to list inside a specific model's folder.
- `save_to_guides(title, content, as_pdf=True)` — Recommended convenience wrapper to publish the howto PDF/.md to the output folder. Filenames are timestamped (e.g. Title_2026-06-17_12-34-56.pdf) to prevent duplicates.
- `list_drive_files` / `get_drive_file` / `save_howto_to_drive` — Lower-level versions (still available).
- `extract_links` / `fetch_link`, `search_by_author` — Supporting tools.
- `search_gmail` — Legacy wrapper.

## Setup & Deployment

### 1. Gmail Owner Connection (one-time)

The server needs your permission to read the labels.

Visit the deployed service and use the `/oauth/google/start` flow (protected by your agent token or IAM).

Tokens are stored securely in Firestore with automatic refresh. The AI client never sees your Gmail credentials.

### 2. Connecting to Grok (Web Client)

In the Grok web interface:

1. Add a **Custom Connector** / MCP server.
2. **MCP Server URL**: `https://sorteberg-mcp-62lr3ybf4a-ew.a.run.app/mcp`
3. Use the OAuth flow (the server provides minimal `/oauth/authorize` and `/oauth/token` endpoints using "none" PKCE for convenience).
4. After connection, Grok will have access to `search_mailing_list`, `get_expert_guidance`, etc.

You can also connect using the raw bearer token if preferred (see `env.yaml` or deployment for the value).

### 3. Running Locally (development)

```bash
# Install dependencies (uv or pip)
uv sync   # or pip install -r requirements.txt

# Set required environment variables (see env.yaml for examples)
export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...
export REDIRECT_URI=...
export AGENT_BEARER_TOKEN=...

python server.py
# or uvicorn server:app --reload
```

The server will be available at http://localhost:8080.

## Architecture

- **Backend**: Python + FastAPI + FastMCP
- **Gmail access**: google-api-python-client with OAuth2 + refresh tokens stored in Firestore
- **Authentication layers**:
  - Cloud Run IAM (owner access)
  - App-level bearer token (for AI agents / Grok)
- **MCP Transport**: Official streamable-http at `/mcp` (primary for modern clients) + legacy JSON-RPC support
- **Deployment**: Google Cloud Run (source deploy with Dockerfile)
- **Persistence**: Firestore for owner tokens (survives restarts)

See `ARCHITECTURE.md` for a deeper dive.

## Documentation

- `USAGE.md` — Detailed examples of prompts that work well with Grok
- `ARCHITECTURE.md` — Internal design and how the tools are implemented
- `DEPLOYMENT.md` — How to deploy / update the Cloud Run service
- `PROMPTS.md` — Curated prompts for generating high-quality how-tos from the mailing list

## Security Notes

- Your Gmail refresh tokens never leave the server.
- The AI client only ever sees the results of the tools you allow.
- The service can be locked down with `--no-allow-unauthenticated` + IAM bindings.
- Owner re-auth is always available via the web flow.

## Contributing / Next Steps

This project was built iteratively with Grok to solve a very specific need: making expert knowledge from old mailing lists usable again.

Common next improvements people ask for:
- Vector embeddings + semantic search over the archive (Vertex AI Vector Search layer in progress - see ARCHITECTURE.md)
- Automatic summarization / knowledge base building
- Image description for photos attached to emails
- Exporting generated how-tos back as nice PDFs or GitHub wiki pages

If you have ideas, open an issue or just tell Grok to implement them.

## License

MIT (or whatever you prefer — this is your personal tool).

---

Built with heavy assistance from Grok. The goal was to create something genuinely useful for preserving and using hard-won mechanical knowledge from enthusiast communities.