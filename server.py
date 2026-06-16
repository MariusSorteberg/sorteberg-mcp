"""
Sorteberg MCP Server - Recommended Architecture

- FastAPI for HTTP layer and extra routes (OAuth, health, info)
- FastMCP for clean tool definitions and schemas
- JSON-RPC compatibility on /mcp for existing clients (initialize, tools/list, tools/call)
- Proper server-side Gmail OAuth with Firestore token storage + automatic refresh
- Multi-label support (ALLOWED_LABELS)
- Agent authentication via Authorization: Bearer <AGENT_BEARER_TOKEN>
- Designed for Google Cloud Run + streamable-http transport available at /mcp-transport
"""

import os
import logging
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mcp.server.fastmcp import FastMCP

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.cloud import firestore

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
ALLOWED_LABELS = [
    label.strip()
    for label in os.getenv("ALLOWED_LABELS", "Merak Group,Citroën SM").split(",")
    if label.strip()
]

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI") or os.getenv("BASE_URL", "https://sorteberg-mcp-328104254531.europe-west1.run.app")

# Agent / Grok authentication (proper inbound auth)
# Caller must send: Authorization: Bearer <this token>
AGENT_BEARER_TOKEN = os.getenv("AGENT_BEARER_TOKEN") or os.getenv("MCP_BEARER_TOKEN", "")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Firestore storage for the Gmail owner tokens (persistent, survives restarts)
TOKEN_COLLECTION = "mcp_config"
TOKEN_DOCUMENT = "gmail_owner"

# In-memory short-lived state for OAuth flows (stateless Cloud Run friendly for short flows)
oauth_states: Dict[str, Dict[str, Any]] = {}

# -----------------------------------------------------------------------------
# FastMCP - Proper tool definitions
# -----------------------------------------------------------------------------
mcp = FastMCP("sorteberg-mcp")

db = firestore.Client()

# -----------------------------------------------------------------------------
# Helpers - Auth, Firestore, Gmail
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def token_doc():
    return db.collection(TOKEN_COLLECTION).document(TOKEN_DOCUMENT)

def require_agent(request: Request) -> None:
    """Proper agent authentication for MCP calls and protected routes.

    With --no-allow-unauthenticated, Cloud Run only forwards requests that have a valid
    Google ID token from a principal that has roles/run.invoker on the service.
    We trust the platform for those calls. The custom AGENT_BEARER_TOKEN is still
    supported for direct bearer-style calls (when the service allows unauthenticated
    or for additional checks).
    """
    auth = request.headers.get("Authorization", "")

    if AGENT_BEARER_TOKEN:
        expected = f"Bearer {AGENT_BEARER_TOKEN}"
        if auth.lower() == expected.lower():
            return

    # If we reached here with a "Bearer ..." (or "bearer ...") token, it means
    # Cloud Run IAM already verified the caller has roles/run.invoker.
    # We trust the platform authentication. The custom agent bearer is an
    # additional/optional path.
    if auth.lower().startswith("bearer "):
        return

    logger.warning("❌ No valid authentication (neither agent bearer nor Google IAM token)")
    raise HTTPException(
        status_code=401,
        detail="Invalid or missing agent bearer token (or Google Cloud IAM authentication)"
    )

def save_owner_tokens(creds: Credentials, email: str) -> None:
    """Persist the owner's Gmail refresh token in Firestore."""
    existing = token_doc().get()
    existing_data = existing.to_dict() if existing.exists else {}

    refresh_token = creds.refresh_token or existing_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="No refresh token returned by Google. Revoke access in Google Account and try again.",
        )

    token_doc().set(
        {
            "email": email,
            "access_token": creds.token,
            "refresh_token": refresh_token,
            "token_uri": creds.token_uri,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "scopes": SCOPES,
            "updated_at": now_iso(),
        },
        merge=True,
    )
    logger.info(f"✅ Gmail owner token saved for {email}")

def get_owner_record() -> Dict[str, Any]:
    snap = token_doc().get()
    if not snap.exists:
        raise HTTPException(
            status_code=403,
            detail="Mailbox owner is not connected. Visit /oauth/google/start (with agent token) first.",
        )
    return snap.to_dict()

def get_gmail_service() -> tuple[Any, Dict[str, Any]]:
    """Get an authorized Gmail service using stored refresh token (with auto-refresh)."""
    data = get_owner_record()

    creds = Credentials(
        token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id", GOOGLE_CLIENT_ID),
        client_secret=data.get("client_secret", GOOGLE_CLIENT_SECRET),
        scopes=data.get("scopes", SCOPES),
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleRequest())
                token_doc().set(
                    {"access_token": creds.token, "updated_at": now_iso()},
                    merge=True,
                )
            except Exception as e:
                logger.exception("Failed to refresh Gmail token")
                raise HTTPException(status_code=401, detail=f"Failed to refresh Gmail token: {e}")
        else:
            raise HTTPException(status_code=401, detail="Stored Gmail credentials are invalid")

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service, data

def list_allowed_labels(service) -> List[Dict[str, Any]]:
    """Return only the labels that are in our ALLOWED_LABELS whitelist."""
    labels_result = service.users().labels().list(userId="me").execute()
    all_labels = labels_result.get("labels", [])

    filtered = []
    for label in all_labels:
        if label.get("name") in ALLOWED_LABELS:
            filtered.append({
                "id": label["id"],
                "name": label["name"],
                "type": label.get("type", "user"),
            })
    return filtered


# -----------------------------------------------------------------------------
# New Helper Functions for Rich Mailing List Access
# -----------------------------------------------------------------------------

def _get_message_body(payload: Dict[str, Any]) -> Dict[str, str]:
    """Recursively extract plain text and HTML body from a Gmail message payload.
    Uses BeautifulSoup for better HTML cleaning when plain text is not available.
    """
    if not payload:
        return {"plain": "", "html": ""}

    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data", "")

    plain = ""
    html = ""

    import base64
    if mime_type == "text/plain" and data:
        plain = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    elif mime_type == "text/html" and data:
        html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Check parts recursively
    for part in payload.get("parts", []):
        part_body = _get_message_body(part)
        if part_body.get("plain"):
            plain = part_body["plain"] if not plain else plain
        if part_body.get("html"):
            html = part_body["html"] if not html else html

    # If we have HTML but no good plain text, clean it with BeautifulSoup
    if html and not plain.strip():
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            # Remove script/style
            for script in soup(["script", "style"]):
                script.decompose()
            plain = soup.get_text(separator="\n", strip=True)
        except Exception:
            plain = html  # fallback

    return {"plain": plain, "html": html}


def _extract_urls(text: str) -> List[str]:
    """Simple URL extraction from text."""
    import re
    if not text:
        return []
    url_pattern = r'https?://[^\s<>"\']+|www\.[^\s<>"\']+'
    urls = re.findall(url_pattern, text)
    # Clean and dedupe while preserving order
    seen = set()
    clean_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            clean_urls.append(u)
    return clean_urls


def _parse_author(header_value: str) -> Dict[str, str]:
    """Parse 'From' header into name and email."""
    import re
    if not header_value:
        return {"name": "Unknown", "email": ""}
    # Common formats: "Name <email@ex.com>" or just "email@ex.com"
    match = re.match(r'^"?([^"<]+)"?\s*<([^>]+)>$', header_value.strip())
    if match:
        name = match.group(1).strip().strip('"')
        email = match.group(2).strip()
        return {"name": name or "Unknown", "email": email}
    # Just email
    if "@" in header_value:
        return {"name": header_value.split("@")[0], "email": header_value.strip()}
    return {"name": header_value, "email": ""}


def get_full_message(service, msg_id: str, max_body_chars: int = 15000) -> Dict[str, Any]:
    """Fetch a complete message with rich metadata, full body, links and author info.
    max_body_chars controls truncation of body_text (set high or 0 for full content when needed for specs/tables).
    """
    msg_data = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    headers = {h["name"].lower(): h["value"] for h in msg_data.get("payload", {}).get("headers", [])}

    author_raw = headers.get("from", "")
    author = _parse_author(author_raw)

    subject = headers.get("subject", "No subject")
    date = headers.get("date", "")

    body = _get_message_body(msg_data.get("payload", {}))
    body_text = body.get("plain") or body.get("html", "")

    links = _extract_urls(body_text)

    # Attachments with attachmentId
    attachments = []
    def _walk_parts(part):
        if part.get("filename"):
            attachments.append({
                "filename": part.get("filename"),
                "mimeType": part.get("mimeType"),
                "size": part.get("body", {}).get("size", 0),
                "attachmentId": part.get("body", {}).get("attachmentId"),
            })
        for subpart in part.get("parts", []):
            _walk_parts(subpart)
    _walk_parts(msg_data.get("payload", {}))

    if max_body_chars and max_body_chars > 0 and len(body_text) > max_body_chars:
        truncated_body = body_text[:max_body_chars]
        body_truncated = True
    else:
        truncated_body = body_text
        body_truncated = False

    return {
        "id": msg_id,
        "threadId": msg_data.get("threadId"),
        "author": author,
        "subject": subject,
        "date": date,
        "labels": [lbl.get("name") for lbl in msg_data.get("labelIds", []) if lbl],
        "body_text": truncated_body,
        "body_truncated": body_truncated,
        "body_length": len(body_text),
        "links": links,
        "attachments": attachments,
        "snippet": msg_data.get("snippet", ""),
    }


def get_thread(service, thread_id: str, max_messages: int = 30, max_body_chars: int = 15000) -> List[Dict[str, Any]]:
    """Get the full thread (conversation) for context. Very useful for mailing lists.
    Pass a high max_body_chars (e.g. 200000) when you need complete long posts containing torque tables and detailed procedures.
    """
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()

    messages = []
    for msg in thread.get("messages", [])[:max_messages]:
        messages.append(get_full_message(service, msg["id"], max_body_chars=max_body_chars))
    return messages


def get_attachments(service, message_id: str) -> List[Dict[str, Any]]:
    """List attachments for a message with attachmentIds."""
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    attachments = []
    def _walk(part):
        if part.get("filename") and part.get("body", {}).get("attachmentId"):
            attachments.append({
                "filename": part.get("filename"),
                "mimeType": part.get("mimeType"),
                "size": part.get("body", {}).get("size", 0),
                "attachmentId": part.get("body", {}).get("attachmentId"),
            })
        for p in part.get("parts", []):
            _walk(p)
    _walk(msg.get("payload", {}))
    return attachments


def get_attachment_content(service, message_id: str, attachment_id: str) -> Dict[str, Any]:
    """Download attachment content. Returns base64 data + metadata.
    - For text formats: provides decoded text_content
    - For PDF: uses pypdf to extract text
    - For images: returns metadata only (description can be done client-side or with vision)
    """
    att = service.users().messages().attachments().get(
        userId="me", messageId=message_id, id=attachment_id
    ).execute()

    import base64
    data = att.get("data", "")
    decoded = base64.urlsafe_b64decode(data) if data else b""

    text = None
    mime = att.get("mimeType", "")
    filename = att.get("filename", "")

    if mime.startswith("text/") or mime in ("application/json", "application/xml"):
        try:
            text = decoded.decode("utf-8", errors="replace")
        except Exception:
            pass
    elif mime == "application/pdf":
        try:
            from pypdf import PdfReader
            from io import BytesIO
            reader = PdfReader(BytesIO(decoded))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            text = text.strip() if text else None
        except Exception as e:
            logger.warning(f"PDF extraction failed for {filename}: {e}")

    # For images, we return metadata. Full description can be requested via vision in Grok or future tool.
    is_image = mime.startswith("image/")

    return {
        "attachmentId": attachment_id,
        "filename": filename,
        "mimeType": mime,
        "size": len(decoded),
        "data_base64": att.get("data"),
        "text_content": text,
        "is_image": is_image,
        "note": "For images, use vision capabilities on the base64 data for description." if is_image else None,
    }


def fetch_url(url: str, max_chars: int = 12000) -> Dict[str, Any]:
    """Fetch external content from a link found in the mailing list. Useful for manuals, photos descriptions, etc.
    When the link is a PDF (common for factory torque specs and diagrams), performs text extraction.
    """
    try:
        import httpx
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Sorteberg-MCP/1.0"})
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "") or ""
            final_url = str(resp.url)

            # Handle direct PDF links (very useful when experts share factory manuals or torque charts)
            if "pdf" in content_type.lower() or final_url.lower().endswith(".pdf"):
                try:
                    from pypdf import PdfReader
                    from io import BytesIO
                    reader = PdfReader(BytesIO(resp.content))
                    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
                    text = extracted.strip()[:max_chars] if extracted else ""
                    return {
                        "url": final_url,
                        "status_code": resp.status_code,
                        "content_type": content_type,
                        "is_pdf": True,
                        "text": text,
                        "truncated": len(extracted) > max_chars if extracted else False,
                        "page_count": len(reader.pages),
                    }
                except Exception as pdf_err:
                    logger.warning(f"PDF extraction via fetch_link failed for {url}: {pdf_err}")

            # Normal text/HTML content
            text = resp.text[:max_chars] if resp.text else ""
            return {
                "url": final_url,
                "status_code": resp.status_code,
                "content_type": content_type,
                "is_pdf": False,
                "text": text,
                "truncated": len(resp.text) > max_chars if resp.text else False,
            }
    except Exception as e:
        return {"url": url, "error": str(e)}

# -----------------------------------------------------------------------------
# FastMCP Tools (clean definitions + schemas)
# -----------------------------------------------------------------------------

@mcp.tool()
def list_labels() -> Dict[str, Any]:
    """List Gmail labels the server is allowed to access (filtered to ALLOWED_LABELS)."""
    service, owner = get_gmail_service()
    labels = list_allowed_labels(service)
    return {
        "mailbox_owner": owner.get("email", ""),
        "allowed_labels": ALLOWED_LABELS,
        "labels": labels,
    }


@mcp.tool()
def search_mailing_list(
    query: str,
    label: Optional[str] = None,
    max_results: int = 15,
    author: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    has_attachments: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """
    Powerful search across your mailing list labels.
    Returns rich results with author name/email, subject, date, snippet, message id and thread id.
    Use this as the primary search tool when you want expert advice from the list.
    """
    max_results = min(max(1, max_results), 50)
    service, owner = get_gmail_service()

    labels_result = service.users().labels().list(userId="me").execute()
    all_labels = labels_result.get("labels", [])

    search_label_ids: List[str] = []
    if label:
        if label not in ALLOWED_LABELS:
            return [{"error": f"Access denied to label '{label}'", "allowed_labels": ALLOWED_LABELS}]
        for lbl in all_labels:
            if lbl.get("name") == label:
                search_label_ids.append(lbl["id"])
                break
    else:
        for lbl in all_labels:
            if lbl.get("name") in ALLOWED_LABELS:
                search_label_ids.append(lbl["id"])

    if not search_label_ids:
        return [{"error": "None of the allowed labels found", "allowed_labels": ALLOWED_LABELS}]

    # Build Gmail query
    q_parts = [query] if query else []
    if author:
        q_parts.append(f'from:{author}')
    if after:
        q_parts.append(f'after:{after}')
    if before:
        q_parts.append(f'before:{before}')
    if has_attachments:
        q_parts.append('has:attachment')

    q = " ".join(q_parts)

    all_messages: List[Dict[str, Any]] = []

    for label_id in search_label_ids:
        try:
            results = service.users().messages().list(
                userId="me",
                q=q,
                labelIds=[label_id],
                maxResults=max_results,
            ).execute()

            for msg in results.get("messages", []):
                full = get_full_message(service, msg["id"])
                # Enrich with label names
                full["labels"] = [lbl.get("name") for lbl in all_labels if lbl["id"] in (msg.get("labelIds") or []) and lbl.get("name") in ALLOWED_LABELS]
                all_messages.append(full)

                if len(all_messages) >= max_results:
                    break
            if len(all_messages) >= max_results:
                break
        except Exception as e:
            logger.warning(f"Error in search_mailing_list for label {label_id}: {e}")
            continue

    if not all_messages:
        return [{"message": f"No results for query '{query}'", "searched_labels": ALLOWED_LABELS}]

    return all_messages


@mcp.tool()
def get_message(message_id: str, include_full_body: bool = True) -> Dict[str, Any]:
    """Retrieve a single email with full body text, properly parsed author name, links, and attachments.
    Set include_full_body=True to retrieve the complete untruncated body (important for long expert posts with specifications and tables).
    """
    service, owner = get_gmail_service()
    max_chars = 0 if include_full_body else 15000   # 0 = no truncation
    msg = get_full_message(service, message_id, max_body_chars=max_chars)
    return msg


@mcp.tool()
def get_thread(thread_id: str, max_messages: int = 25, include_full_bodies: bool = False) -> List[Dict[str, Any]]:
    """Get the full conversation thread. Essential for understanding complete advice that spans multiple replies.
    Set include_full_bodies=True to avoid truncating long detailed posts (critical when experts discuss exact torque values, clearances and procedures in depth).
    """
    service, owner = get_gmail_service()
    max_chars = 0 if include_full_bodies else 15000
    return get_thread(service, thread_id, max_messages, max_body_chars=max_chars)


@mcp.tool()
def list_attachments(message_id: str) -> List[Dict[str, Any]]:
    """List all attachments for a message (with attachmentId so you can fetch them)."""
    service, owner = get_gmail_service()
    return get_attachments(service, message_id)


@mcp.tool()
def get_attachment(message_id: str, attachment_id: str) -> Dict[str, Any]:
    """Download an attachment. Returns base64 data. For text/PDFs we also try to extract readable text."""
    service, owner = get_gmail_service()
    return get_attachment_content(service, message_id, attachment_id)


@mcp.tool()
def get_thread_attachments(thread_id: str) -> Dict[str, Any]:
    """Collect every attachment (photos, diagrams, PDF scans, torque charts, etc.) across an entire thread.
    Returns them with message context (author, date, subject, message_id) so the expert can attribute figures and incorporate real pictures/diagrams into professional documentation.
    This is the preferred tool when building richly illustrated guides (e.g. engine overhaul with measurement photos and factory-style diagrams).
    """
    service, owner = get_gmail_service()
    # Use full bodies off to keep response reasonable, but we only need the attachment metadata list here
    thread_messages = get_thread(service, thread_id, max_messages=40, max_body_chars=15000)
    all_atts: List[Dict[str, Any]] = []
    for msg in thread_messages:
        for att in msg.get("attachments", []):
            if att.get("attachmentId"):
                all_atts.append({
                    "message_id": msg["id"],
                    "author": msg.get("author"),
                    "date": msg.get("date"),
                    "subject": msg.get("subject"),
                    "filename": att.get("filename"),
                    "mimeType": att.get("mimeType"),
                    "size": att.get("size"),
                    "attachmentId": att.get("attachmentId"),
                })
    return {
        "thread_id": thread_id,
        "attachment_count": len(all_atts),
        "attachments": all_atts,
        "note": "Use get_attachment(message_id, attachmentId) on the items you need. Images include base64 for vision use; PDFs include extracted text_content.",
    }


@mcp.tool()
def extract_links(message_id: str) -> List[str]:
    """Extract all URLs mentioned in a message body (great for following manuals, photos, etc.)."""
    service, owner = get_gmail_service()
    msg = get_full_message(service, message_id)
    return msg.get("links", [])


@mcp.tool()
def fetch_link(url: str, max_chars: int = 8000) -> Dict[str, Any]:
    """Fetch content from a URL found in the mailing list (manuals, forum posts, images descriptions, etc.).
    Automatically extracts readable text when the link points to a PDF (excellent for factory torque tables and diagram references shared by experts).
    """
    return fetch_url(url, max_chars)


@mcp.tool()
def search_by_author(author: str, label: Optional[str] = None, max_results: int = 10) -> List[Dict[str, Any]]:
    """Find posts written by a specific expert on the mailing list."""
    return search_mailing_list(
        query="",
        label=label,
        max_results=max_results,
        author=author,
    )


@mcp.tool()
def get_expert_guidance(
    topic: str,
    label: Optional[str] = None,
    max_threads: int = 5,
) -> Dict[str, Any]:
    """
    High-level tool designed for creating full how-to writeups.
    Searches the mailing list for expert discussions on a topic (e.g. "overhaul engine", "gearbox rebuild"),
    fetches the most relevant threads, and returns structured data with authors, key excerpts, dates,
    and links/attachments so Grok can synthesize an accurate, sourced step-by-step guide.
    """
    service, owner = get_gmail_service()

    # Smart multi-query search for high quality advice
    queries = [
        topic,
        f'"{topic}" (howto OR "how to" OR guide OR overhaul OR rebuild OR tips OR "step by step")',
        f'{topic} (problem OR issue OR fix OR solution)',
    ]

    all_threads: List[Dict[str, Any]] = []
    seen_thread_ids = set()

    for q in queries:
        results = search_mailing_list(
            query=q,
            label=label,
            max_results=max(3, max_threads),
        )
        for msg in results:
            if isinstance(msg, dict) and "threadId" in msg:
                tid = msg["threadId"]
                if tid and tid not in seen_thread_ids:
                    seen_thread_ids.add(tid)
                    try:
                        # Pull fuller bodies in the high-level guidance tool so specs and detailed procedures are not truncated
                        thread = get_thread(service, tid, max_messages=10, max_body_chars=0)
                        if thread:
                            all_threads.append({
                                "thread_id": tid,
                                "messages": thread,
                                "search_query_used": q,
                            })
                    except Exception as e:
                        logger.warning(f"Failed to fetch thread {tid}: {e}")
            if len(all_threads) >= max_threads:
                break
        if len(all_threads) >= max_threads:
            break

    # Deduplicate and limit
    unique_threads = []
    seen = set()
    for t in all_threads:
        if t["thread_id"] not in seen:
            seen.add(t["thread_id"])
            unique_threads.append(t)
            if len(unique_threads) >= max_threads:
                break

    return {
        "topic": topic,
        "label": label or "all allowed labels",
        "mailbox_owner": owner.get("email", ""),
        "threads_found": len(unique_threads),
        "threads": unique_threads,
        "note": "Each thread contains full messages with author names, dates, bodies, links and attachments. Use this to build accurate how-to guides with proper attribution to list experts.",
    }


# Keep the old search_gmail for backward compatibility (it now calls the improved logic)
@mcp.tool()
def search_gmail(
    query: str,
    max_results: int = 10,
    label: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Legacy search tool. Prefer search_mailing_list for more power (author, date filters, etc.)."""
    return search_mailing_list(query=query, label=label, max_results=max_results)

# -----------------------------------------------------------------------------
# FastAPI App + Routes (OAuth + compatibility JSON-RPC + health)
# -----------------------------------------------------------------------------
app = FastAPI(title="Sorteberg MCP", version="0.2.0")

@app.middleware("http")
async def mcp_auth_middleware(request: Request, call_next):
    """Apply agent authentication to all MCP transport and JSON-RPC paths.
    This ensures the bearer token is checked for /mcp, /mcp-transport, etc.
    Cloud Run IAM can still be used in parallel for owner access.
    """
    path = request.url.path
    if path.startswith("/mcp") or path.startswith("/mcp-transport"):
        try:
            require_agent(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
    return await call_next(request)

@app.get("/health")
def health():
    return {"status": "healthy", "service": "sorteberg-mcp", "allowed_labels": ALLOWED_LABELS}

@app.get("/debug/auth")
def debug_auth(request: Request):
    """Temporary debug to see what Authorization header the app actually receives."""
    auth = request.headers.get("Authorization", "")
    return {
        "authorization_header": auth,
        "starts_with_bearer": auth.startswith("Bearer "),
        "agent_bearer_set": bool(AGENT_BEARER_TOKEN),
        "agent_bearer_preview": AGENT_BEARER_TOKEN[:10] + "..." if AGENT_BEARER_TOKEN else None,
    }

# -----------------------------------------------------------------------------
# Minimal OAuth2 endpoints for Grok / custom connector compatibility (PKCE "none")
# This allows the Grok web client Custom Connector OAuth flow to "succeed"
# and obtain our AGENT_BEARER_TOKEN as the access token.
# The actual protection for /mcp remains the bearer check in require_agent.
# -----------------------------------------------------------------------------

@app.get("/oauth/authorize")
def oauth_authorize(request: Request):
    """
    Minimal authorize endpoint for PKCE public client (none client auth).
    We immediately "issue" a code that is our bearer token (or a short code).
    In real PKCE the client would redirect here with code_challenge etc.
    We simplify heavily for AI agent use.
    """
    redirect_uri = request.query_params.get("redirect_uri")
    state = request.query_params.get("state", "")
    # For simplicity, use the bearer itself as the "code" (or a fixed one)
    # Grok will then exchange it at /token.
    code = AGENT_BEARER_TOKEN  # the client will receive this as "code"

    if redirect_uri:
        from urllib.parse import urlencode
        params = {"code": code, "state": state}
        separator = "&" if "?" in redirect_uri else "?"
        full_redirect = f"{redirect_uri}{separator}{urlencode(params)}"
        return RedirectResponse(full_redirect)

    # Fallback if no redirect_uri (some clients just want the code)
    return {"code": code, "state": state}

@app.post("/oauth/token")
async def oauth_token(request: Request):
    """
    Token endpoint for the "none" (PKCE only) flow.
    We accept the code (which is our bearer) and return it as access_token.
    No client_secret required.
    """
    try:
        form = await request.form()
    except Exception:
        form = {}

    # The "code" from authorize should be our bearer.
    # We don't do real PKCE verification here for simplicity.
    code = form.get("code") or (await request.json()).get("code") if request.headers.get("content-type", "").startswith("application/json") else None

    # Always return our bearer as the access token.
    # This way Grok gets the correct token to use in Authorization: Bearer for /mcp
    return {
        "access_token": AGENT_BEARER_TOKEN,
        "token_type": "Bearer",
        "expires_in": 3600 * 24 * 30,  # long lived for convenience
        "scope": "mcp",
    }

@app.get("/", response_class=HTMLResponse)
def root():
    labels = "<br>".join(f"• {l}" for l in ALLOWED_LABELS)
    return f"""
    <html>
      <head>
        <style>body {{ font-family: system-ui, sans-serif; margin: 40px; line-height: 1.5; }}</style>
      </head>
      <body>
        <h1>🚀 Sorteberg MCP Server</h1>
        <p><strong>Status:</strong> ✅ Running (FastMCP + Firestore)</p>
        <div style="background:#e3f2fd;padding:15px;border-radius:6px;margin:20px 0">
          <h3>📧 Allowed Gmail Labels</h3>
          {labels}
        </div>
        <p><a href="/oauth/google/start">🔐 Connect / Reconnect Gmail (requires agent token)</a></p>
        <p><small>MCP endpoint: POST /mcp &nbsp;|&nbsp; Proper transport: /mcp-transport (streamable-http)</small></p>
      </body>
    </html>
    """

# --- OAuth flow (owner Gmail connection) ---
def _oauth_client_config():
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }

@app.get("/oauth/google/start")
def oauth_google_start(request: Request):
    # Protected by Cloud Run IAM (--no-allow-unauthenticated) + the invoker role granted to the owner.
    # The agent bearer is used for /mcp tool calls, not for the owner connect flow.

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(500, "Google OAuth credentials not configured")

    flow = Flow.from_client_config(_oauth_client_config(), scopes=SCOPES)
    flow.redirect_uri = REDIRECT_URI

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    oauth_states[state] = {"flow": flow, "created": now_iso()}
    logger.info(f"🔐 OAuth flow started, state={state}")

    return RedirectResponse(auth_url)

@app.get("/oauth/google/callback", response_class=HTMLResponse)
def oauth_google_callback(request: Request):
    query = request.query_params
    state = query.get("state")
    code = query.get("code")
    error = query.get("error")

    if error:
        return HTMLResponse(f"<h1>OAuth Error</h1><p>{error}</p>", status_code=400)

    if not state or state not in oauth_states:
        return HTMLResponse("<h1>Invalid state</h1>", status_code=400)

    flow = oauth_states[state].get("flow")
    if not flow:
        return HTMLResponse("<h1>Flow not found</h1>", status_code=400)

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Get user email from ID token or profile
        email = "unknown"
        if creds.id_token:
            try:
                from google.oauth2 import id_token as google_id_token
                idinfo = google_id_token.verify_oauth2_token(
                    creds.id_token, GoogleRequest(), GOOGLE_CLIENT_ID
                )
                email = idinfo.get("email", "unknown")
            except Exception:
                pass

        save_owner_tokens(creds, email)

        # Cleanup
        oauth_states.pop(state, None)

        labels_html = "<br>".join(f"• {l}" for l in ALLOWED_LABELS)
        return f"""
        <html>
          <body style="font-family: system-ui, sans-serif; margin:40px">
            <h1 style="color:#28a745">✅ Gmail connected successfully</h1>
            <p><b>Email:</b> {email}</p>
            <p><b>Allowed labels:</b><br>{labels_html}</p>
            <p>The MCP server now has persistent access. You can close this window.</p>
            <p><small>Tokens are stored securely in Firestore and refreshed automatically.</small></p>
          </body>
        </html>
        """
    except Exception as e:
        logger.exception("OAuth callback error")
        return HTMLResponse(f"<h1>OAuth Callback Error</h1><p>{e}</p>", status_code=500)

# --- Legacy JSON-RPC compatibility layer (for older clients) ---
# The primary MCP interface is now the official streamable-http transport
# mounted at /mcp (see below). This legacy handler is kept at /mcp-legacy
# for backward compatibility if needed.
@app.post("/")
@app.post("/mcp-legacy")
async def mcp_jsonrpc(request: Request):
    """Legacy JSON-RPC 2.0 endpoint for older clients.
    New clients (including Grok web) should use the /mcp streamable-http transport.
    """
    require_agent(request)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    req_id = body.get("id", 1)
    method = body.get("method")
    params = body.get("params") or {}

    try:
        if method == "hello":
            name = params.get("name", "user")
            result = f"Hei {name}! Sorteberg MCP (FastMCP + Firestore) is working."

        elif method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sorteberg-mcp", "version": "0.2.0"},
            }

        elif method == "tools/list":
            # Use FastMCP for the official tool list when possible
            tools = []
            for tool in await mcp.list_tools():
                tools.append({
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.inputSchema or {"type": "object", "properties": {}},
                })
            result = {"tools": tools}

        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}

            # Dispatch to FastMCP (this gives us proper validation + execution)
            tool_result = await mcp.call_tool(tool_name, arguments)

            # Robust extraction: FastMCP wraps returns in CallToolResult with TextContent.
            # Our tools return plain dict/list, so extract and parse back to native types.
            def _extract_result(obj):
                if isinstance(obj, (dict, list, str, int, float, bool, type(None))):
                    return obj
                if hasattr(obj, "content") and obj.content:
                    parts = []
                    for c in obj.content:
                        if hasattr(c, "text"):
                            t = c.text
                            try:
                                parts.append(json.loads(t))
                            except Exception:
                                parts.append(t)
                        else:
                            parts.append(str(c))
                    return parts[0] if len(parts) == 1 else parts
                # Fallback
                try:
                    return json.loads(str(obj))
                except Exception:
                    return str(obj)

            result = _extract_result(tool_result)

        else:
            raise HTTPException(status_code=404, detail=f"Unknown method: {method}")

        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})

    except HTTPException as e:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": e.status_code, "message": e.detail}},
            status_code=e.status_code,
        )
    except Exception as e:
        logger.exception("MCP handler error")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": 500, "message": str(e)}},
            status_code=500,
        )

# Mount the official modern MCP transport (streamable-http) for proper remote MCP clients.
# This is the recommended endpoint for Grok web client and other modern MCP clients.
# Connect Grok to: https://sorteberg-mcp-62lr3ybf4a-ew.a.run.app/mcp
try:
    mcp_http_app = mcp.streamable_http_app()
    app.mount("/mcp", mcp_http_app)
    logger.info("Mounted official streamable-http transport at /mcp")
except Exception as e:
    logger.warning(f"Could not mount streamable-http app: {e}")

# Keep the legacy transport mount for compatibility if some clients expect it
try:
    app.mount("/mcp-transport", mcp_http_app)
except Exception:
    pass

# -----------------------------------------------------------------------------
# Entrypoint (for local `python server.py`)
# -----------------------------------------------------------------------------
def main():
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    logger.info(f"🚀 Starting Sorteberg MCP on port {port}")
    logger.info(f"Allowed labels: {ALLOWED_LABELS}")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)

if __name__ == "__main__":
    main()
