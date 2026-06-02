"""
storage/s3_client.py
────────────────────
Single place where the boto3 S3 client is constructed.

Credentials are resolved by boto3's default credential chain (AWS CLI
configuration, environment variables, or an EC2 instance role) – nothing
is read from .env.  Only the region comes from config.settings.

Centralising the client factory here means:
  • credential / region resolution happens in exactly one place;
  • the client is easy to mock in tests;
  • boto3 is imported lazily so the rest of the app does not pay the
    import cost (and does not hard-fail) when DOCUMENT_SOURCE=local.
"""

from __future__ import annotations

import threading

from config import settings

_client_lock = threading.Lock()
_s3_client = None


def get_s3_client():
    """
    Return a process-wide singleton boto3 S3 client.

    The client is created lazily on first use and reused thereafter.
    boto3 clients are thread-safe for concurrent calls, so a single shared
    instance is the recommended pattern.
    """
    global _s3_client
    if _s3_client is not None:
        return _s3_client

    with _client_lock:
        if _s3_client is None:
            import boto3  # imported lazily; only needed for the S3 source

            _s3_client = boto3.client("s3", region_name=settings.AWS_REGION)
    return _s3_client
