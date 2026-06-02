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
from werkzeug.utils import secure_filename

from config import settings


class UploadError(ValueError):
    """Raised when an upload is rejected (e.g. disallowed extension)."""


def _extension(filename: str) -> str:
    """Return the lower-case extension without the leading dot ('' if none)."""
    _, dot, ext = filename.rpartition(".")
    return ext.lower() if dot else ""


def is_allowed(filename: str) -> bool:
    return _extension(filename) in settings.ALLOWED_UPLOAD_EXTENSIONS


def build_s3_key(original_filename: str) -> str:
    """
    Build a unique S3 key for an upload.

    Format: ``<prefix><uuid4>__<safe_filename>`` e.g.
    ``documents/3f9c…__report.pdf``.  The double-underscore separator makes
    the original (sanitised) name trivially recoverable as a fallback when no
    DB row exists for the key.
    """
    safe = secure_filename(original_filename)
    # secure_filename can return '' for all-non-ASCII names (e.g. Hebrew).
    # Fall back to preserving just the extension so the key is still useful.
    if not safe:
        ext = _extension(original_filename)
        safe = f"upload.{ext}" if ext else "upload"
    return f"{settings.S3_PREFIX}{uuid.uuid4().hex}__{safe}"


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
