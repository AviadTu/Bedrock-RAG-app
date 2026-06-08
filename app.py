"""
app.py
──────
Flask application entry point.

Architecture (Phase 2)
  Retrieval and generation are handled entirely by an AWS Bedrock Knowledge
  Base.  Uploaded files go to S3 and a Bedrock ingestion job indexes them;
  chat answers come from a Bedrock Agent (invoke_agent).  There is no local
  embedding / FAISS index, so the server is ready to serve immediately.

Startup sequence
  1. Load .env via config.settings (python-dotenv).
  2. Initialise SQLite schema.
  3. Create the BedrockService and ChatService singletons.
  4. Register all routes.

API endpoints
  GET  /                → serve the chat UI (index.html)
  POST /chat            → handle one conversational turn (Bedrock RAG)
  GET  /history         → return full message history for the session
  POST /clear           → clear the session's message history
  GET  /status          → Knowledge Base / ingestion status
  GET  /documents       → list uploaded documents
  POST /upload          → upload files to S3 + start a Bedrock ingestion job

Session
  The session_id is supplied by the browser via the X-Session-Id header.
  The frontend generates a UUID once per tab and stores it in
  `sessionStorage`, which is automatically cleared when the tab is closed.
  This guarantees that closing the website and reopening it yields a new,
  empty session, while reloading within the same tab preserves memory.
"""

from __future__ import annotations

import threading
import uuid
from urllib.parse import quote

from flask import Flask, jsonify, redirect, render_template, request

from config import settings
from database.models import init_db, list_uploads, record_upload, delete_upload
from services.bedrock_service import BedrockService
from services.chat_service import ChatService
from storage.uploads import UploadError, save_upload

app = Flask(__name__)
app.secret_key = settings.FLASK_SECRET_KEY

# Reject request bodies larger than the configured upload limit before they
# are buffered.  Flask raises 413 (RequestEntityTooLarge) automatically.
app.config["MAX_CONTENT_LENGTH"] = settings.MAX_UPLOAD_MB * 1024 * 1024

# ─────────────────────────────────────────────────────────────────────
# Singletons – created once, shared across all requests
# ─────────────────────────────────────────────────────────────────────

bedrock_service = BedrockService()
chat_service = ChatService(bedrock_service)


# ─────────────────────────────────────────────────────────────────────
# Application startup
# ─────────────────────────────────────────────────────────────────────

with app.app_context():
    init_db()


# ─────────────────────────────────────────────────────────────────────
# Session helper
# ─────────────────────────────────────────────────────────────────────

def _is_valid_uuid(value: str) -> bool:
    """Return True if `value` is a syntactically valid UUID string."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _get_session_id() -> str:
    """
    Return the current browser tab's session ID.

    The frontend keeps the ID in `sessionStorage` (per-tab, cleared when the
    tab is closed) and sends it as the `X-Session-Id` header on every
    request.  If the header is missing or malformed, a fresh isolated UUID
    is generated so the caller still receives a usable ID; this also means
    no stale identifier from a previous browser session can leak into a
    new one.
    """
    sid = (request.headers.get("X-Session-Id") or "").strip()
    if sid and _is_valid_uuid(sid):
        return sid
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # No server-side session creation here – the browser tab generates its
    # own ID in sessionStorage and sends it with subsequent requests.
    return render_template("index.html")


@app.route("/chat", methods=["POST"])
def chat():
    """
    POST /chat
    Request  body: {"message": "…"}
    Response body: {"answer": "…", "context": […]}

    The Knowledge Base is always queryable, so there is no readiness gate.
    """
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()

    if not user_message:
        return jsonify({"error": "Message must not be empty."}), 400

    session_id = _get_session_id()

    try:
        result = chat_service.chat(session_id, user_message)
        return jsonify(result)
    except Exception as exc:
        print(f"[app] /chat error: {exc}", flush=True)
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


@app.route("/history", methods=["GET"])
def history():
    """
    GET /history
    Returns the full message history for the current session.
    """
    session_id = _get_session_id()
    messages = chat_service.get_history(session_id)
    return jsonify({"messages": messages})


@app.route("/clear", methods=["POST"])
def clear():
    """
    POST /clear
    Delete all messages for the current session (starts a fresh chat).
    """
    session_id = _get_session_id()
    chat_service.clear(session_id)
    return jsonify({"ok": True})


@app.route("/status", methods=["GET"])
def status():
    """
    GET /status
    Returns Knowledge Base / ingestion status so the frontend can show
    whether documents are still being processed after an upload.
    """
    return jsonify(chat_service.engine_status)


def _start_ingestion_background() -> None:
    """Start a Bedrock ingestion job in a daemon thread (non-blocking upload)."""
    def _run() -> None:
        try:
            bedrock_service.start_ingestion()
        except Exception as exc:
            try:
                print(f"[app] start ingestion failed: {exc}", flush=True)
            except (OSError, ValueError):
                pass

    threading.Thread(target=_run, daemon=True).start()


def _display_name_for_key(key: str) -> str:
    """
    Best-effort human-readable name for an S3 object key.

    1. Strip the configured S3 prefix.
    2. For backward compatibility, drop any legacy ``<uuid-hex>__`` prefix
       that older uploads may still carry in S3.
    """
    basename = key[len(settings.S3_PREFIX):] if key.startswith(settings.S3_PREFIX) else key
    basename = basename.rsplit("/", 1)[-1]
    prefix, sep, remainder = basename.partition("__")
    if sep and prefix and all(c in "0123456789abcdef" for c in prefix.lower()):
        return remainder
    return basename


def _list_s3_documents() -> list[dict]:
    """
    List every object under S3_PREFIX in the configured bucket.

    The Knowledge Base S3 bucket is the single source of truth, so the
    document panel reflects whatever actually exists in S3 (including
    files placed there directly by an admin via the AWS Console).
    """
    from botocore.exceptions import BotoCoreError, ClientError
    from storage.s3_client import get_s3_client

    docs: list[dict] = []
    paginator = get_s3_client().get_paginator("list_objects_v2")
    try:
        pages = paginator.paginate(
            Bucket=settings.S3_BUCKET, Prefix=settings.S3_PREFIX
        )
        for page in pages:
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key", "")
                # Skip the "folder" marker if present.
                if not key or key.endswith("/"):
                    continue
                docs.append(
                    {
                        "original_filename": _display_name_for_key(key),
                        "s3_key": key,
                        "upload_timestamp": (
                            obj["LastModified"].isoformat()
                            if obj.get("LastModified")
                            else None
                        ),
                    }
                )
    except (BotoCoreError, ClientError) as exc:
        print(f"[app] S3 list failed; falling back to DB registry: {exc}", flush=True)
        return list_uploads()

    # Newest first (matches the previous DB-backed ordering).
    docs.sort(key=lambda d: d.get("upload_timestamp") or "", reverse=True)
    return docs


@app.route("/documents", methods=["GET"])
def documents():
    """
    GET /documents
    Return the live list of documents in the Knowledge Base S3 bucket.
    The bucket is the single source of truth – any file added directly
    in AWS will appear here, and any file deleted in AWS will disappear.
    """
    return jsonify({"documents": _list_s3_documents()})


@app.route("/documents/download", methods=["GET"])
def download_document():
    """
    GET /documents/download?s3_key=<key>
    Issue a short-lived S3 pre-signed URL and redirect the browser to it.

    The bucket remains private; the URL is valid for a few minutes and
    expires automatically.  Only keys under the configured S3_PREFIX may
    be requested, to prevent arbitrary object access.
    """
    s3_key = (request.args.get("s3_key") or "").strip()
    if not s3_key:
        return jsonify({"error": "s3_key is required."}), 400
    if not s3_key.startswith(settings.S3_PREFIX):
        return jsonify({"error": "Invalid s3_key prefix."}), 400

    from botocore.exceptions import BotoCoreError, ClientError
    from storage.s3_client import get_s3_client

    # Force the browser to download (rather than render inline) and
    # preserve the original Hebrew filename via RFC 5987.  HTTP headers
    # are limited to ISO-8859-1, so the ``filename`` parameter holds an
    # ASCII-only fallback for very old clients, and ``filename*`` carries
    # the UTF-8, URL-encoded original name that modern browsers prefer.
    display_name = _display_name_for_key(s3_key)
    ascii_fallback = display_name.encode("ascii", "ignore").decode("ascii") or "document"
    disposition = (
        f'inline; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(display_name, safe='')}"
    )

    try:
        url = get_s3_client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": settings.S3_BUCKET,
                "Key": s3_key,
                "ResponseContentDisposition": disposition,
            },
            ExpiresIn=300,  # 5 minutes
        )
    except (BotoCoreError, ClientError) as exc:
        print(f"[app] presign failed for '{s3_key}': {exc}", flush=True)
        return jsonify({"error": "Could not generate download link."}), 500

    return redirect(url, code=302)


@app.route("/logout")
def logout():
    """
    GET /logout
    Render a simple goodbye page.  The frontend clears its per-tab
    session storage before navigating here.
    """
    return render_template("logout.html")


@app.route("/documents", methods=["DELETE"])
def delete_document():
    """
    DELETE /documents
    Request  body: {"s3_key": "data/<uuid>__filename.pdf"}
    Response body: {"ok": true, "ingesting": true}

    Removes the object from S3, removes its record from the
    uploaded_documents table, then starts a Bedrock ingestion job so the
    Knowledge Base no longer returns content from the deleted document.

    Only keys that begin with the configured S3_PREFIX are accepted to
    prevent arbitrary object deletion.
    """
    data = request.get_json(silent=True) or {}
    s3_key = (data.get("s3_key") or "").strip()

    if not s3_key:
        return jsonify({"error": "s3_key is required."}), 400

    if not s3_key.startswith(settings.S3_PREFIX):
        return jsonify({"error": "Invalid s3_key prefix."}), 400

    from botocore.exceptions import ClientError
    from storage.s3_client import get_s3_client

    try:
        get_s3_client().delete_object(Bucket=settings.S3_BUCKET, Key=s3_key)
    except ClientError as exc:
        print(f"[app] S3 delete failed for '{s3_key}': {exc}", flush=True)
        return jsonify({"error": "Failed to delete from S3."}), 500

    delete_upload(s3_key)
    _start_ingestion_background()

    return jsonify({"ok": True, "ingesting": True})


@app.route("/upload", methods=["POST"])
def upload():
    """
    POST /upload  (multipart/form-data)

    Accepts one or more files under the form field ``files``, streams each
    one directly to S3, records it in the uploaded_documents table, and
    starts a Bedrock Knowledge Base ingestion job so the new content becomes
    searchable.

    Response body:
      {
        "uploaded":  [{"original_filename": …, "s3_key": …}, …],
        "errors":    [{"filename": …, "error": …}, …],
        "ingesting": true|false
      }
    """
    files = request.files.getlist("files")
    if not files:
        # Also accept a single file under the "file" field for convenience.
        single = request.files.get("file")
        if single is not None:
            files = [single]

    if not files:
        return jsonify({"error": "No files provided. Use the 'files' field."}), 400

    uploaded: list[dict] = []
    errors: list[dict] = []

    for file_storage in files:
        filename = file_storage.filename or "(unnamed)"
        try:
            original_filename, s3_key = save_upload(file_storage)
            record_upload(original_filename, s3_key)
            uploaded.append(
                {"original_filename": original_filename, "s3_key": s3_key}
            )
        except UploadError as exc:
            errors.append({"filename": filename, "error": str(exc)})
        except Exception as exc:
            print(f"[app] /upload error for '{filename}': {exc}", flush=True)
            errors.append({"filename": filename, "error": "Upload failed."})

    ingesting = bool(uploaded)
    if ingesting:
        _start_ingestion_background()

    status_code = 200 if uploaded else 400
    return (
        jsonify(
            {"uploaded": uploaded, "errors": errors, "ingesting": ingesting}
        ),
        status_code,
    )


@app.errorhandler(413)
def _too_large(_exc):
    """Friendly JSON for oversized uploads instead of an HTML error page."""
    return (
        jsonify(
            {
                "error": f"File too large. Maximum upload size is "
                f"{settings.MAX_UPLOAD_MB} MB."
            }
        ),
        413,
    )


# ─────────────────────────────────────────────────────────────────────
# Dev-server entry point
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(
        host=settings.FLASK_HOST,
        port=settings.FLASK_PORT,
        debug=settings.FLASK_DEBUG,
    )
