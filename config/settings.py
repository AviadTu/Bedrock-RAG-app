"""
config/settings.py
──────────────────
Central configuration loader.

All secrets and tuneable parameters are read from environment variables,
which are themselves loaded from a .env file by python-dotenv.
Nothing is hard-coded here; all values have documented defaults.

Usage
-----
    from config import settings

    print(settings.BEDROCK_KNOWLEDGE_BASE_ID)
    print(settings.BEDROCK_MODEL_ARN)
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (two levels up from this file)
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)


def _require(name: str) -> str:
    """Return env-var value or raise a clear error if it is missing."""
    value = os.getenv(name, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set.\n"
            f"Copy .env.example to .env and fill in the missing value."
        )
    return value


def _optional(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value if value else default


class _Settings:
    # ── Required secrets ─────────────────────────────────────────────
    @property
    def FLASK_SECRET_KEY(self) -> str:
        return _require("FLASK_SECRET_KEY")

    # ── AWS Bedrock Knowledge Base ───────────────────────────────────
    @property
    def BEDROCK_KNOWLEDGE_BASE_ID(self) -> str:
        return _require("BEDROCK_KNOWLEDGE_BASE_ID")

    @property
    def BEDROCK_DATA_SOURCE_ID(self) -> str:
        return _require("BEDROCK_DATA_SOURCE_ID")

    @property
    def BEDROCK_MODEL_ID(self) -> str:
        """Foundation model used by RetrieveAndGenerate for answer generation."""
        return _optional(
            "BEDROCK_MODEL_ID",
            "anthropic.claude-haiku-4-5-20251001-v1:0",
        )

    @property
    def BEDROCK_MODEL_ARN(self) -> str:
        """
        Model identifier passed to RetrieveAndGenerate as ``modelArn``.

        Resolution order:
          1. If BEDROCK_MODEL_ARN is set explicitly, use it as-is.
          2. Else if BEDROCK_MODEL_ID is an inference-profile id (prefixed with
             "global.", "us.", or "eu."), pass it through exactly as-is –
             cross-region inference profiles must NOT be wrapped in a
             foundation-model ARN.
          3. Else build a foundation-model ARN from AWS_REGION + BEDROCK_MODEL_ID.
        """
        explicit = _optional("BEDROCK_MODEL_ARN", "")
        if explicit:
            return explicit

        model_id = self.BEDROCK_MODEL_ID
        if model_id.startswith(("global.", "us.", "eu.")):
            return model_id

        return (
            f"arn:aws:bedrock:{self.AWS_REGION}::"
            f"foundation-model/{model_id}"
        )

    @property
    def BEDROCK_SYSTEM_PROMPT(self) -> str:
        """
        Optional generation prompt template for RetrieveAndGenerate.

        Must contain the ``$search_results$`` placeholder (Bedrock replaces it
        with the retrieved document chunks at inference time).

        When this variable is not set (or empty), the parameter is omitted
        from the API call entirely so Bedrock uses its own default prompt.
        """
        return os.getenv("BEDROCK_SYSTEM_PROMPT", "").strip()

    # ── Document storage ─────────────────────────────────────────────
    @property
    def AWS_REGION(self) -> str:
        return _optional("AWS_REGION", "us-east-1")

    @property
    def S3_BUCKET(self) -> str:
        return _optional("S3_BUCKET", "oz-private-aviadt")

    @property
    def S3_PREFIX(self) -> str:
        """
        Key prefix under which all knowledge-base documents live.
        Always normalised to end with a single trailing slash.
        """
        prefix = _optional("S3_PREFIX", "documents/").lstrip("/")
        return prefix if prefix.endswith("/") else prefix + "/"

    # ── Uploads ──────────────────────────────────────────────────────
    @property
    def MAX_UPLOAD_MB(self) -> int:
        return int(_optional("MAX_UPLOAD_MB", "20"))

    @property
    def ALLOWED_UPLOAD_EXTENSIONS(self) -> set[str]:
        """Lower-case extensions without the leading dot, e.g. {'txt','pdf','docx'}."""
        raw = _optional("ALLOWED_UPLOAD_EXTENSIONS", "txt,pdf,docx")
        return {
            ext.strip().lower().lstrip(".")
            for ext in raw.split(",")
            if ext.strip()
        }

    # ── Database ─────────────────────────────────────────────────────
    @property
    def DB_PATH(self) -> str:
        return _optional("DB_PATH", "database/chat_history.db")

    # ── Flask ────────────────────────────────────────────────────────
    @property
    def FLASK_HOST(self) -> str:
        return _optional("FLASK_HOST", "0.0.0.0")

    @property
    def FLASK_PORT(self) -> int:
        return int(_optional("FLASK_PORT", "5000"))

    @property
    def FLASK_DEBUG(self) -> bool:
        return _optional("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")


settings = _Settings()
