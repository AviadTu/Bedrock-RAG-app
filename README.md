# Lesson Plan RAG Assistant

## Project Topic

The selected topic is a RAG-based assistant for matching, improving, and adapting lesson plans to the needs and style of the target audience.

The system is intended for educational environments such as schools, high schools, and educational institutions. It helps educators search existing lesson materials, retrieve relevant content, and generate improved or adapted responses based on authentic lesson plans.

## Documents Used

The documents used in this project are authentic lesson plans uploaded into an AWS Bedrock Knowledge Base.

The documents demonstrate how educational content can be indexed, searched, and reused by the application in order to support teachers and educational staff.

## Public URL Used During Testing

The application was deployed and tested on an AWS EC2 instance.

Public URL used during testing:

```text
http://100.31.192.126:5000/
```

## How the Application Works

The application is a Flask-based web application with a Hebrew right-to-left user interface.

Users can upload lesson-plan documents through the website. The documents are uploaded to Amazon S3 under the configured data-source prefix. After upload, the application starts an AWS Bedrock Knowledge Base ingestion job so the new documents become searchable.

When the user asks a question, the application sends the query to AWS Bedrock Knowledge Base using RetrieveAndGenerate. Bedrock retrieves relevant information from the indexed documents and generates an answer based on the lesson-plan content.

The application does not create or manage a local vector database. Retrieval, embeddings, indexing, and generation are handled by AWS Bedrock Knowledge Base.

## Architecture

```text
Browser
  |
  v
Flask Application
  |
  |-- Upload documents
  |     -> Amazon S3
  |     -> AWS Bedrock Knowledge Base ingestion
  |
  |-- Chat request
        -> AWS Bedrock RetrieveAndGenerate
        -> Answer returned to the user
```

## Main Components

```text
app.py
```

Main Flask entry point and application routes.

```text
Dockerfile
```

Defines the Docker image used to run the application.

```text
services/bedrock_service.py
```

Handles communication with AWS Bedrock Knowledge Base.

```text
services/chat_service.py
```

Manages chat flow and communication between the UI and Bedrock.

```text
storage/s3_client.py
storage/uploads.py
```

Handle document upload to Amazon S3.

```text
database/models.py
```

Stores local chat history and uploaded document metadata in SQLite.

```text
templates/index.html
static/css/style.css
static/js/chat.js
```

Hebrew RTL web interface.

## Docker Deployment

The project was built locally as a Docker image and pushed to Docker Hub.

Docker image used:

```text
aviadq550/web-rag-app:latest
```

On the EC2 Ubuntu instance, the image was pulled and executed as a container.

Example run command:

```bash
sudo docker run -d -p 5000:5000 -e FLASK_SECRET_KEY=<secret> -e FLASK_DEBUG=true -e AWS_REGION=us-east-1 -e S3_BUCKET=oz-private-aviadt -e S3_PREFIX=data/ -e MAX_UPLOAD_MB=20 -e ALLOWED_UPLOAD_EXTENSIONS=txt,pdf,docx -e BEDROCK_KNOWLEDGE_BASE_ID=<knowledge-base-id> -e BEDROCK_DATA_SOURCE_ID=<data-source-id> -e BEDROCK_MODEL_ARN=<model-arn-or-model-id> -v ~/.aws:/home/appuser/.aws:ro --name web-rag-app aviadq550/web-rag-app:latest
```

The container was exposed on port 5000:

```text
0.0.0.0:5000->5000/tcp
```

The EC2 Security Group inbound rules included port 5000 in order to allow access to the web application.

## Environment Variables

The application uses environment variables for configuration.

Important variables include:

```text
FLASK_SECRET_KEY
AWS_REGION
S3_BUCKET
S3_PREFIX
BEDROCK_KNOWLEDGE_BASE_ID
BEDROCK_DATA_SOURCE_ID
BEDROCK_MODEL_ARN
MAX_UPLOAD_MB
ALLOWED_UPLOAD_EXTENSIONS
```

AWS credentials are not stored inside the project code. During testing, AWS credentials were configured on the EC2 instance and mounted into the Docker container using:

```bash
-v ~/.aws:/home/appuser/.aws:ro
```

## AWS Services Used

The project uses the following AWS services:

```text
Amazon EC2
```

Used to host and run the Docker container.

```text
Amazon S3
```

Used to store uploaded lesson-plan documents.

```text
AWS Bedrock Knowledge Base
```

Used for document indexing, retrieval, and answer generation.

```text
IAM
```

Used for permissions to access S3 and Bedrock.

## Cleanup After Testing

After completing the testing phase, all temporary AWS resources created for the project were deleted in order to prevent unnecessary charges and to leave the environment clean.

The following resources were removed:

```text
EC2 Instance
Amazon S3 Bucket used for document storage
AWS Bedrock Knowledge Base
Vector Store associated with the Knowledge Base
```

These resources were used solely for development, testing, and demonstration purposes and were deleted after the successful completion of the project validation process.

## Notes

New uploaded documents are not immediately searchable. After upload, AWS Bedrock starts an ingestion process. Once ingestion is complete, the document becomes available for retrieval through the RAG chat.

The local SQLite database is used only for chat history and metadata display. The actual RAG retrieval is managed by AWS Bedrock Knowledge Base.
