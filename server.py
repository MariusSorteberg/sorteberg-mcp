"""
KnowledgeForge - MCP Server for Private Archives and Expert Knowledge

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
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
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
    for label in os.getenv("ALLOWED_LABELS", "Expert Mailing List,Technical Discussions").split(",")
    if label.strip()
]

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI") or os.getenv("BASE_URL", "https://your-knowledgeforge-service.a.run.app")

# Agent / Grok authentication (proper inbound auth)
# Caller must send: Authorization: Bearer <this token>
AGENT_BEARER_TOKEN = os.getenv("AGENT_BEARER_TOKEN") or os.getenv("MCP_BEARER_TOKEN", "")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Google Drive scopes (readonly for input sources + file for writing howtos to output folder)
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

DRIVE_INPUT_FOLDER_ID = os.getenv("DRIVE_INPUT_FOLDER_ID", "")
DRIVE_OUTPUT_FOLDER_ID = os.getenv("DRIVE_OUTPUT_FOLDER_ID", "")

# Vertex AI Vector Search config (for semantic search over emails + Drive docs)
VERTEX_PROJECT = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT", "")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
# The Vector Search index resource name, e.g. projects/.../locations/.../indexes/...
VECTOR_INDEX_NAME = os.getenv("VECTOR_INDEX_NAME", "")
# Optional: deployed index endpoint for queries if using endpoint
VECTOR_INDEX_ENDPOINT = os.getenv("VECTOR_INDEX_ENDPOINT", "")
# The deployed_index_id you chose when running deploy-index (e.g. knowledge_forge_index)
VECTOR_DEPLOYED_INDEX_ID = os.getenv("VECTOR_DEPLOYED_INDEX_ID", "knowledge_forge_index")
# For embeddings model
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-005")

# Authors known to provide high-quality, reliable technical information.
# Their posts will be injected with context during embedding and boosted in hybrid/guidance results.
TRUSTED_AUTHORS = [
    a.strip() for a in os.getenv("TRUSTED_AUTHORS", "John Titus,Darren Kriticos").split(",")
    if a.strip()
]

# Firestore storage for the Gmail owner tokens (persistent, survives restarts)
TOKEN_COLLECTION = "mcp_config"
TOKEN_DOCUMENT = "gmail_owner"

# In-memory short-lived state for OAuth flows (stateless Cloud Run friendly for short flows)
oauth_states: Dict[str, Dict[str, Any]] = {}

# -----------------------------------------------------------------------------
# FastMCP - Proper tool definitions
# -----------------------------------------------------------------------------
mcp = FastMCP("knowledge-forge")

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
    We trust the platform for those calls (including allUsers or specific users).

    The custom AGENT_BEARER_TOKEN is supported for direct calls.
    Requests with no Authorization header are allowed (Cloud Run already enforced invoker).
    """
    auth = request.headers.get("Authorization", "")

    if not auth:
        # No header: rely on Cloud Run IAM (allUsers or signed-in user with invoker)
        return

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


def save_drive_tokens(creds: Credentials) -> None:
    """Persist Drive tokens (refresh preferred, but access token alone can work temporarily)."""
    existing = token_doc().get()
    existing_data = existing.to_dict() if existing.exists else {}

    refresh_token = creds.refresh_token or existing_data.get("drive_refresh_token")
    access_token = creds.token or existing_data.get("drive_access_token")

    if not refresh_token and not access_token:
        raise HTTPException(
            status_code=400,
            detail="No Drive tokens returned. Revoke access and try again with full consent (prompt=consent + access_type=offline).",
        )

    token_doc().set(
        {
            "drive_access_token": access_token,
            "drive_refresh_token": refresh_token,
            "drive_token_uri": creds.token_uri or existing_data.get("drive_token_uri"),
            "drive_client_id": GOOGLE_CLIENT_ID,
            "drive_client_secret": GOOGLE_CLIENT_SECRET,
            "drive_scopes": DRIVE_SCOPES,
            "drive_updated_at": now_iso(),
        },
        merge=True,
    )
    if refresh_token:
        logger.info("✅ Drive refresh token saved for owner")
    else:
        logger.warning("✅ Drive access token saved (no refresh token this time - will need re-auth later)")


def get_drive_service():
    """Get an authorized Drive service. Separate from Gmail for clarity.
    Supports cases where only an access_token was returned (no refresh_token).
    """
    data = get_owner_record()

    access_token = data.get("drive_access_token")
    refresh_token = data.get("drive_refresh_token")

    if not access_token and not refresh_token:
        raise HTTPException(
            status_code=403,
            detail="Drive not connected for this owner. Visit /oauth/google/drive/start with agent token.",
        )

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=data.get("drive_token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("drive_client_id", GOOGLE_CLIENT_ID),
        client_secret=data.get("drive_client_secret", GOOGLE_CLIENT_SECRET),
        scopes=data.get("drive_scopes", DRIVE_SCOPES),
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleRequest())
                token_doc().set(
                    {"drive_access_token": creds.token, "drive_updated_at": now_iso()},
                    merge=True,
                )
            except Exception as e:
                logger.exception("Failed to refresh Drive token")
                raise HTTPException(status_code=401, detail=f"Failed to refresh Drive token: {e}")
        elif not creds.token:
            raise HTTPException(status_code=401, detail="Stored Drive credentials are invalid or expired (no refresh token available)")
        # else: use the current (possibly still valid) access token

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return service


# -----------------------------------------------------------------------------
# Vertex AI Vector Search helpers (first phase - manual/scheduled)
# -----------------------------------------------------------------------------

def _init_vertex():
    """Lazy init Vertex AI."""
    import vertexai
    if VERTEX_PROJECT:
        vertexai.init(project=VERTEX_PROJECT, location=VERTEX_LOCATION)


def _get_embedding(
    text: str,
    task_type: str = "RETRIEVAL_DOCUMENT",
    title: Optional[str] = None,
) -> List[float]:
    """Generate embedding using Vertex AI text-embedding-005 (or configured model).

    Use task_type="RETRIEVAL_DOCUMENT" (with optional title) when embedding chunks during ingest.
    Use task_type="RETRIEVAL_QUERY" when embedding user queries for search.
    This greatly improves relevance for asymmetric retrieval tasks.
    """
    if not VERTEX_PROJECT or not VECTOR_INDEX_NAME:
        raise HTTPException(500, "Vertex AI Vector Search not configured (set VERTEX_PROJECT and VECTOR_INDEX_NAME)")
    _init_vertex()
    from vertexai.language_models import TextEmbeddingModel
    model = TextEmbeddingModel.from_pretrained(EMBEDDING_MODEL)
    # Trim aggressively; embeddings have input limits
    safe_text = (text or "")[:10000]
    kwargs = {"task_type": task_type}
    if title:
        kwargs["title"] = title
    embeddings = model.get_embeddings([safe_text], **kwargs)
    return embeddings[0].values


def _chunk_text(
    text: str,
    base_meta: Dict[str, Any],
    max_chars: int = 4000,
    context_prefix: str = "",
) -> List[Dict[str, Any]]:
    """Split text into embedding-friendly chunks.
    Tries to keep paragraphs and especially table-like blocks together for specs/torques.
    Adds overlap and optional context_prefix (author, subject, date) to every chunk
    so that vector embeddings capture provenance and quality signals.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Prepend context so every chunk knows who wrote it and the context
    if context_prefix:
        full_text = f"{context_prefix}\n\n{text}"
    else:
        full_text = text

    if len(full_text) <= max_chars:
        m = dict(base_meta)
        m["chunk_index"] = 0
        return [{"text": full_text, "meta": m}]

    # Prefer splitting on blank lines (paragraphs). Keep large table blocks intact when possible.
    paragraphs = re.split(r'\n\s*\n+', full_text)
    chunks: List[str] = []
    current = ""
    overlap = 400  # characters of overlap for continuity

    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(current) + len(p) + 2 <= max_chars:
            current = (current + "\n\n" + p).strip()
        else:
            if current:
                chunks.append(current)
            if len(p) > max_chars:
                start = 0
                while start < len(p):
                    chunk = p[start : start + max_chars]
                    chunks.append(chunk)
                    start += max_chars - overlap
            else:
                current = p
    if current:
        chunks.append(current)

    result = []
    for i, c in enumerate(chunks):
        m = dict(base_meta)
        m["chunk_index"] = i
        result.append({"text": c, "meta": m})
    return result


def _upsert_chunks(chunks: List[Dict[str, Any]]):
    """Upsert chunks to Vertex AI Vector Search index.
    chunks: [{"id": "...", "embedding": [...], "metadata": {...}}]
    Uses restricts for metadata filtering support.
    """
    if not VECTOR_INDEX_NAME:
        raise HTTPException(500, "VECTOR_INDEX_NAME not set")
    import google.cloud.aiplatform as aiplatform
    index = aiplatform.MatchingEngineIndex(index_name=VECTOR_INDEX_NAME)
    datapoints = []
    for chunk in chunks:
        restricts = []
        for key, val in (chunk.get("metadata") or {}).items():
            if val is not None and val != "":
                allow = [str(val)] if not isinstance(val, list) else [str(v) for v in val if v]
                if allow:
                    restricts.append({"namespace": key, "allow_list": allow})
        dp = {
            "datapoint_id": chunk["id"],
            "feature_vector": chunk["embedding"],
        }
        if restricts:
            dp["restricts"] = restricts
        datapoints.append(dp)

    if datapoints:
        index.upsert_datapoints(datapoints=datapoints)
        logger.info(f"Upserted {len(datapoints)} chunks to Vector Search")


def _expand_query_for_vector(query: str) -> str:
    """Lightweight query expansion/rewriting to improve vector recall.
    Technical mailing list topics often benefit from adding domain terms.
    """
    q = query.strip()
    if not q:
        return q
    expansions = [
        "technical advice",
        "procedure",
        "how to",
        "tips",
        "solution",
        "expert discussion",
        "specs",
    ]
    # Avoid duplicating if already present
    expanded = q
    lower = q.lower()
    for exp in expansions:
        if exp not in lower:
            expanded += f" {exp}"
    return expanded


def _semantic_search_impl(query: str, top_k: int = 8, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Internal semantic search impl. Returns [{'id':, 'score':, 'metadata': {...}}]"""
    if not VECTOR_INDEX_NAME:
        return []
    import google.cloud.aiplatform as aiplatform
    _init_vertex()
    expanded_query = _expand_query_for_vector(query)
    embedding = _get_embedding(expanded_query, task_type="RETRIEVAL_QUERY")

    raw_results = []
    try:
        if VECTOR_INDEX_ENDPOINT:
            try:
                endpoint = aiplatform.MatchingEngineIndexEndpoint(index_endpoint_name=VECTOR_INDEX_ENDPOINT)
                deployed_id = VECTOR_DEPLOYED_INDEX_ID or VECTOR_INDEX_NAME.split("/")[-1]
                response = endpoint.find_neighbors(
                    deployed_index_id=deployed_id,
                    queries=[embedding],
                    num_neighbors=min(top_k * 3, 50),
                )
                for neighbor in (response[0] if response else []):
                    raw_results.append({
                        "id": neighbor.id,
                        "score": getattr(neighbor, "distance", None) or getattr(neighbor, "similarity", None),
                        "metadata": {}
                    })
            except Exception as ep_err:
                logger.warning(f"Endpoint query failed (may not be deployed yet): {ep_err}. Falling back to direct index.")
                # fallthrough to direct
                index = aiplatform.MatchingEngineIndex(index_name=VECTOR_INDEX_NAME)
                response = index.find_neighbors(queries=[embedding], num_neighbors=min(top_k * 3, 50))
                for neighbor in (response[0] if response else []):
                    raw_results.append({
                        "id": neighbor.id,
                        "score": getattr(neighbor, "distance", None) or getattr(neighbor, "similarity", None),
                        "metadata": {}
                    })
        else:
            index = aiplatform.MatchingEngineIndex(index_name=VECTOR_INDEX_NAME)
            response = index.find_neighbors(queries=[embedding], num_neighbors=min(top_k * 3, 50))
            for neighbor in (response[0] if response else []):
                raw_results.append({
                    "id": neighbor.id,
                    "score": getattr(neighbor, "distance", None) or getattr(neighbor, "similarity", None),
                    "metadata": {}
                })
    except Exception as e:
        logger.warning(f"Vector query error (endpoint may still be provisioning): {e}")
        return []

    # Client-side filter using filters dict (e.g. {"label": "Expert Mailing List"})
    if filters:
        def matches(r):
            # We don't get metadata back reliably from find_neighbors in all setups.
            # Rely on chunk id prefix + explicit filter pass-through where possible.
            # For phase 1 we accept and let caller filter further using full fetch.
            return True
        # Future: when metadata returned or sidecar, apply here.
        filtered = [r for r in raw_results if matches(r)]
        raw_results = filtered

    # Limit and normalize
    results = raw_results[:top_k]
    return results


def _reciprocal_rank_fusion(
    result_lists: List[List[Dict[str, Any]]], k: int = 60
) -> List[Dict[str, Any]]:
    """Simple Reciprocal Rank Fusion for combining vector and keyword results."""
    scores: Dict[str, float] = {}
    id_to_item: Dict[str, Dict[str, Any]] = {}

    for results in result_lists:
        for rank, item in enumerate(results, 1):
            # Use a stable key: prefer thread/message id
            key = item.get("id") or item.get("thread_id") or str(item)
            if key not in id_to_item:
                id_to_item[key] = item
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)

    # Sort by fused score desc
    sorted_keys = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
    fused = []
    for key in sorted_keys:
        item = dict(id_to_item[key])
        item["fused_score"] = scores[key]
        fused.append(item)
    return fused


def _boost_trusted_authors(items: List[Dict[str, Any]], boost: float = 0.25) -> List[Dict[str, Any]]:
    """Boost items that mention or are authored by trusted experts."""
    boosted = []
    for item in items:
        score = item.get("fused_score") or item.get("score") or 0.0
        author = ""
        data = item.get("data") or item
        if isinstance(data, dict):
            auth = data.get("author") or {}
            if isinstance(auth, dict):
                author = auth.get("name", "") or auth.get("email", "")
            else:
                author = str(auth)
        if any(ta.lower() in author.lower() for ta in TRUSTED_AUTHORS):
            score += boost
        new_item = dict(item)
        new_item["score"] = score
        boosted.append(new_item)
    # Re-sort after boost
    boosted.sort(key=lambda x: x.get("score", 0), reverse=True)
    return boosted


def _boost_trusted_authors_in_threads(threads: List[Dict[str, Any]], boost: float = 0.3) -> List[Dict[str, Any]]:
    """Boost threads in get_expert_guidance that contain posts from trusted authors."""
    for t in threads:
        messages = t.get("messages", []) or []
        has_trusted = False
        for msg in messages:
            auth = msg.get("author") or {}
            name = ""
            if isinstance(auth, dict):
                name = auth.get("name", "") or auth.get("email", "")
            else:
                name = str(auth)
            if any(ta.lower() in name.lower() for ta in TRUSTED_AUTHORS):
                has_trusted = True
                break
        if has_trusted:
            current = t.get("score", 0) or 0
            t["score"] = current + boost
            t["boosted_for_trusted_author"] = True
    # Sort by score if present, otherwise leave order (vector first is still good)
    if any("score" in t for t in threads):
        threads.sort(key=lambda x: x.get("score", 0), reverse=True)
    return threads


def _trigger_ingest_impl(manual: bool = True, days_back: int = 7, label: Optional[str] = None, max_messages: int = 50, before: Optional[str] = None, after: Optional[str] = None, incremental: bool = False) -> Dict[str, Any]:
    """Core ingest logic. Fetches recent content, chunks (preserving table-ish areas), embeds, upserts.
    Supports exact date ranges via after=YYYY/MM/DD and before=YYYY/MM/DD (ideal for year-by-year bulk: 2026, then 2025, ...).
    Stores persistent watermark (last_covered_date) in Firestore for "run all new data since last successful run".
    Use incremental=True (or after not specified) to automatically start after the last successful covered date.
    """
    summary = {"emails_indexed": 0, "drive_files_indexed": 0, "chunks": 0, "errors": [], "range": {}}
    try:
        from datetime import timedelta

        # Determine after_date
        after_date = after
        if not after_date:
            if incremental:
                # Load last covered date from Firestore
                try:
                    rec = token_doc().get()
                    rec_data = rec.to_dict() if rec.exists else {}
                    last = rec_data.get("vector_last_covered_date")
                    if last:
                        after_date = last
                        logger.info(f"Incremental mode: using last_covered_date={after_date}")
                except Exception:
                    pass
            elif days_back and days_back > 0 and not before:
                after_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y/%m/%d")

        # Track the range we are targeting for watermark updates
        target_after = after_date
        target_before = before
        summary["range"] = {"after": target_after, "before": target_before, "incremental": incremental}

        # Gmail side - get service ONCE per label batch to minimize auth churn
        labels_to_process = [label] if label and label in ALLOWED_LABELS else ALLOWED_LABELS
        for lbl in labels_to_process:
            try:
                service, owner = get_gmail_service()

                # Proactive refresh once per batch start. Reduces "Refreshing credentials due to 401" noise
                # and ensures a fresh access token for the duration of the (potentially long) paged ingest.
                try:
                    data = get_owner_record()
                    rtoken = data.get("refresh_token")
                    if rtoken:
                        rcreds = Credentials(
                            token=data.get("access_token"),
                            refresh_token=rtoken,
                            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
                            client_id=data.get("client_id", GOOGLE_CLIENT_ID),
                            client_secret=data.get("client_secret", GOOGLE_CLIENT_SECRET),
                            scopes=data.get("scopes", SCOPES),
                        )
                        rcreds.refresh(GoogleRequest())
                        token_doc().set({"access_token": rcreds.token, "updated_at": now_iso()}, merge=True)
                        service = build("gmail", "v1", credentials=rcreds, cache_discovery=False)
                        logger.info("Proactively refreshed Gmail token at start of ingest batch (fewer 401s expected)")
                except Exception as _re:
                    logger.debug(f"Proactive refresh skipped or partial: {_re}")

                # Resolve label id
                labels_result = service.users().labels().list(userId="me").execute()
                label_id = None
                for l in labels_result.get("labels", []):
                    if l.get("name") == lbl:
                        label_id = l["id"]
                        break
                if not label_id:
                    summary["errors"].append(f"label id not found for {lbl}")
                    continue

                # Paged walk to reach up to max_messages, using before/after for slices
                page_token = None
                processed = 0
                seen = set()
                while processed < max_messages:
                    list_kwargs = {
                        "userId": "me",
                        "labelIds": [label_id],
                        "maxResults": min(200, max(50, max_messages)),
                    }
                    q_parts = []
                    if after_date:
                        q_parts.append(f"after:{after_date}")
                    if before:
                        q_parts.append(f"before:{before}")
                    if q_parts:
                        list_kwargs["q"] = " ".join(q_parts)
                    if page_token:
                        list_kwargs["pageToken"] = page_token

                    res = service.users().messages().list(**list_kwargs).execute()
                    page_msgs = res.get("messages", []) or []
                    page_token = res.get("nextPageToken")

                    for m in page_msgs:
                        mid = m.get("id")
                        if not mid or mid in seen:
                            continue
                        if processed >= max_messages:
                            break
                        seen.add(mid)
                        try:
                            full = get_full_message(service, mid, max_body_chars=0)
                            body_text = full.get("body_text", "") or full.get("snippet", "")
                            if not body_text or len(body_text) < 80:
                                processed += 1
                                continue

                            author = (full.get("author") or {}).get("name", "")
                            date = full.get("date", "")
                            subject = full.get("subject", "")
                            base_meta = {
                                "label": lbl,
                                "source_type": "email",
                                "message_id": mid,
                                "thread_id": full.get("threadId", ""),
                                "author": author,
                                "date": date,
                                "subject": subject,
                            }

                            # Inject author + context into the text so embeddings capture quality signals
                            # (e.g. "Author: John Titus" makes high-value content more distinctive)
                            context_prefix = f"Author: {author}\nDate: {date}\nSubject: {subject}"
                            if author in TRUSTED_AUTHORS:
                                context_prefix = f"Author: {author} (trusted expert)\nDate: {date}\nSubject: {subject}"

                            chunks = _chunk_text(body_text, base_meta, context_prefix=context_prefix)
                            for ch in chunks:
                                emb = _get_embedding(
                                    ch["text"],
                                    task_type="RETRIEVAL_DOCUMENT",
                                    title=subject or author,
                                )
                                th = full.get("threadId", "") or "t"
                                cid = f"email_{th}_{mid}_{ch['meta'].get('chunk_index', 0)}"
                                _upsert_chunks([{"id": cid, "embedding": emb, "metadata": ch["meta"]}])
                                summary["chunks"] += 1
                            summary["emails_indexed"] += 1
                            processed += 1
                        except Exception as e:
                            summary["errors"].append(str(e)[:100])

                    if not page_token:
                        break
            except Exception as e:
                summary["errors"].append(f"search label {lbl}: {str(e)[:80]}")
                continue

        # Drive input side - now traverses category subfolders (e.g. by model or equipment type)
        if DRIVE_INPUT_FOLDER_ID:
            try:
                top_resp = list_drive_files(query="", max_results=50, folder_id=DRIVE_INPUT_FOLDER_ID)
                top_items = top_resp.get("files", []) if isinstance(top_resp, dict) else []
            except Exception as e:
                top_items = []
                summary["errors"].append(f"drive top list: {str(e)[:80]}")

            drive_files_to_process = []
            for item in top_items:
                if not isinstance(item, dict):
                    continue
                if item.get("mimeType", "").startswith("application/vnd.google-apps.folder"):
                    # Recurse into model subfolder
                    try:
                        sub_resp = list_drive_files(query="", max_results=50, folder_id=item["id"])
                        for sf in sub_resp.get("files", []):
                            if not sf.get("mimeType", "").startswith("application/vnd.google-apps.folder"):
                                sf["_parent_folder"] = item.get("name", "")
                                drive_files_to_process.append(sf)
                    except Exception as e:
                        summary["errors"].append(f"drive subfolder {item.get('name')}: {str(e)[:60]}")
                else:
                    drive_files_to_process.append(item)

            for f in drive_files_to_process:
                if not isinstance(f, dict):
                    continue
                try:
                    file_data = get_drive_file(f["id"])
                    text = file_data.get("text_content") or ""
                    if not text or len(text) < 120:
                        continue
                    fname = f.get("name", "")
                    model_folder = f.get("_parent_folder", "")
                    base_meta = {
                        "source_type": "drive",
                        "file_id": f["id"],
                        "name": fname,
                        "mime": f.get("mimeType", ""),
                        "modified": f.get("modifiedTime", ""),
                        "model_folder": model_folder,
                    }
                    context_prefix = f"Source: Drive document\nFile: {fname}\nFolder: {model_folder}"
                    chunks = _chunk_text(text, base_meta, context_prefix=context_prefix)
                    for ch in chunks:
                        emb = _get_embedding(
                            ch["text"],
                            task_type="RETRIEVAL_DOCUMENT",
                            title=fname,
                        )
                        cid = f"drive_{f['id']}_{ch['meta'].get('chunk_index', 0)}"
                        _upsert_chunks([{"id": cid, "embedding": emb, "metadata": ch["meta"]}])
                        summary["chunks"] += 1
                    summary["drive_files_indexed"] += 1
                except Exception as e:
                    summary["errors"].append(str(e)[:100])

        # Record last sync + watermark for incremental "since last successful run"
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y/%m/%d")
            update = {
                "vector_last_ingest": now_iso(),
                "vector_ingest_summary": summary,
                "vector_last_range": {"after": target_after, "before": target_before},
            }
            # Advance the covered date if this run touched "recent" data (no restrictive before or the before is in the future)
            # This enables reliable "run all new data since last successful run".
            if not target_before or target_before >= now_str:
                update["vector_last_covered_date"] = now_str
            token_doc().set(update, merge=True)
        except Exception:
            pass

        logger.info(f"Ingest summary: {summary}")
        return summary
    except Exception as e:
        logger.exception("Ingest failed")
        summary["errors"].append(str(e)[:200])
        return summary


def _do_ingest(days_back: int, label: Optional[str], max_messages: int, before: Optional[str] = None, after: Optional[str] = None, incremental: bool = False):
    """Background task to perform the ingest so the HTTP request returns quickly."""
    logger.info(f"Starting background ingest: days_back={days_back}, label={label}, max_messages={max_messages}, before={before}, after={after}, incremental={incremental}")
    try:
        result = _trigger_ingest_impl(manual=True, days_back=days_back, label=label, max_messages=max_messages, before=before, after=after, incremental=incremental)
        logger.info(f"Background ingest completed: {result}")
    except Exception as e:
        logger.exception(f"Background ingest failed: {e}")


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
        "labels": msg_data.get("labelIds", []),  # raw label IDs; enrichment happens in search_mailing_list when needed
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
            resp = client.get(url, headers={"User-Agent": "KnowledgeForge/1.0"})
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
    Now starts with semantic/vector search (Vertex AI) over the indexed archive for better recall of relevant
    chunks from emails + Drive docs, then falls back/enriches with keyword search + full thread/file fetch.
    """
    service, owner = get_gmail_service()

    all_threads: List[Dict[str, Any]] = []
    seen_thread_ids = set()

    # Start with semantic/vector search (Vertex AI) for better relevance
    # Use expanded query for stronger recall on technical topics
    expanded_topic = _expand_query_for_vector(topic)
    try:
        vec_results = _semantic_search_impl(query=expanded_topic, top_k=max(5, max_threads))
        for vr in vec_results:
            rid = vr.get("id", "")
            # Parse source from chunk id (email_<msg>_<n> or drive_...)
            meta = vr.get("metadata") or {}
            tid = meta.get("thread_id") or meta.get("message_id")
            if not tid:
                if rid.startswith("email_"):
                    # New format: email_{thread}_{msg}_{chunk} or old: email_{msg}_{chunk}
                    parts = rid.split("_")
                    if len(parts) >= 4 and parts[0] == "email":
                        # email_thread_msg_chunk
                        tid = parts[1]
                    else:
                        # strip trailing _chunk
                        base = rid.split("_", 2)[1] if "_" in rid else rid.replace("email_", "")
                        tid = base.split("_")[0] if "_" in base else base
            # If we only have a message_id from the chunk (common for vector results since metadata not returned),
            # resolve it to the real threadId so get_thread works.
            if rid.startswith("email_") and tid:
                try:
                    msg_dict = get_full_message(service, tid, max_body_chars=0)
                    resolved = msg_dict.get("threadId") or msg_dict.get("thread_id")
                    if resolved:
                        tid = resolved
                except Exception as ex:
                    logger.debug(f"Vector result: could not resolve threadId for msg {tid}: {ex}")
            if tid and tid not in seen_thread_ids:
                seen_thread_ids.add(tid)
                try:
                    # Use thread fetch for emails; for drive chunks we still use full get_drive_file later if needed
                    if rid.startswith("email_") or meta.get("source_type") == "email":
                        thread = get_thread(service, tid, max_messages=10, max_body_chars=0)
                        if thread:
                            all_threads.append({
                                "thread_id": tid,
                                "messages": thread,
                                "search_query_used": "vector_semantic",
                                "source": "vector",
                                "score": vr.get("score"),
                            })
                except Exception as e:
                    logger.warning(f"Failed to fetch from vector result {tid}: {e}")
            if len(all_threads) >= max_threads:
                break
    except Exception as e:
        logger.warning(f"Vector search failed or not configured, falling back to keyword: {e}")

    # Fallback / enrich with keyword search
    if len(all_threads) < max_threads:
        queries = [
            topic,
            f'"{topic}" (howto OR "how to" OR guide OR overhaul OR rebuild OR tips OR "step by step")',
            f'{topic} (problem OR issue OR fix OR solution)',
        ]
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
                                    "source": "keyword",
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

    # Compute provenance summary
    vector_count = sum(1 for t in unique_threads if t.get("source") == "vector")
    keyword_count = sum(1 for t in unique_threads if t.get("source") == "keyword")

    # Boost threads that contain contributions from trusted high-quality authors
    unique_threads = _boost_trusted_authors_in_threads(unique_threads)

    return {
        "topic": topic,
        "label": label or "all allowed labels",
        "mailbox_owner": owner.get("email", ""),
        "threads_found": len(unique_threads),
        "vector_threads": vector_count,
        "keyword_threads": keyword_count,
        "threads": unique_threads,
        "note": "Each thread contains full messages with author names, dates, bodies, links and attachments. Use this to build accurate how-to guides with proper attribution to list experts. 'source' field indicates whether the thread was primarily surfaced via vector search or keyword search.",
        "provenance_summary": {
            "vector": vector_count,
            "keyword": keyword_count,
            "total": len(unique_threads)
        }
    }


# -----------------------------------------------------------------------------
# Google Drive tools (input folder for PDFs/sources, output folder for generated howtos)
# -----------------------------------------------------------------------------

def _get_drive_file_content(service, file_id: str) -> Dict[str, Any]:
    """Download a Drive file. For PDFs use pypdf text extraction (like Gmail attachments)."""
    meta = service.files().get(fileId=file_id, fields="id,name,mimeType,size").execute()
    mime = meta.get("mimeType", "")
    filename = meta.get("name", file_id)

    if mime == "application/vnd.google-apps.document":
        # Export Google Doc as plain text
        content = service.files().export(fileId=file_id, mimeType="text/plain").execute()
        text = content.decode("utf-8", errors="replace") if isinstance(content, (bytes, bytearray)) else content
        return {
            "file_id": file_id,
            "filename": filename,
            "mimeType": mime,
            "text_content": text,
            "is_google_doc": True,
        }

    # Binary file (PDF, etc.)
    request = service.files().get_media(fileId=file_id)
    from io import BytesIO
    from googleapiclient.http import MediaIoBaseDownload

    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()

    data = fh.getvalue()

    text = None
    if mime == "application/pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            text = text.strip() if text else None
        except Exception as e:
            logger.warning(f"Drive PDF extraction failed for {filename}: {e}")

    return {
        "file_id": file_id,
        "filename": filename,
        "mimeType": mime,
        "size": len(data),
        "text_content": text,
        "is_pdf": mime == "application/pdf",
        "note": "For images or complex files use vision on the client or additional processing." if mime.startswith("image/") else None,
    }


@mcp.tool()
def list_drive_files(query: str = "", max_results: int = 20, folder_id: Optional[str] = None) -> Dict[str, Any]:
    """List files in Drive. Defaults to the configured INPUT folder if no folder_id provided.
    Use this to discover supplemental PDFs, manuals, and sources for howtos.
    """
    service = get_drive_service()
    effective_folder = folder_id or DRIVE_INPUT_FOLDER_ID or "root"

    q_parts = []
    if query:
        q_parts.append(query)
    if effective_folder != "root":
        q_parts.append(f"'{effective_folder}' in parents")
    q = " and ".join(q_parts) if q_parts else ""

    results = service.files().list(
        q=q,
        pageSize=min(max_results, 50),
        fields="files(id, name, mimeType, size, modifiedTime)",
        orderBy="modifiedTime desc",
    ).execute()

    files = results.get("files", [])
    return {
        "folder_id": effective_folder,
        "query": query,
        "count": len(files),
        "files": files,
        "note": "Use get_drive_file(file_id) to retrieve full text (PDF extraction supported).",
    }


@mcp.tool()
def get_drive_file(file_id: str) -> Dict[str, Any]:
    """Retrieve content from a Drive file (PDF text extraction supported, Google Docs exported as text).
    Ideal for pulling supplemental specs, diagrams descriptions, and reference material from your input folder.
    """
    service = get_drive_service()
    return _get_drive_file_content(service, file_id)


@mcp.tool()
def save_howto_to_drive(title: str, content: str, as_pdf: bool = True, folder_id: Optional[str] = None) -> Dict[str, Any]:
    """Save a generated howto to the configured OUTPUT Drive folder.
    content can be Markdown or plain text. If as_pdf=True a basic PDF is created using reportlab.
    Filenames are automatically timestamped (e.g. Title_2026-06-17_12-34-56.pdf) to avoid duplicates in the output folder.
    Returns the created file metadata.
    """
    service = get_drive_service()
    target_folder = folder_id or DRIVE_OUTPUT_FOLDER_ID
    if not target_folder:
        raise HTTPException(400, "No output folder configured (DRIVE_OUTPUT_FOLDER_ID env) and none provided.")

    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import inch
    from io import BytesIO
    import textwrap
    from googleapiclient.http import MediaIoBaseUpload
    from datetime import datetime

    filename_base = title.replace(" ", "_").replace("/", "-")[:80]
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename_base = f"{filename_base}_{timestamp}"

    if as_pdf:
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
        from reportlab.lib.units import inch
        from reportlab.lib.enums import TA_LEFT, TA_CENTER
        from io import BytesIO

        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=0.6*inch,
            leftMargin=0.6*inch,
            topMargin=0.6*inch,
            bottomMargin=0.6*inch
        )

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=16, spaceAfter=12, alignment=TA_CENTER)
        heading_style = ParagraphStyle('CustomHeading', parent=styles['Heading2'], fontSize=12, spaceBefore=10, spaceAfter=6)
        body_style = ParagraphStyle('CustomBody', parent=styles['Normal'], fontSize=9, leading=11, spaceAfter=4)
        notes_style = ParagraphStyle('NotesStyle', parent=styles['Normal'], fontSize=8, leading=10, textColor=colors.gray, spaceBefore=6)

        story = []
        story.append(Paragraph(title, title_style))
        story.append(Spacer(1, 8))

        # Split for separate references block
        main_content = content
        references_block = ""
        for marker in ["**GENERATION NOTES", "GENERATION NOTES - MCP / SKILL"]:
            if marker in content:
                parts = content.split(marker, 1)
                main_content = parts[0].strip()
                references_block = (marker + parts[1]) if len(parts) > 1 else ""
                break

        # Parse content into flowables (headings + tables + paragraphs)
        lines = main_content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            if line.startswith('### '):
                story.append(Paragraph(line[4:], heading_style))
            elif line.startswith('## '):
                story.append(Paragraph(line[3:], heading_style))
            elif line.startswith('# '):
                story.append(Paragraph(line[2:], title_style))
            elif line.startswith('|') and '|' in line[1:]:
                table_lines = [line]
                i += 1
                while i < len(lines) and lines[i].strip().startswith('|'):
                    table_lines.append(lines[i].strip())
                    i += 1
                table_data = []
                for tl in table_lines:
                    if '---' in tl: continue
                    cells = [c.strip() for c in tl.split('|')[1:-1]]
                    table_data.append(cells)
                if table_data:
                    t = Table(table_data)
                    t.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, -1), 8),
                        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ('TOPPADDING', (0, 0), (-1, -1), 3),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                    ]))
                    story.append(Spacer(1, 6))
                    story.append(t)
                    story.append(Spacer(1, 6))
                continue
            else:
                safe = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(safe, body_style))
            i += 1

        if references_block:
            story.append(PageBreak())
            story.append(Paragraph("GENERATION NOTES (MCP / Skills)", heading_style))
            story.append(Paragraph("(This block can be removed for public distribution)", notes_style))
            story.append(Spacer(1, 6))
            for line in references_block.splitlines():
                if line.strip():
                    safe = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    story.append(Paragraph(safe, notes_style))

        doc.build(story)
        buffer.seek(0)

        file_metadata = {
            "name": f"{filename_base}.pdf",
            "parents": [target_folder] if target_folder else None,
            "mimeType": "application/pdf",
        }
        media = MediaIoBaseUpload(buffer, mimetype="application/pdf", resumable=True)
    else:
        file_metadata = {
            "name": f"{filename_base}.md",
            "parents": [target_folder] if target_folder else None,
            "mimeType": "text/markdown",
        }
        media = MediaIoBaseUpload(BytesIO(content.encode("utf-8")), mimetype="text/markdown", resumable=True)

    created = service.files().create(body=file_metadata, media_body=media, fields="id,name,webViewLink").execute()

    return {
        "file_id": created.get("id"),
        "name": created.get("name"),
        "webViewLink": created.get("webViewLink"),
        "folder": target_folder,
        "as_pdf": as_pdf,
        "note": "File saved to your Drive output folder. The expert can now reference it.",
    }


@mcp.tool()
def list_input_manuals(model: Optional[str] = None, max_results: int = 20) -> Dict[str, Any]:
    """List documents and manuals from the Drive input folder (technical manuals).

    The input folder is organized with subfolders sorted by category
    (e.g. Engine, Chassis, etc.).

    - Call without 'model' to see top-level contents and available subfolders.
    - Provide a category name (e.g. "Engine") to list files inside that subfolder.

    Perfect for discovering PDFs, workshop manuals, torque specs, and diagrams
    to supplement the mailing list data.
    """
    base_folder = DRIVE_INPUT_FOLDER_ID
    target_folder = base_folder

    if model:
        service = get_drive_service()
        q = (
            f"name contains '{model}' "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and '{base_folder}' in parents"
        )
        res = service.files().list(
            q=q,
            fields="files(id, name)",
            pageSize=5
        ).execute()
        folders = res.get("files", [])
        if not folders:
            # Fallback: list top level so user can see available models
            top = list_drive_files(query="", max_results=10, folder_id=base_folder)
            top["error"] = f"No subfolder matching model '{model}'"
            top["note"] = "Subfolders are categories (e.g. by model or equipment type). List without model to see them."
            return top
        target_folder = folders[0]["id"]

    result = list_drive_files(query="", max_results=max_results, folder_id=target_folder)
    result["input_folder"] = base_folder
    if model:
        result["model"] = model
    result["note"] = (
        "Input folder subfolders are sorted by category (e.g. Engine, Chassis). "
        "Use get_drive_file(file_id) to extract text from PDFs."
    )
    return result


@mcp.tool()
def save_to_guides(title: str, content: str, as_pdf: bool = True) -> Dict[str, Any]:
    """Save a generated howto directly to the output Drive folder (Generated guides).

    Filenames are automatically timestamped to prevent duplicate files with the same name.
    This is the recommended convenience tool for the expert when publishing
    a finished professional document.
    """
    return save_howto_to_drive(
        title=title,
        content=content,
        as_pdf=as_pdf,
        folder_id=DRIVE_OUTPUT_FOLDER_ID
    )


@mcp.tool()
def semantic_search(query: str, top_k: int = 8, label: Optional[str] = None) -> List[Dict[str, Any]]:
    """Semantic (vector) search over the indexed email and Drive content.
    Uses Vertex AI Vector Search (first phase). Returns top matching chunk ids + scores.
    Chunk ids are self-describing (email_<id> or drive_<id>). Pair with get_thread / get_drive_file
    to retrieve full attributed text, tables, and images.
    """
    filters = {"label": label} if label else None
    return _semantic_search_impl(query=query, top_k=top_k, filters=filters)


@mcp.tool()
def hybrid_search(query: str, top_k: int = 8, label: Optional[str] = None) -> Dict[str, Any]:
    """Hybrid retrieval: vector semantic results + keyword search results.
    Uses Reciprocal Rank Fusion + trusted author boosting for better relevance.
    """
    vec = []
    kw = []
    try:
        vec = _semantic_search_impl(query=query, top_k=max(4, top_k // 2))
    except Exception as e:
        logger.warning(f"hybrid vec part failed: {e}")

    try:
        kw = search_mailing_list(query=query, label=label, max_results=max(6, top_k))
    except Exception as e:
        logger.warning(f"hybrid kw part failed: {e}")

    # Prepare lists for RRF
    vec_for_fusion = [{"id": v.get("id"), "score": v.get("score", 0)} for v in vec]
    kw_for_fusion = []
    for item in (kw or []):
        if isinstance(item, dict):
            mid = item.get("id") or item.get("threadId")
            kw_for_fusion.append({"id": mid, "data": item})

    fused = _reciprocal_rank_fusion([vec_for_fusion, kw_for_fusion])

    # Convert back and add type/source
    combined = []
    for item in fused:
        if "data" in item:
            combined.append({
                "type": "keyword",
                "id": item["id"],
                "data": item.get("data"),
                "score": item.get("fused_score", 0),
                "source": "keyword",
            })
        else:
            combined.append({
                "type": "vector",
                "id": item["id"],
                "score": item.get("fused_score", 0),
                "source": "semantic",
            })

    # Boost trusted authors (John Titus etc.)
    combined = _boost_trusted_authors(combined)

    return {
        "query": query,
        "label": label,
        "vector_hits": len(vec),
        "keyword_hits": len([c for c in combined if c["type"] == "keyword"]),
        "combined": combined[:top_k],
        "note": "Use the ids to fetch full content via get_thread/get_message/get_drive_file. Trusted authors are boosted."
    }


@mcp.tool()
def trigger_ingest(manual: bool = True, days_back: int = 7, label: Optional[str] = None, max_messages: int = 50, before: Optional[str] = None, after: Optional[str] = None, incremental: bool = False) -> Dict[str, Any]:
    """Trigger manual or incremental indexing into the Vertex AI vector index.
    Supports exact date ranges with after=YYYY/MM/DD and before=YYYY/MM/DD (perfect for year-by-year: after=2026/01/01 before=2027/01/01).
    Use incremental=True to automatically process only data newer than the last successful covered date (stored in Firestore).
    Returns summary with emails_indexed so you can verify success and decide when a year slice is "done".
    """
    return _trigger_ingest_impl(manual=manual, days_back=days_back, label=label, max_messages=max_messages, before=before, after=after, incremental=incremental)


@mcp.tool()
def get_ingest_status(label: Optional[str] = None) -> Dict[str, Any]:
    """Returns the last ingest watermark, covered date, and summary.
    Use this after a year slice (or incremental run) to verify success (emails_indexed, errors, range processed).
    The vector_last_covered_date is what incremental=True will use as the starting 'after' for new data.
    """
    try:
        rec = token_doc().get()
        data = rec.to_dict() if rec.exists else {}
        status = {
            "vector_last_ingest": data.get("vector_last_ingest"),
            "vector_last_covered_date": data.get("vector_last_covered_date"),
            "last_range": data.get("vector_last_range"),
            "last_summary": data.get("vector_ingest_summary"),
        }
        # Also surface current index size if possible (best effort)
        try:
            import google.cloud.aiplatform as aiplatform
            idx = aiplatform.MatchingEngineIndex(index_name=VECTOR_INDEX_NAME)
            status["index_vectors_count"] = getattr(getattr(idx, "index_stats", None), "vectors_count", None)
        except Exception:
            pass
        return status
    except Exception as e:
        return {"error": str(e)}


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
app = FastAPI(title="KnowledgeForge", version="0.2.0")

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
    return {"status": "healthy", "service": "knowledge-forge", "allowed_labels": ALLOWED_LABELS}


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


@app.post("/ingest")
def trigger_ingest_endpoint(request: Request, background_tasks: BackgroundTasks, days_back: int = 30, label: Optional[str] = None, max_messages: int = 50, before: Optional[str] = None, after: Optional[str] = None, incremental: bool = False):
    """Manual trigger for vector indexing (year-by-year bulk or incremental).
    Protected by require_agent.
    Use after=2026/01/01&before=2027/01/01 to process a full year (repeat with high max_messages until emails_indexed drops to near zero for that slice).
    Then move to previous year (2025, 2024, ...).
    Use incremental=true (after last bulk done) to only ingest new mail since the last successful run (watermark stored automatically).
    The call returns immediately; check logs or get_ingest_status() for the summary to verify success.
    """
    require_agent(request)
    if not VERTEX_PROJECT or not VECTOR_INDEX_NAME:
        return {"error": "Vertex not configured. Set VERTEX_PROJECT and VECTOR_INDEX_NAME env."}
    background_tasks.add_task(_do_ingest, days_back, label, max_messages, before, after, incremental)
    return {
        "status": "ingest started in background",
        "days_back": days_back,
        "label": label,
        "max_messages": max_messages,
        "after": after,
        "before": before,
        "incremental": incremental
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
        <h1>🚀 KnowledgeForge</h1>
        <p><strong>Status:</strong> ✅ Running (FastMCP + Firestore)</p>
        <div style="background:#e3f2fd;padding:15px;border-radius:6px;margin:20px 0">
          <h3>📧 Allowed Gmail Labels</h3>
          {labels}
        </div>
        <p><a href="/oauth/google/start">🔐 Connect / Reconnect Gmail (requires agent token)</a></p>
        <p><a href="/oauth/google/drive/start">📁 Connect / Reconnect Google Drive (input PDFs + output howtos)</a></p>
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
    # We also allow the AGENT_BEARER_TOKEN for convenience when using curl/scripts.
    require_agent(request)

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


# --- Google Drive OAuth flow (separate consent for Drive access) ---
def _drive_redirect_uri():
    base = REDIRECT_URI.rstrip("/")
    if base.endswith("/oauth/google/callback"):
        base = base.replace("/oauth/google/callback", "")
    return f"{base}/oauth/google/drive/callback"


@app.get("/oauth/google/drive/start")
def oauth_google_drive_start(request: Request):
    """Start Drive OAuth flow. Requires agent bearer or IAM for owner."""
    require_agent(request)

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(500, "Google OAuth credentials not configured")

    # Request Drive scopes + existing Gmail scope to encourage Google to issue a refresh_token
    # on incremental consent.
    drive_auth_scopes = list(set(DRIVE_SCOPES + ["https://www.googleapis.com/auth/gmail.readonly"]))
    flow = Flow.from_client_config(_oauth_client_config(), scopes=drive_auth_scopes)
    flow.redirect_uri = _drive_redirect_uri()

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    oauth_states[state] = {"flow": flow, "created": now_iso(), "type": "drive"}
    logger.info(f"🔐 Drive OAuth flow started, state={state}")

    return RedirectResponse(auth_url)


@app.get("/oauth/google/drive/callback", response_class=HTMLResponse)
def oauth_google_drive_callback(request: Request):
    query = request.query_params
    state = query.get("state")
    code = query.get("code")
    error = query.get("error")

    if error:
        return HTMLResponse(f"<h1>Drive OAuth Error</h1><p>{error}</p>", status_code=400)

    if not state or state not in oauth_states:
        return HTMLResponse("<h1>Invalid state</h1>", status_code=400)

    flow_info = oauth_states[state]
    flow = flow_info.get("flow")
    if not flow or flow_info.get("type") != "drive":
        return HTMLResponse("<h1>Drive Flow not found</h1>", status_code=400)

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
    except Exception as fetch_err:
        if "Scope has changed" in str(fetch_err):
            # Incremental authorization case: Google returned a superset of scopes
            # (Gmail scopes were previously granted for the same client).
            # Extract the token manually from the OAuth2 session.
            logger.warning("Handling incremental Drive auth with extra granted scopes")
            token = flow.oauth2session.token
            from google.oauth2.credentials import Credentials as GoogleCredentials
            scopes = token.get("scope")
            if isinstance(scopes, str):
                scopes = scopes.split()
            creds = GoogleCredentials(
                token=token.get("access_token"),
                refresh_token=token.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=GOOGLE_CLIENT_ID,
                client_secret=GOOGLE_CLIENT_SECRET,
                scopes=scopes or DRIVE_SCOPES,
            )
        else:
            logger.exception("Drive OAuth callback error")
            return HTMLResponse(f"<h1>Drive OAuth Callback Error</h1><p>{fetch_err}</p>", status_code=500)

    try:
        save_drive_tokens(creds)
        oauth_states.pop(state, None)

        return f"""
        <html>
          <body style="font-family: system-ui, sans-serif; margin:40px">
            <h1 style="color:#28a745">✅ Google Drive connected successfully</h1>
            <p>The MCP now has access to your configured Drive folders for supplemental PDFs/sources (input) and generated howtos (output).</p>
            <p>Input folder ID: {DRIVE_INPUT_FOLDER_ID or '(not set in env)'}</p>
            <p>Output folder ID: {DRIVE_OUTPUT_FOLDER_ID or '(not set in env)'}</p>
            <p>You can close this window.</p>
          </body>
        </html>
        """
    except Exception as e:
        logger.exception("Drive OAuth callback error during save")
        return HTMLResponse(f"<h1>Drive OAuth Callback Error</h1><p>{e}</p>", status_code=500)


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
            result = f"Hei {name}! KnowledgeForge (MCP server for private archives) is working."

        elif method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "knowledge-forge", "version": "0.2.0"},
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
# Connect Grok to: https://your-knowledgeforge-service.a.run.app/mcp  # (update after deploy)
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
    logger.info(f"🚀 Starting KnowledgeForge on port {port}")
    logger.info(f"Allowed labels: {ALLOWED_LABELS}")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)

if __name__ == "__main__":
    main()
