# Support Ticket Agent

A production-oriented Gmail-to-ticket support platform built with FastAPI, LangGraph, Groq, PostgreSQL, FAISS, Gmail, Slack, Redis/Celery, and React.

The platform receives Gmail Pub/Sub notifications, retrieves new messages through Gmail History API, resolves conversations into durable tickets, classifies requests with structured model output, searches a semantic FAQ index, and then applies a deterministic fail-closed policy. Safe and well-grounded cases enqueue an RFC-threaded Gmail reply; uncertain or unsafe cases enqueue a Slack escalation for human review.

## Features

- Gmail OAuth authorization-code flow with signed, expiring callback state.
- Durable mailbox OAuth credential records and Gmail history cursors in PostgreSQL.
- Gmail Pub/Sub webhook ingestion and History API traversal.
- Ticket threading through Gmail thread IDs plus `Message-ID`, `In-Reply-To`, and `References` headers.
- Incoming Gmail message idempotency backed by a unique database constraint.
- Typed LangGraph workflows and Groq structured-output classification.
- FAISS similarity search with HuggingFace sentence-transformer embeddings.
- Deterministic, fail-closed routing based on classification confidence, FAQ similarity, sentiment, category, and priority.
- RFC-threaded Gmail responses for safe, grounded cases.
- Slack Block Kit escalation notifications for human-review cases.
- Redis/Celery outbound jobs with exponential backoff and terminal failure persistence.
- Ticket history and agent-decision audit records.
- React and TypeScript operations dashboard for queue filtering, ticket updates, and agent decisions.
- Backend and frontend Docker images, Docker Compose, and GitHub Actions CI.
- Google Cloud production deployment guidance for Cloud Run, GKE Autopilot, Cloud SQL, Memorystore, Secret Manager, and Cloud Monitoring.

## Architecture

```text
Gmail mailbox
    |
    | Gmail watch notification
    v
Google Pub/Sub ---> POST /webhooks/gmail ---> Gmail History API
                                                |
                                                v
                                      Typed LangGraph workflow
                                                |
                  +-----------------------------+-----------------------------+
                  |                             |                             |
             Groq classifier             PostgreSQL tickets             FAISS FAQ search
                  |                    messages/history/cursors                 |
                  +-----------------------------+-----------------------------+
                                                |
                                      Deterministic routing policy
                                                |
                          +---------------------+---------------------+
                          |                                           |
                 safe + grounded                              unsafe / uncertain
                          |                                           |
                 Celery gmail_send                       Celery slack_escalation
                          |                                           |
                    Gmail reply                               Slack webhook

React dashboard ---> FastAPI ticket API ---> PostgreSQL
Celery worker <---- Redis broker --------> failed-job records in PostgreSQL
```

The model supplies structured classification data, but it does not make the final delivery decision. The deterministic policy is the final authority and escalates whenever required evidence or confidence is insufficient.

## Repository Layout

```text
backend/app/
  agent/          Typed LangGraph state, nodes, graphs, and routing policy
  api/            Health, ticket, failed-job, Gmail webhook, OAuth, and development routes
  database/       SQLAlchemy models, repositories, session composition, mailbox store
  integrations/   Gmail, runtime Gmail orchestration, Groq, and Slack adapters
  knowledge/      FAQ source, FAISS index builder, and retriever
  schemas/        Pydantic API and workflow models
  services/       Ticket, email-ticket, failed-job, delivery, and retry services
frontend/src/     React operations dashboard and typed API client
docs/             Production architecture and operational guidance
.github/workflows GitHub Actions verification pipeline
```

## Technology Stack

| Layer | Technology |
|---|---|
| API | Python 3.11, FastAPI, Pydantic |
| Workflow | LangGraph |
| Classification | Groq structured output |
| Persistence | PostgreSQL, SQLAlchemy 2 |
| Retrieval | FAISS, sentence-transformers |
| Email | Gmail OAuth, Gmail API, Gmail History API, Google Pub/Sub |
| Escalation | Slack incoming webhook |
| Jobs | Celery, Redis |
| Dashboard | React 18, TypeScript, TanStack Query, Vite, Nginx |
| Verification | Ruff, pytest, ESLint, TypeScript, Vite, npm audit, Docker |

## Configuration

Copy the example file and replace every empty secret with an environment-specific value:

```cmd
copy .env.example .env
```

For a backend process started from the `backend` directory, also provide the file there because Pydantic resolves `.env` from the process working directory:

```cmd
copy .env.example backend\.env
```

Important variables:

| Variable | Purpose |
|---|---|
| `APP_ENV` | Runtime environment; the development test route is disabled in production |
| `DATABASE_URL` | SQLAlchemy PostgreSQL connection URL; in Cloud Run it must use the mounted `/cloudsql/...` socket or be paired with `CLOUD_SQL_CONNECTION_NAME` |
| `CLOUD_SQL_CONNECTION_NAME` | Optional Cloud SQL instance connection name (`project:region:instance`); converts PostgreSQL access to the Cloud Run Unix socket |
| `OPERATOR_API_KEY` | Shared operator credential required by ticket and failed-job APIs |
| `GROQ_API_KEY` | Groq API credential; Gmail runtime processing is composed when this is set |
| `GROQ_MODEL` | Structured classification model |
| `EMBEDDING_MODEL` | HuggingFace embedding model used by the FAQ retriever |
| `FAQ_SOURCE_PATH` | FAQ JSON source path |
| `FAQ_INDEX_PATH` | Generated FAISS index directory |
| `GOOGLE_CLIENT_ID` | Google OAuth web client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth web client secret |
| `GOOGLE_REDIRECT_URI` | Registered OAuth callback, normally `/auth/google/callback` |
| `GOOGLE_OAUTH_STATE_SECRET` | High-entropy secret used to sign OAuth state |
| `GOOGLE_OAUTH_STATE_TTL_SECONDS` | OAuth state lifetime, from 60 to 3600 seconds |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook for escalations |
| `REDIS_URL` | Celery Redis broker/backend URL |
| `CLASSIFICATION_CONFIDENCE_THRESHOLD` | Minimum model confidence accepted by policy |
| `FAQ_SIMILARITY_THRESHOLD` | Minimum semantic grounding score accepted by policy |
| `MAX_RETRY_ATTEMPTS` | Maximum outbound delivery attempts |
| `RETRY_BACKOFF_BASE_SECONDS` | Exponential retry base delay |

Generate `GOOGLE_OAUTH_STATE_SECRET` with a cryptographically secure secret generator. Never reuse an API key or OAuth client secret as the state-signing secret.

## Run with Docker Compose

Requirements:

- Docker Engine or Docker Desktop with Compose v2.
- Groq, Google OAuth, and Slack configuration in `.env` when those integrations are used.

Start PostgreSQL, Redis, the API, Celery worker, and dashboard:

```cmd
docker compose up --build
```

Services:

- Dashboard: `http://localhost:3000`
- FastAPI: `http://localhost:8000`
- OpenAPI UI: `http://localhost:8000/docs`
- Health check: `http://localhost:8000/health`

Stop the stack without deleting data:

```cmd
docker compose down
```

Delete local PostgreSQL and Redis volumes as well:

```cmd
docker compose down --volumes
```

The Compose API command runs the versioned Alembic migrations before starting Uvicorn. The migration guard adopts only a verified matching legacy schema and refuses unmanaged or drifted schemas.

## Run Locally without Docker

Requirements:

- Python 3.11.
- Node.js 22 and npm.
- Running PostgreSQL and Redis instances.

Create and install the backend environment from the repository root:

```cmd
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
copy .env.example backend\.env
```

Edit `backend\.env`, initialize the database, and build the FAQ index:

```cmd
cd backend
.venv\Scripts\python.exe -m app.database.init_db
.venv\Scripts\python.exe -m app.knowledge.build_index
```

Start the API in one terminal:

```cmd
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Start the Celery worker in another terminal after Redis is available:

```cmd
cd backend
.venv\Scripts\celery.exe -A app.tasks:celery_app worker --loglevel=INFO
```

Install and start the dashboard in another terminal:

```cmd
cd frontend
npm ci
npm run dev
```

The Vite development server is normally available at `http://localhost:5173`. Set `VITE_API_BASE_URL=http://localhost:8000` for the frontend build or development process when the API is not exposed through the same origin. In the Compose image, Nginx proxies ticket, failed-job, and health requests to the API service.

The first sentence-transformer load may download model artifacts and can take longer than subsequent starts.

## Gmail OAuth Setup

1. In Google Cloud, enable the Gmail API.
2. Configure an OAuth consent screen.
3. Create a Web application OAuth client.
4. Register the exact redirect URI configured by `GOOGLE_REDIRECT_URI`, for example:

   ```text
   http://localhost:8000/auth/google/callback
   ```

5. Configure `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `GOOGLE_OAUTH_STATE_SECRET`, and `GMAIL_CREDENTIALS_ENCRYPTION_KEY`. All five are required by the OAuth endpoint; storing only the Google client ID and secret is insufficient.
6. Start authorization for the support mailbox:

   ```text
   GET http://localhost:8000/auth/google?email_address=support@example.com
   ```

7. Open the returned `authorization_url`, grant access as the support mailbox, and allow Google to redirect to the callback.
8. The callback validates the signed, time-limited state, exchanges the authorization code, and stores the mailbox credential record in PostgreSQL.

The canonical OAuth route is `GET /auth/google`; `GET /auth/login` is a compatibility redirect to it. Both the start route and mailbox cleanup route require `X-Operator-Key`.

If a row was encrypted with a stale or unknown Fernet key, the ciphertext cannot be recovered by changing the key. With the correct historical key, migrate or reauthorize normally. Without it, delete only the affected row from `gmail_mailboxes` using the authenticated `DELETE /auth/google/mailbox?email_address=...` endpoint, then authorize the mailbox again with the active key. Do not use the old `gmail_mailbox_store` table name; the model and migration use `gmail_mailboxes`.

## Gmail Pub/Sub Ingestion

Configure Gmail `watch` and a Google Pub/Sub push subscription targeting:

```text
POST https://your-api.example.com/webhooks/gmail
```

The endpoint decodes the Pub/Sub envelope, finds the durable history cursor for the notified mailbox, requests changed message IDs through Gmail History API, retrieves new messages, and advances the cursor. The first notification initializes the cursor instead of replaying the entire mailbox history.

Important operational requirements:

- Renew Gmail watch registrations before they expire.
- Gmail and Pub/Sub delivery are at least once; retain the incoming `gmail_message_id` uniqueness constraint.
- Validate Pub/Sub OIDC issuer, audience, and service-account identity before public production exposure. Envelope validation alone is not authentication.
- Define and test cursor recovery procedures for Gmail history expiration and partial batch failures.

## Processing and Routing

For every new Gmail message, the workflow:

1. Parses and validates sender, recipient, body, Gmail IDs, and RFC threading headers.
2. Requests structured category, priority, sentiment, product, and confidence output from Groq.
3. Resolves an existing ticket by Gmail thread ID or RFC references, or creates a new ticket.
4. Persists the source message idempotently.
5. Searches the FAISS FAQ index using the configured embedding model.
6. Applies deterministic routing thresholds and safety rules.
7. Persists the routing decision and enqueues one external action.

Safe and grounded messages enqueue `gmail_send`. The worker loads the mailbox OAuth credentials at execution time, keeping secrets out of Celery payloads. Unsafe or insufficiently grounded messages enqueue `slack_escalation`. External failures use bounded exponential retries, and terminal failures are recorded in PostgreSQL.

## Ticket API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Verify API and database health |
| `GET` | `/failed-jobs` | List failed jobs; requires `X-Operator-Key` and supports status, limit, and offset filters |
| `GET` | `/failed-jobs/{failed_job_id}` | Inspect one failed job; requires `X-Operator-Key` |
| `POST` | `/failed-jobs/{failed_job_id}/replay` | Replay an eligible terminal failure; requires `X-Operator-Key` |
| `GET` | `/tickets` | List tickets; supports status, limit, and offset filters |
| `GET` | `/tickets/{ticket_id}` | Return ticket details, history, and agent decisions |
| `PATCH` | `/tickets/{ticket_id}` | Update mutable ticket fields and record audit history |
| `GET` | `/tickets/{ticket_id}/history` | Return the ticket audit trail |
| `POST` | `/tickets/{ticket_id}/decisions` | Record a human agent decision and corresponding audit entry |
| `POST` | `/webhooks/gmail` | Receive Gmail Pub/Sub notifications |
| `GET` | `/auth/google` | Start support-mailbox OAuth authorization |
| `GET` | `/auth/google/callback` | Validate state and persist exchanged credentials |
| `POST` | `/dev/test-email` | Exercise classification in non-production environments |

Interactive API documentation is served at `/docs` and `/redoc`.

Failed-job replay accepts only records in `failed` or `replay_failed` state whose correlated outbound delivery is still `failed`. It atomically resets the delivery to `queued`, resets attempts to zero, assigns a new Celery task ID, and publishes to the queue for the stored job type. Duplicate replay attempts return `409`. Records in `uncertain` state cannot be replayed automatically because the provider may already have completed the action. Broker publication failures are persisted as `replay_failed` and can be retried after broker recovery.

## Operations Dashboard

The dashboard provides:

- Ticket queue and status filtering.
- Text search across ticket fields.
- Ticket detail, customer message, classification, and confidence display.
- Status, priority, and assignee updates.
- Human decision and rationale capture.
- Combined ticket-history and decision timeline.

Backend and frontend status/priority contracts share the following values:

- Status: `open`, `in_progress`, `auto_resolved`, `escalated`, `closed`.
- Priority: `low`, `medium`, `high`, `critical`.

## Verification

Backend checks from the repository root:

```cmd
backend\.venv\Scripts\ruff.exe check backend\app backend\tests
backend\.venv\Scripts\python.exe -m compileall -q backend\app backend\tests
backend\.venv\Scripts\python.exe -m pytest backend
```

Frontend checks:

```cmd
cd frontend
npm ci
npm test
npm run lint
npm run build
npm audit --omit=dev
```

Container configuration check when Docker is installed:

```cmd
docker compose config --quiet
```

Current local verification result on August 14, 2026:

- Backend: repository-wide Ruff and compilation passed; all 144 pytest tests passed. One non-failing LangGraph pending-deprecation warning remains.
- Frontend: ESLint and the TypeScript/Vite production build passed; the production dependency audit reported zero vulnerabilities. `npm test` found no frontend test files and exited with status 1, so no frontend unit-test suite is currently verified.
- Containers: Compose configuration validation and backend/frontend image builds passed. On a clean isolated Compose project, PostgreSQL, Redis, API, frontend, Gmail worker, and Slack worker all reached healthy state. Alembic reported revision `0001` at head with no schema drift.
- Live replay: Nginx-to-API authentication returned `401` without the operator key; authenticated list and detail returned the seeded failure; replay published exactly one task to `gmail_outbound`; the durable delivery moved to `queued` with attempts reset to zero; duplicate replay returned `409`; and the Gmail worker consumed the task and persisted its controlled terminal result.
- Existing local data: the default Compose PostgreSQL volume contains an older unmanaged schema without `outbound_deliveries`. The migration guard correctly refused to stamp it. Verification used a separate clean project and did not alter that volume; migrate or replace the legacy volume deliberately before using the default project with this revision.

## Continuous Integration

GitHub Actions runs three jobs:

1. Backend Ruff, Python compilation, and pytest.
2. Frontend ESLint, TypeScript/Vite build, and production dependency audit.
3. Backend image build, frontend image build, and Docker Compose configuration validation after both code jobs pass.

See `.github/workflows/ci.yml` for the executable pipeline.

## Security and Production Readiness

Do not commit `.env` files, OAuth grants, refresh tokens, Slack webhooks, or provider API keys. If a secret is ever printed in logs, issue output, chat output, or repository history, treat it as compromised and rotate or revoke it immediately. Removing the text from the latest file is not sufficient.

Before production exposure, complete these gates:

- Add role-based authorization to the dashboard, ticket mutations, and OAuth bootstrap routes; operator-key authentication currently protects dashboard APIs and failed-job operations.
- Authenticate Google Pub/Sub push requests with validated OIDC claims.
- Encrypt persisted Gmail OAuth credentials with managed envelope encryption or an equivalent control.
- Define cursor advancement semantics for partial Gmail history batch failures.
- Make mailbox upserts concurrency-safe for simultaneous first authorization or notification events.
- Build or distribute the FAISS index as a deterministic deployment artifact.
- Add structured logs, provider-safe error handling, metrics, alerts, and watch renewal.
- Perform end-to-end staging tests against non-production Gmail, Pub/Sub, Groq, Slack, PostgreSQL, and Redis resources.

The implementation is a strong deployable foundation, but the gates above are deliberately documented rather than represented as already completed controls.

## Google Cloud Deployment

The full production target, network topology, service sizing, secret strategy, observability plan, recovery procedures, CI/CD flow, and production-readiness gates are documented in `docs/google-cloud-production-architecture.md`. Image builds are defined in `cloudbuild.yaml`. Image builds do not deploy Cloud Run or map Secret Manager values; deploy the service with explicit `--set-secrets` and `--add-cloudsql-instances` flags, or equivalent infrastructure as code.

The proposed topology uses:

- Cloud Run services for the frontend and request-driven FastAPI API.
- GKE Autopilot Deployments for continuously running Gmail and Slack Celery workers.
- Cloud SQL for PostgreSQL with regional high availability.
- Memorystore for Redis as the Celery transport.
- A global external Application Load Balancer, Cloud DNS, and Google-managed certificates for public HTTPS and same-origin path routing.
- Secret Manager and IAM service accounts for credentials and workload identity.
- Cloud Logging, Cloud Monitoring dashboards, and alerting.
- Artifact Registry and Cloud Build for immutable container releases.

## License

No license has been declared. Add an explicit license before distributing the project outside its intended organization.
