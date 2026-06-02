"""
services/chat_service.py
────────────────────────
Orchestration layer between the Flask routes, the Bedrock Knowledge Base
service, and the SQLite persistence layer.

Responsibilities
  1. Resolve the Bedrock conversation session for the current browser session.
  2. Ask the Knowledge Base (RetrieveAndGenerate) for an answer.
  3. Persist both the user message and the assistant response to SQLite for
     history display.
  4. Return a clean response payload to the Flask route.

Conversation memory is handled natively by Bedrock via its ``sessionId``;
SQLite is used only for displaying and persisting chat history, not for
feeding context back to the model.
"""

from __future__ import annotations

import threading

from database.models import clear_session, get_history, save_message
from services.bedrock_service import BedrockService


class ChatService:
    """
    Stateless-per-request service that wraps one shared BedrockService.

    The only in-memory state is a mapping from the browser session id to the
    Bedrock session id, used to give Bedrock conversational continuity across
    turns within the same browser tab.
    """

    def __init__(self, bedrock_service: BedrockService) -> None:
        self._bedrock = bedrock_service
        self._sessions: dict[str, str] = {}   # browser_sid → bedrock_sid
        self._sessions_lock = threading.Lock()

    # ─────────────────────────────────────────────────────────────────
    # Chat
    # ─────────────────────────────────────────────────────────────────

    def chat(self, session_id: str, user_message: str) -> dict:
        """
        Handle one conversational turn.

        Parameters
        ----------
        session_id   : unique string identifying the browser session
        user_message : the text typed by the user

        Returns
        -------
        {
            "answer":  str,             # Knowledge Base answer text
            "context": list[dict],      # [{"source": <original filename>}, …]
        }
        """
        with self._sessions_lock:
            bedrock_sid = self._sessions.get(session_id)

        result = self._bedrock.retrieve_and_generate(
            query=user_message, session_id=bedrock_sid
        )

        # Remember the Bedrock session id for the next turn in this tab.
        new_sid = result.get("session_id")
        if new_sid:
            with self._sessions_lock:
                self._sessions[session_id] = new_sid

        # Persist both turns for history display.
        save_message(session_id, role="user", content=user_message)
        save_message(
            session_id,
            role="assistant",
            content=result["answer"],
            retrieved=result["context"],
        )

        return {"answer": result["answer"], "context": result["context"]}

    # ─────────────────────────────────────────────────────────────────
    # History
    # ─────────────────────────────────────────────────────────────────

    def get_history(self, session_id: str) -> list[dict]:
        """Return the full conversation history for a session."""
        return get_history(session_id)

    # ─────────────────────────────────────────────────────────────────
    # Clear
    # ─────────────────────────────────────────────────────────────────

    def clear(self, session_id: str) -> None:
        """
        Delete all messages for a session (used by the 'New chat' button) and
        forget the Bedrock session so the next turn starts a fresh conversation.
        """
        clear_session(session_id)
        with self._sessions_lock:
            self._sessions.pop(session_id, None)

    # ─────────────────────────────────────────────────────────────────
    # Engine status
    # ─────────────────────────────────────────────────────────────────

    @property
    def engine_status(self) -> dict:
        """Return Knowledge Base / ingestion readiness for the frontend."""
        return self._bedrock.ingestion_status()
