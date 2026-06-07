# Knowledge Base Chat (AWS Bedrock)

A full-stack chat application built with Flask and an **AWS Bedrock Knowledge
Base**. Documents are uploaded to S3, indexed by Bedrock, and answered with
**RetrieveAndGenerate**. The UI is Hebrew / RTL.

There is **no** local embedding/index: retrieval, the vector store, and answer
generation are all managed by AWS Bedrock.

---

## Architecture

```
Browser (Hebrew/RTL UI)
        │
        ▼
Flask (app.py)
  ├── POST /upload  → S3 (data/<uuid>__name) → SQLite(uploaded_documents)
  │                   → Bedrock StartIngestionJob
  ├── POST /chat    → BedrockService.retrieve_and_generate(sessionId)
  │                   → persist turns in SQLite (history display)
  ├── GET  /status  → Bedrock ingestion job status
  ├── GET  /history → SQLite
  └── POST /clear   → SQLite + drop Bedrock session

services/bedrock_service.py
   • bedrock-agent          → StartIngestionJob / GetIngestionJob
   • bedrock-agent-runtime  → RetrieveAndGenerate

AWS Bedrock Knowledge Base  (managed embeddings + vector store + generation)
   • S3 data source: s3://<S3_BUCKET>/<S3_PREFIX>
```

**Chat turn:** the browser sends `POST /chat`; `ChatService` looks up the
Bedrock `sessionId` for the tab, calls `RetrieveAndGenerate`, persists both
turns to SQLite (for history display only), and returns the answer plus the
source filenames. Conversation memory is held by Bedrock via `sessionId`.

**Upload:** the file is streamed to S3 under the KB's data-source prefix,
recorded in `uploaded_documents`, and a Bedrock ingestion job is started so the
new content becomes searchable. Ingestion is asynchronous — the status card
shows "processing documents" until it completes; chat stays available.

---

## Project Structure

```
/
├── app.py                    # Flask entry point + routes
├── requirements.txt          # flask, python-dotenv, boto3
├── .env.example
│
├── config/settings.py        # env config (Bedrock + S3 + Flask)
│
├── services/
│   ├── bedrock_service.py     # Bedrock KB layer (ingestion + RAG)
│   └── chat_service.py        # orchestration + Bedrock session map
│
├── storage/
│   ├── s3_client.py           # boto3 S3 client factory
│   └── uploads.py             # upload write-path (validate + stream to S3)
│
├── database/models.py         # SQLite: sessions, messages, uploaded_documents
│
├── templates/index.html       # Hebrew/RTL chat UI
├── static/css/style.css
├── static/js/chat.js
└── data/                      # legacy local corpus (not used at runtime)
```

---

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env            # then fill in the values below
python app.py
```

Open `http://localhost:5000`.

### AWS prerequisites

- AWS credentials configured via the default chain (`aws configure`, env vars,
  or an instance role). Verify: `aws s3 ls s3://oz-private-aviadt/data/`.
- An existing Bedrock Knowledge Base with an S3 data source pointing at
  `s3://<S3_BUCKET>/<S3_PREFIX>`.
- IAM permissions for the running identity:
  `bedrock:RetrieveAndGenerate`, `bedrock:StartIngestionJob`,
  `bedrock:GetIngestionJob`, `bedrock:InvokeModel` on the generation model,
  plus `s3:PutObject` / `s3:ListBucket` on the bucket.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `FLASK_SECRET_KEY` | ✅ | — | Flask session signing key |
| `BEDROCK_KNOWLEDGE_BASE_ID` | ✅ | — | Existing Knowledge Base ID |
| `BEDROCK_DATA_SOURCE_ID` | ✅ | — | KB S3 data-source ID |
| `BEDROCK_MODEL_ID` | ✗ | `anthropic.claude-sonnet-4-6` | Generation model |
| `BEDROCK_MODEL_ARN` | ✗ | built from region + model id | Full model ARN override |
| `AWS_REGION` | ✗ | `us-east-1` | AWS region |
| `S3_BUCKET` | ✗ | `oz-private-aviadt` | Upload bucket |
| `S3_PREFIX` | ✗ | `documents/` | Upload prefix (**must match the KB data source**, e.g. `data/`) |
| `MAX_UPLOAD_MB` | ✗ | `20` | Max upload request size |
| `ALLOWED_UPLOAD_EXTENSIONS` | ✗ | `txt,pdf,docx` | Upload allow-list |
| `DB_PATH` | ✗ | `database/chat_history.db` | SQLite path |
| `FLASK_HOST` / `FLASK_PORT` / `FLASK_DEBUG` | ✗ | `0.0.0.0` / `5000` / `false` | Flask server |

> AWS credentials are resolved by boto3's default chain and are never stored in `.env`.

---

## Memory & Persistence

- **Conversation memory:** handled by Bedrock via `sessionId` (per browser tab,
  kept in memory and reset by "New Chat").
- **SQLite:** stores `sessions`, `messages` (for history display), and
  `uploaded_documents (id, original_filename, s3_key, upload_timestamp)`. The
  full S3 key is the system-of-record; the UI shows only the original filename.

---

## Notes

- New documents become searchable only after their Bedrock ingestion job
  completes (asynchronous).
- The full S3 key is stored internally; the sources panel displays only the
  original filename (mapped via `uploaded_documents`, with a basename fallback).
- The legacy local pipeline (FAISS / Hugging Face / Gemini) has been removed.
