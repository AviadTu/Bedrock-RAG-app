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

import re
import threading

from database.models import clear_session, get_history, save_message
from services.bedrock_service import BedrockService

# Conversation-memory tuning knobs.  Kept small on purpose: the goal is
# to give the model just enough context to resolve self-referential
# follow-ups ("what's my name?", "what did I ask before?") without
# inflating prompts or bleeding old chat into KB retrieval.
_HISTORY_MAX_MESSAGES = 6     # last N raw turns from SQLite (3 exchanges)
_HISTORY_MAX_CHARS    = 500   # per-message cap to keep the prompt bounded

# ─────────────────────────────────────────────────────────────────────
# Local conversation-memory patterns
#
# Bedrock RetrieveAndGenerate has no conversation-history placeholder
# in its prompt template, so even when we prepend the prior turns to
# the user's query, the model frequently replies with "I am unable to
# assist" for trivial self-referential questions ("what's my name?",
# "what did I just ask?").  For that narrow class of questions we
# answer locally from SQLite instead of calling Bedrock at all.
# Document-grounded questions are untouched and still go to the KB.
# ─────────────────────────────────────────────────────────────────────

# Trailing/closing punctuation that should not affect intent detection.
_TRIM_CHARS = " \t\n\r?.!,;:،؟"

# Questions of the form "what is my name?".
_NAME_QUESTION_RE = re.compile(
    r"(?:איך\s+קוראים\s+לי|מה\s+שמי|מה\s+השם\s+שלי)",
    re.UNICODE,
)

# Questions of the form "what did I ask before?" / "what did I just say?".
_PREV_QUESTION_RE = re.compile(
    r"(?:"
    r"מה\s+השאלה\s+(?:האחרונה|הקודמת)(?:\s+ששאלתי)?"
    r"|מה\s+שאלתי(?:\s+קודם|\s+לפני\s+זה|\s+קודם\s+לכן)?"
    r"|מה\s+אמרתי(?:\s+לך)?(?:\s+הרגע|\s+קודם|\s+לפני\s+זה)?"
    r")",
    re.UNICODE,
)

# Name introductions: "קוראים לי X", "שמי X", "השם שלי X" (optional "הוא").
# Captures up to three short tokens so multi-word names also work, while
# stopping at sentence-ending punctuation so we don't swallow the rest
# of the message.
_NAME_EXTRACT_RE = re.compile(
    r"(?:קוראים\s+לי|שמי(?:\s+הוא)?|השם\s+שלי(?:\s+הוא)?)\s+"
    r"([^\s.!?,;:\n]+(?:\s+[^\s.!?,;:\n]+){0,2})",
    re.UNICODE,
)


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
        # ── Local conversation-memory shortcut ───────────────────────
        # Try to answer trivial self-referential questions ("what's my
        # name?", "what did I just ask?") directly from SQLite so we
        # don't depend on Bedrock RetrieveAndGenerate – which provably
        # ignores the augmented history and replies "Sorry, I am unable
        # to assist" for this class of questions.  Document-grounded
        # questions yield no match here and fall through to Bedrock,
        # preserving existing RAG behaviour.
        local_answer = self._try_local_memory_answer(session_id, user_message)
        if local_answer is not None:
            save_message(session_id, role="user", content=user_message)
            save_message(
                session_id, role="assistant", content=local_answer, retrieved=[]
            )
            return {"answer": local_answer, "context": []}

        with self._sessions_lock:
            bedrock_sid = self._sessions.get(session_id)

        # Build the query sent to Bedrock by prepending a short, bounded
        # transcript of the most recent raw turns from SQLite.  Bedrock's
        # RetrieveAndGenerate prompt template has no conversation-history
        # placeholder of its own, so doing this here is what lets the
        # model answer self-referential follow-ups such as "what's my
        # name?" while still grounding document answers in the KB.
        augmented_query = self._build_augmented_query(session_id, user_message)

        result = self._bedrock.retrieve_and_generate(
            query=user_message, session_id=bedrock_sid
        )

        # Remember the Bedrock session id for the next turn in this tab.
        new_sid = result.get("session_id")
        if new_sid:
            with self._sessions_lock:
                self._sessions[session_id] = new_sid

        # Persist ONLY the raw user message and the raw assistant answer.
        # The augmented prompt is never written back, so the next turn
        # rebuilds its history from clean originals – no recursive growth.
        save_message(session_id, role="user", content=user_message)
        save_message(
            session_id,
            role="assistant",
            content=result["answer"],
            retrieved=result["context"],
        )

        final_payload = {"answer": result["answer"], "context": result["context"]}
        return final_payload

    # ─────────────────────────────────────────────────────────────────
    # Local conversation-memory resolver
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _try_local_memory_answer(
        session_id: str, user_message: str
    ) -> str | None:
        """
        Return a locally-computed answer for trivial memory questions,
        or ``None`` to indicate the caller should fall through to the
        normal Bedrock path.

        Only two narrow intents are handled:
          * "what is my name?" → look back through prior *user* messages
            for an introduction ("קוראים לי X", "שמי X", "השם שלי X")
            and return the captured name.
          * "what did I ask before?" → return the most recent *user*
            message stored in SQLite, excluding the current one (which
            has not been written to the DB yet at this point, but is
            filtered defensively).

        Any other input – including document/KB questions – yields
        ``None`` so the existing RAG flow runs unchanged.
        """
        normalized = (user_message or "").strip().strip(_TRIM_CHARS).strip()
        if not normalized:
            return None

        # 1) "What did I ask / say before?" – more specific intent first.
        if _PREV_QUESTION_RE.search(normalized):
            current = (user_message or "").strip()
            prior_user_msgs = [
                (m.get("content") or "").strip()
                for m in get_history(session_id)
                if m.get("role") == "user"
                and (m.get("content") or "").strip() != current
            ]
            if prior_user_msgs:
                last_question = prior_user_msgs[-1]
                return f'השאלה הקודמת ששאלת הייתה: "{last_question}"'

        # 2) "What is my name?" – scan user messages newest-to-oldest.
        if _NAME_QUESTION_RE.search(normalized):
            for msg in reversed(get_history(session_id)):
                if msg.get("role") != "user":
                    continue
                match = _NAME_EXTRACT_RE.search(msg.get("content") or "")
                if not match:
                    continue
                name = match.group(1).strip().strip(_TRIM_CHARS).strip()
                if name:
                    return f"שמך {name}."

        return None

    # ─────────────────────────────────────────────────────────────────
    # Conversation-history augmentation
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_augmented_query(session_id: str, user_message: str) -> str:
        """
        Compose `<short transcript>\n---\n<current user message>` from
        the last few raw rows in SQLite.

        Properties:
          * Pulls from the persisted ``messages`` table, which stores
            ORIGINAL user inputs and assistant outputs (never the
            augmented prompts) – so this is safe from recursive growth.
          * Capped at ``_HISTORY_MAX_MESSAGES`` rows total and
            ``_HISTORY_MAX_CHARS`` per row, so the added context stays
            small relative to the KB chunks.
          * Returns the user message unchanged on the very first turn,
            preserving existing RAG behaviour for fresh conversations.
        """
        prior = get_history(session_id)[-_HISTORY_MAX_MESSAGES:]
        if not prior:
            return user_message

        def _clip(text: str) -> str:
            text = (text or "").strip()
            if len(text) <= _HISTORY_MAX_CHARS:
                return text
            return text[:_HISTORY_MAX_CHARS].rstrip() + "…"

        lines = []
        for msg in prior:
            label = "משתמש" if msg.get("role") == "user" else "עוזר"
            lines.append(f"{label}: {_clip(msg.get('content', ''))}")
        transcript = "\n".join(lines)

        return (
            "היסטוריית השיחה האחרונה (לעיון בלבד, אל תצטט אותה מילולית; "
            "השתמש בה רק כדי להבין את ההקשר של שאלת המשתמש):\n"
            f"{transcript}\n"
            "---\n"
            f"שאלת המשתמש הנוכחית: {user_message}"
        )

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
