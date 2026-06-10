# Educational Assistant — Lesson Plan RAG System

A Flask web application providing a Hebrew RTL educational assistant.  
The assistant searches lesson plans from an AWS Bedrock Knowledge Base, adapts content
to teacher requirements, generates lesson-plan PDFs, and delivers them by email.

---

## Live Demo

The application is currently deployed on Amazon EC2 and available at:

**http://44.222.79.3:5000/**

---

## Architecture

```
Browser (Hebrew RTL UI)
  │
  ▼
Flask Application  ──────────────────────────────────────────────────┐
  │                                                                   │
  │  POST /chat                                                       │
  ▼                                                                   │
Amazon Bedrock Agent  ◄── Guardrails (content safety)                │
  │                                                                   │
  ├── Knowledge Base lookup  ──►  Amazon Bedrock Knowledge Base       │
  │                                    │                              │
  │                               Amazon S3 (documents)              │
  │                                                                   │
  ├── Action Group: create_pdf  ──►  AWS Lambda (PDF generation)      │
  │                                    └──►  Amazon S3 (PDF upload)  │
  │                                    └──►  Presigned download URL  │
  │                                                                   │
  └── Action Group: send_email  ──►  AWS Lambda (email delivery)      │
                                          └──►  Amazon SES            │
                                                                      │
Flask Application  ◄──────────────────────────────────────────────────┘
  │
  └── SQLite (chat history + document metadata — local only)
```

---

## Main Capabilities

- Upload educational documents (TXT, PDF, DOCX) to Amazon S3.
- Trigger Bedrock Knowledge Base ingestion after upload.
- Search and retrieve lesson plans from the Knowledge Base.
- Combine and adapt content from multiple lesson plans.
- Answer educational questions in Hebrew.
- Generate lesson-plan PDFs via a Lambda Action Group.
- Upload generated PDFs to Amazon S3 and return presigned download links.
- Send PDF download links by email via Amazon SES.
- Help & Support modal, User Profile modal, and suggestion chips in the UI.
- Per-tab session management: each browser tab maintains an independent conversation.

---

## AWS Services

| Service | Purpose |
|---|---|
| **Amazon Bedrock Agent** | Orchestrates conversation, routes requests to KB and Action Groups |
| **Bedrock Knowledge Base** | Indexes and retrieves lesson-plan documents |
| **Bedrock Guardrails** | Enforces responsible educational responses and content safety |
| **AWS Lambda** | Action Groups: PDF generation (`create_pdf`) and email delivery (`send_email`) |
| **Amazon S3** | Stores uploaded documents and generated PDFs |
| **Amazon SES** | Sends HTML emails with PDF download links |
| **Amazon EC2** | Hosts the Docker container |
| **IAM** | Roles and policies for Bedrock, S3, SES, Lambda, and CloudWatch |
| **SQLite** | Local per-container chat history and document metadata |

---

## Agent Action Groups (Lambda Tools)

### `create_pdf`

Creates a formatted PDF from a lesson plan, uploads it to S3, and returns a presigned download URL.

| Parameter | Description |
|---|---|
| `title` | Title of the lesson plan |
| `content` | Full text content of the lesson plan |

### `send_email`

Sends an HTML email through Amazon SES containing a clickable PDF download link.

| Parameter | Description |
|---|---|
| `lesson_title` | Title used in the email subject and body |
| `message` | Body text of the email |
| `download_url` | Presigned S3 URL for the generated PDF |

> The recipient email address is configured inside the Lambda function, not passed as a parameter.

---

## Project Structure

```
app.py                    Flask entry point and all HTTP routes
config/settings.py        Environment variable loader
services/
  bedrock_service.py      Bedrock Agent invocation and KB ingestion
  chat_service.py         Chat orchestration and SQLite persistence
storage/
  s3_client.py            boto3 S3 client singleton
  uploads.py              File validation, encoding normalisation, S3 upload
database/
  models.py               SQLite schema, read/write helpers
templates/index.html      Hebrew RTL single-page UI
static/                   CSS and legacy JS assets
Dockerfile                Container image definition
requirements.txt          Python dependencies
.env.example              Environment variable template
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in real values.  
**Never commit `.env` to version control.**

### Required

| Variable | Description |
|---|---|
| `FLASK_SECRET_KEY` | Secret key for Flask session signing |
| `BEDROCK_AGENT_ID` | Bedrock Agent ID (e.g. `ABCDE12345`) |
| `BEDROCK_AGENT_ALIAS_ID` | Bedrock Agent Alias ID (e.g. `TSTALIASID`) |
| `BEDROCK_KNOWLEDGE_BASE_ID` | Knowledge Base ID used for ingestion |
| `BEDROCK_DATA_SOURCE_ID` | Data Source ID within the Knowledge Base |

### Optional (defaults shown)

| Variable | Default | Description |
|---|---|---|
| `AWS_REGION` | `us-east-1` | AWS region for all services |
| `S3_BUCKET` | *(none)* | S3 bucket name for documents and PDFs |
| `S3_PREFIX` | `documents/` | Key prefix for uploaded documents |
| `MAX_UPLOAD_MB` | `20` | Maximum upload file size in MB |
| `ALLOWED_UPLOAD_EXTENSIONS` | `txt,pdf,docx` | Comma-separated allowed extensions |
| `BEDROCK_MODEL_ARN` | *(built from region + model ID)* | Override model ARN for KB generation |
| `BEDROCK_MODEL_ID` | `anthropic.claude-haiku-4-5-20251001-v1:0` | Foundation model ID if ARN is not set |
| `BEDROCK_SYSTEM_PROMPT` | *(empty — Bedrock default)* | Custom generation prompt template |
| `DB_PATH` | `database/chat_history.db` | SQLite database file path |
| `FLASK_HOST` | `0.0.0.0` | Host the Flask server binds to |
| `FLASK_PORT` | `5000` | Port the Flask server listens on |
| `FLASK_DEBUG` | `false` | Enable Flask debug mode |

> AWS credentials are **not** read from `.env`.  
> boto3 uses the default credential chain: `aws configure`, environment variables, or an EC2 instance role.

---

## Local Development

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and fill in your values

# 4. Run the application
python app.py
```

The application will be available at `http://localhost:5000`.

---

## Docker

### Build

```bash
docker build -t edu-assistant .
```

### Run with an env file

```bash
docker run -d \
  -p 5000:5000 \
  --env-file .env \
  -v ~/.aws:/home/appuser/.aws:ro \
  --name edu-assistant \
  edu-assistant
```

### Run with inline environment variables

```bash
docker run -d \
  -p 5000:5000 \
  -e FLASK_SECRET_KEY=<secret> \
  -e AWS_REGION=us-east-1 \
  -e S3_BUCKET=<bucket> \
  -e S3_PREFIX=data/ \
  -e BEDROCK_AGENT_ID=<agent-id> \
  -e BEDROCK_AGENT_ALIAS_ID=<alias-id> \
  -e BEDROCK_KNOWLEDGE_BASE_ID=<kb-id> \
  -e BEDROCK_DATA_SOURCE_ID=<ds-id> \
  -e BEDROCK_MODEL_ARN=<model-arn-or-profile-id> \
  -v ~/.aws:/home/appuser/.aws:ro \
  --name edu-assistant \
  edu-assistant
```

The container exposes port `5000`.  
The `-v ~/.aws:/home/appuser/.aws:ro` mount provides AWS credentials from the host.  
On EC2 with an instance role, omit the volume mount entirely.

---

## EC2 Deployment (Ubuntu)

### 1. Install Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) \
  signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io
sudo usermod -aG docker $USER   # log out and back in after this
```

### 2. Clone the repository

```bash
git clone https://github.com/<your-org>/<your-repo>.git
cd <your-repo>
```

### 3. Create and configure `.env`

```bash
cp .env.example .env
nano .env   # fill in all required values
```

### 4. Build the Docker image

```bash
docker build -t edu-assistant .
```

### 5. Run the container

```bash
# Using an IAM instance role (recommended for EC2)
docker run -d \
  -p 5000:5000 \
  --env-file .env \
  --name edu-assistant \
  edu-assistant

# Using mounted AWS credentials (alternative)
docker run -d \
  -p 5000:5000 \
  --env-file .env \
  -v ~/.aws:/home/appuser/.aws:ro \
  --name edu-assistant \
  edu-assistant
```

### 6. Open the inbound port

In the EC2 Security Group, add a custom TCP inbound rule:

| Type | Port | Source |
|---|---|---|
| Custom TCP | 5000 | `0.0.0.0/0` (or restrict to your IP) |

### 7. Verify the deployment

```bash
# Check the container is running
docker ps

# Tail application logs
docker logs -f edu-assistant

# Verify the application responds
curl http://localhost:5000/status
```

### 8. Verify Bedrock connectivity

Send a test message through the UI and confirm:
- The assistant returns an answer (Bedrock Agent is reachable).
- Citations appear in the response (Knowledge Base retrieval is working).

### 9. Verify PDF generation

Ask the assistant to create a PDF. Confirm:
- The Lambda Action Group returns a presigned URL.
- The link in the chat is clickable and downloads the file.

### 10. Verify email delivery

Ask the assistant to send the PDF by email. Confirm:
- Amazon SES accepts the message.
- The recipient receives the email with the download link.

---

## Required IAM Permissions

The IAM role or user used by the application requires:

```
bedrock:InvokeAgent
bedrock:StartIngestionJob
bedrock:GetIngestionJob
s3:PutObject
s3:GetObject
s3:DeleteObject
s3:ListBucket
ses:SendEmail          (Lambda execution role, not Flask)
lambda:InvokeFunction  (Bedrock Agent service role)
logs:CreateLogGroup    (Lambda execution role)
logs:CreateLogStream   (Lambda execution role)
logs:PutLogEvents      (Lambda execution role)
```

---

## Notes and Limitations

- **SES Sandbox**: By default, Amazon SES can only send to verified email addresses. Request SES Production Access to send to arbitrary recipients.
- **Presigned URL expiry**: Download links returned by the PDF Lambda expire after a fixed duration (configured inside the Lambda). Expired links will return a 403 error.
- **Guardrails**: Bedrock Guardrails policies must be configured and associated with the Agent in the AWS Console before they take effect.
- **Knowledge Base ingestion**: Newly uploaded documents are not immediately searchable. Ingestion typically takes 30–120 seconds depending on document count and size.
- **SQLite persistence**: The SQLite database lives inside the container. Chat history is lost when the container is removed. Mount a volume or use an external database for persistent history.
- **Session continuity**: Each browser tab uses an independent UUID as the Bedrock Agent `sessionId`. Closing a tab and reopening the site starts a new conversation. Bedrock Agent sessions may expire after a period of inactivity, depending on the Agent configuration.
