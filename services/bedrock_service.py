"""
services/bedrock_service.py
───────────────────────────
Service layer over Amazon Bedrock Knowledge Bases.

This replaces the legacy local RAG pipeline (Hugging Face embeddings +
FAISS + Gemini).  Retrieval, embedding, the vector store, and answer
generation are all managed by AWS Bedrock.  This module only:

  • starts and tracks Knowledge Base ingestion jobs (after uploads), and
  • answers questions via a Bedrock Agent (invoke_agent).

Two boto3 clients are used:
  • bedrock-agent          → control plane (StartIngestionJob / GetIngestionJob)
  • bedrock-agent-runtime  → data plane    (InvokeAgent; RetrieveAndGenerate kept for reference)

Credentials come from the default AWS credential chain; only the region is
read from config.settings.
"""

from __future__ import annotations

import threading

from config import settings
from database.models import get_display_name

# Ingestion job states reported by Bedrock, mapped to coarse UI states.
_INGESTION_DONE = "COMPLETE"
_INGESTION_FAILED_STATES = {"FAILED"}
_INGESTION_ACTIVE_STATES = {"STARTING", "IN_PROGRESS", "STOPPING"}


def _log(msg: str) -> None:
    try:
        print(f"[bedrock] {msg}", flush=True)
    except (OSError, ValueError):
        pass


class BedrockService:
    """
    Thread-safe wrapper around the Bedrock Knowledge Base APIs.

    A single instance is shared across all Flask requests.  boto3 clients are
    created lazily so the app can import without AWS connectivity and so unit
    tests can run without credentials.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agent_client = None          # bedrock-agent (control plane)
        self._runtime_client = None        # bedrock-agent-runtime (data plane)
        # Tracks the most recent ingestion job so /status can report progress.
        self._last_job: dict | None = None

    # ─────────────────────────────────────────────────────────────────
    # Lazy client factories
    # ─────────────────────────────────────────────────────────────────

    def _agent(self):
        if self._agent_client is None:
            with self._lock:
                if self._agent_client is None:
                    import boto3
                    self._agent_client = boto3.client(
                        "bedrock-agent", region_name=settings.AWS_REGION
                    )
        return self._agent_client

    def _runtime(self):
        if self._runtime_client is None:
            with self._lock:
                if self._runtime_client is None:
                    import boto3
                    self._runtime_client = boto3.client(
                        "bedrock-agent-runtime", region_name=settings.AWS_REGION
                    )
        return self._runtime_client

    # ─────────────────────────────────────────────────────────────────
    # Ingestion (control plane)
    # ─────────────────────────────────────────────────────────────────

    def start_ingestion(self) -> dict:
        """
        Start a Knowledge Base ingestion job so newly-uploaded S3 documents
        are embedded and indexed by Bedrock.  Returns ``{job_id, status}``.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            resp = self._agent().start_ingestion_job(
                knowledgeBaseId=settings.BEDROCK_KNOWLEDGE_BASE_ID,
                dataSourceId=settings.BEDROCK_DATA_SOURCE_ID,
            )
        except (BotoCoreError, ClientError) as exc:
            _log(f"StartIngestionJob failed: {exc}")
            raise RuntimeError(f"Failed to start ingestion job: {exc}") from exc

        job = resp.get("ingestionJob", {})
        record = {
            "job_id": job.get("ingestionJobId"),
            "status": job.get("status", "STARTING"),
        }
        with self._lock:
            self._last_job = record
        _log(f"Started ingestion job {record['job_id']} ({record['status']}).")
        return record

    def _refresh_last_job(self) -> dict | None:
        """Poll Bedrock for the latest status of the tracked ingestion job."""
        with self._lock:
            job = dict(self._last_job) if self._last_job else None
        if not job or not job.get("job_id"):
            return job

        from botocore.exceptions import BotoCoreError, ClientError

        try:
            resp = self._agent().get_ingestion_job(
                knowledgeBaseId=settings.BEDROCK_KNOWLEDGE_BASE_ID,
                dataSourceId=settings.BEDROCK_DATA_SOURCE_ID,
                ingestionJobId=job["job_id"],
            )
            job["status"] = resp.get("ingestionJob", {}).get("status", job["status"])
            with self._lock:
                self._last_job = job
        except (BotoCoreError, ClientError) as exc:
            _log(f"GetIngestionJob failed (non-fatal): {exc}")
        return job

    def ingestion_status(self) -> dict:
        """
        Report status for the frontend.

        The Knowledge Base is always queryable, so ``ready`` is always True –
        chat is never blocked.  ``status`` reflects the latest ingestion job
        so the UI can show "processing documents" after an upload.
        """
        job = self._refresh_last_job()
        if not job:
            return {"ready": True, "status": "ready", "ingestion": None}

        raw = (job.get("status") or "").upper()
        if raw == _INGESTION_DONE:
            ui_status = "ready"
        elif raw in _INGESTION_FAILED_STATES:
            ui_status = "ingestion_failed"
        elif raw in _INGESTION_ACTIVE_STATES:
            ui_status = "ingesting"
        else:
            ui_status = "ingesting"

        return {"ready": True, "status": ui_status, "ingestion": job}

    # ─────────────────────────────────────────────────────────────────
    # Retrieve + generate (data plane)
    # ─────────────────────────────────────────────────────────────────

    def retrieve_and_generate(
        self, query: str, session_id: str | None = None
    ) -> dict:
        """
        Answer a question with the Knowledge Base.

        Conversation memory is handled natively by Bedrock via ``sessionId``:
        the returned session id should be passed back on the next turn.

        Returns
        -------
        {
            "answer":     str,
            "context":    [{"source": <original filename>}, …],
            "session_id": str,   # Bedrock session id to reuse next turn
        }
        """
        from botocore.exceptions import ClientError

        kb_config: dict = {
            "knowledgeBaseId": settings.BEDROCK_KNOWLEDGE_BASE_ID,
            "modelArn": settings.BEDROCK_MODEL_ARN,
        }

        kb_config["retrievalConfiguration"] = {
        "vectorSearchConfiguration": {
            "numberOfResults": 8
            }
        }

        # ── Generation configuration ─────────────────────────────────
        # Build only when at least one sub-key is present, so we never
        # send an empty object to Bedrock.
        generation_config: dict = {}

        # System / generation prompt.
        # Set BEDROCK_SYSTEM_PROMPT in .env to override Bedrock's default.
        # The template MUST contain the $search_results$ placeholder.
        # When the variable is absent or empty the parameter is omitted
        # entirely and Bedrock falls back to its built-in default prompt.
        system_prompt = settings.BEDROCK_SYSTEM_PROMPT
        if system_prompt:
            generation_config["promptTemplate"] = {
                "textPromptTemplate": system_prompt
            }

        # ── Inference tuning (commented out – uncomment to activate) ──
        # generation_config["inferenceConfig"] = {
        #     "textInferenceConfig": {
        #         "temperature": 0.3,   # 0.0 = deterministic, 1.0 = creative
        #         "topP": 0.9,
        #         "maxTokens": 512,
        #         # "stopSequences": [],
        #     }
        # }

        if generation_config:
            kb_config["generationConfiguration"] = generation_config

        config = {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": kb_config,
        }

        def _call(sid: str | None):
            kwargs = {
                "input": {"text": query},
                "retrieveAndGenerateConfiguration": config,
            }
            # Forward the previous Bedrock sessionId so the Knowledge Base
            # treats subsequent turns within the same conversation as a
            # continuation (giving the assistant access to earlier
            # questions and answers in the same chat session).  If the
            # sessionId is missing or rejected, the caller will retry
            # below with sid=None to start a fresh session.
            if sid:
                kwargs["sessionId"] = sid
            return self._runtime().retrieve_and_generate(**kwargs)

        try:
            resp = _call(session_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            # An expired or invalid session id cannot be reused – start fresh.
            if session_id and code in (
                "ValidationException",
                "ConflictException",
                "ResourceNotFoundException",
            ):
                _log(f"Session '{session_id}' rejected ({code}); starting new session.")
                resp = _call(None)
            else:
                _log(f"RetrieveAndGenerate failed: {exc}")
                raise RuntimeError(f"Bedrock query failed: {exc}") from exc

        raw_output_text = (resp.get("output", {}) or {}).get("text", "")
        answer = raw_output_text.strip()
        new_session_id = resp.get("sessionId", session_id)

        raw_citations = resp.get("citations", []) or []
        sources = self._extract_sources(raw_citations)

        return {
            "answer": answer,
            "context": sources,
            "session_id": new_session_id,
        }

    # ─────────────────────────────────────────────────────────────────
    # Invoke Agent (data plane)
    # ─────────────────────────────────────────────────────────────────

    def invoke_agent(self, query: str, session_id: str) -> dict:
        """
        Answer a question using a Bedrock Agent.

        The ``session_id`` (browser UUID) is passed directly as the Agent
        sessionId so Bedrock can maintain conversational continuity across
        turns without a separate in-memory mapping.

        Returns
        -------
        {
            "answer":  str,
            "context": [],   # TODO: extract citations from agent trace events
        }

        Raises
        ------
        EnvironmentError  if BEDROCK_AGENT_ID or BEDROCK_AGENT_ALIAS_ID are
                          missing from the environment.
        RuntimeError      if the AWS call fails or returns no answer chunks.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        agent_id = settings.BEDROCK_AGENT_ID
        agent_alias_id = settings.BEDROCK_AGENT_ALIAS_ID

        try:
            resp = self._runtime().invoke_agent(
                agentId=agent_id,
                agentAliasId=agent_alias_id,
                sessionId=session_id,
                inputText=query,
            )
        except (BotoCoreError, ClientError) as exc:
            _log(f"InvokeAgent failed: {exc}")
            raise RuntimeError(f"Bedrock Agent query failed: {exc}") from exc

        # The response carries an EventStream under 'completion'.
        # Iterate over it, collect text chunks and per-chunk citations.
        chunks: list[str] = []
        all_citations: list[dict] = []
        try:
            for event in resp.get("completion", []):
                if "chunk" in event:
                    chunk = event["chunk"]

                    chunk_bytes = chunk.get("bytes", b"")
                    if chunk_bytes:
                        chunks.append(chunk_bytes.decode("utf-8"))

                    attribution = chunk.get("attribution", {}) or {}
                    citations = attribution.get("citations", []) or []
                    all_citations.extend(citations)
        except (BotoCoreError, ClientError) as exc:
            _log(f"InvokeAgent stream error: {exc}")
            raise RuntimeError(f"Bedrock Agent stream error: {exc}") from exc

        answer = "".join(chunks).strip()
        if not answer:
            raise RuntimeError(
                "Bedrock Agent returned no answer chunks. "
                "Check agentId, agentAliasId, and Agent configuration in AWS."
            )

        sources = self._extract_sources(all_citations)
        return {"answer": answer, "context": sources}

    # ─────────────────────────────────────────────────────────────────
    # Citation → source-label mapping
    # ─────────────────────────────────────────────────────────────────

    def _extract_sources(self, citations: list[dict]) -> list[dict]:
        """
        Turn Bedrock citations into a de-duplicated list of display sources.

        Each entry contains ``source`` (human-readable filename) and
        ``s3_key`` (the underlying object key, used by the frontend to
        request a pre-signed download URL).
        """
        seen: set[str] = set()
        sources: list[dict] = []
        for citation in citations:
            for ref in citation.get("retrievedReferences", []) or []:
                uri = (
                    ref.get("location", {})
                    .get("s3Location", {})
                    .get("uri", "")
                )
                if not uri:
                    continue
                label = self._label_for_uri(uri)
                if label and label not in seen:
                    seen.add(label)
                    sources.append(
                        {"source": label, "s3_key": self.s3_key_for_uri(uri)}
                    )
        return sources

    @staticmethod
    def _label_for_uri(uri: str) -> str:
        """
        Map an S3 URI (e.g. s3://bucket/data/report.pdf) to a friendly
        label for display in chat citations.

        New uploads use a clean key (``<prefix><filename>``) so the basename
        IS the original filename.  For backward compatibility we still strip
        any legacy ``<uuid>__`` prefix and fall back to the upload-table
        display name if one was recorded.
        """
        # Strip the "s3://<bucket>/" prefix to recover the object key.
        key = uri
        if uri.startswith("s3://"):
            without_scheme = uri[len("s3://"):]
            _, _, key = without_scheme.partition("/")

        basename = key.rsplit("/", 1)[-1]

        # Backward-compat: strip legacy <uuid-hex>__ prefix if present.
        prefix, sep, remainder = basename.partition("__")
        if sep and prefix and all(c in "0123456789abcdef" for c in prefix.lower()):
            return remainder

        # Fall back to a recorded display name (legacy uploads), else basename.
        return get_display_name(key) or basename

    @staticmethod
    def s3_key_for_uri(uri: str) -> str:
        """Return the object key portion of an ``s3://bucket/key`` URI."""
        if uri.startswith("s3://"):
            without_scheme = uri[len("s3://"):]
            _, _, key = without_scheme.partition("/")
            return key
        return uri
