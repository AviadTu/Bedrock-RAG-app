"""
storage/uploads.py
──────────────────
Write-path helper for the upload endpoint.

Responsibilities
  1. Validate the upload's file extension against the configured allow-list.
  2. Generate a collision-free S3 key:  documents/<uuid>__<safe_filename>
  3. Stream the file straight to S3 (no local temp file is written).

The original filename is preserved verbatim by the caller (it is stored in
the uploaded_documents table), so non-ASCII / Hebrew filenames are not lost –
only the S3 *key* uses an ASCII-sanitised variant for portability.
"""

from __future__ import annotations

import uuid

from werkzeug.datastructures import FileStorage

from config import settings


class UploadError(ValueError):
    """Raised when an upload is rejected (e.g. disallowed extension)."""


def _extension(filename: str) -> str:
    """Return the lower-case extension without the leading dot ('' if none)."""
    _, dot, ext = filename.rpartition(".")
    return ext.lower() if dot else ""


def is_allowed(filename: str) -> bool:
    return _extension(filename) in settings.ALLOWED_UPLOAD_EXTENSIONS


def _sanitise_for_s3_key(name: str) -> str:
    """
    Make a filename safe to embed in an S3 object key while preserving its
    visible characters (including Hebrew and other Unicode letters).

    Only path- and control-unsafe characters are removed:
      • forward and back slashes (so the filename never spans S3 "folders")
      • NUL and ASCII control characters (0x00–0x1F and 0x7F)
      • leading / trailing whitespace
    Spaces and Unicode (e.g. Hebrew) are intentionally preserved – S3 supports
    them in object keys and the AWS Console displays them correctly.
    """
    stripped = "".join(
        ch for ch in name
        if ch not in ("/", "\\") and (ord(ch) >= 32 and ord(ch) != 127)
    )
    return stripped.strip()


def build_s3_key(original_filename: str) -> str:
    """
    Build a unique, S3-safe key for an upload.

    Format: ``<prefix><uuid4>__<visible_filename>`` e.g.
    ``data/3f9c…__report.pdf`` or ``data/3f9c…__דוח.pdf``.

    The uuid prefix guarantees uniqueness; the visible portion preserves the
    original filename (Hebrew included) after stripping only path separators
    and control characters.  If sanitisation leaves nothing usable, a
    deterministic ``document_<uuid>`` fallback is used so the key is still
    unique and keeps its extension.

    The original (unmodified) filename is stored separately in the
    uploaded_documents table by the caller, so this sanitisation never
    affects what the UI displays.
    """
    token = uuid.uuid4().hex
    ext = _extension(original_filename)

    safe_name = _sanitise_for_s3_key(original_filename)
    if not safe_name:
        # Pathological input (e.g. name was only slashes/control chars).
        safe_name = f"document_{token}.{ext}" if ext else f"document_{token}"

    return f"{settings.S3_PREFIX}{token}__{safe_name}"


def save_upload(file_storage: FileStorage) -> tuple[str, str]:
    """
    Validate and stream a single uploaded file to S3.

    Parameters
    ----------
    file_storage : the werkzeug FileStorage from ``request.files``

    Returns
    -------
    (original_filename, s3_key)

    Raises
    ------
    UploadError  – if the filename is missing or the extension is not allowed.
    RuntimeError – if the S3 upload fails.
    """
    original_filename = (file_storage.filename or "").strip()
    if not original_filename:
        raise UploadError("Upload is missing a filename.")

    if not is_allowed(original_filename):
        allowed = ", ".join(sorted(settings.ALLOWED_UPLOAD_EXTENSIONS))
        raise UploadError(
            f"File type not allowed for '{original_filename}'. "
            f"Allowed types: {allowed}."
        )

    s3_key = build_s3_key(original_filename)

    from botocore.exceptions import BotoCoreError, ClientError

    from storage.s3_client import get_s3_client

    client = get_s3_client()
    try:
        # Stream directly from the request to S3 – no temp file on disk.
        client.upload_fileobj(
            Fileobj=file_storage.stream,
            Bucket=settings.S3_BUCKET,
            Key=s3_key,
            ExtraArgs={
                "ContentType": file_storage.mimetype or "application/octet-stream"
            },
        )
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(
            f"Failed to upload '{original_filename}' to S3: {exc}"
        ) from exc

    return original_filename, s3_key
