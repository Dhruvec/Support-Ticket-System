# Google Cloud Production Architecture

## Scope

This document defines the Google Cloud production target for the support ticket platform. It maps the React dashboard, FastAPI API, Celery workers, PostgreSQL database, Redis broker, Gmail integration, Groq classification, Slack escalation, and FAISS retrieval components onto managed Google Cloud services.

The target uses Cloud Run for request-driven HTTP services, GKE Autopilot for continuously running Celery consumers, Cloud SQL for PostgreSQL, Memorystore for Redis, Artifact Registry for images, Secret Manager for credentials, and Cloud Logging and Monitoring for operations.

## Service Mapping

| Application concern | Google Cloud service |
|---|---|
| Container images | Artifact Registry |
| Public API | Cloud Run |
| React dashboard | Cloud Run |
| Celery Gmail and Slack consumers | GKE Autopilot |
| PostgreSQL | Cloud SQL for PostgreSQL |
| Redis broker and result backend | Memorystore for Redis |
| HTTPS entry point and path routing | Global external Application Load Balancer with serverless NEGs |
| DNS and certificates | Cloud DNS and Google-managed SSL certificates |
| Secrets | Secret Manager |
| Container and audit logs | Cloud Logging |
| Metrics, dashboards, and alerts | Cloud Monitoring |
| Image build | Cloud Build using `cloudbuild.yaml` |
| Gmail notifications | Pub/Sub authenticated push subscription |

Cloud Run is not used for the Celery consumers. A Cloud Run service receives CPU only while processing requests by default and cannot reliably maintain an always-running Redis consumer. GKE Autopilot supplies continuously allocated worker pods without requiring node management.

## Runtime Topology

```text
Internet
  |
Cloud DNS + Google-managed certificate
  |
Global external Application Load Balancer + Cloud Armor
  |--------------------------------------|
  |                                      |
/*                                     API paths
  |                                    /tickets*, /failed-jobs*, /health,
Dashboard Cloud Run                   /ready, /webhooks/*, /auth/*
Nginx + React                           |
                                    API Cloud Run
                                         |
                          Serverless VPC Access / Direct VPC egress
                                         |
                          |--------------|---------------|
                          |                              |
                   Cloud SQL PostgreSQL          Memorystore Redis
                          |                              |
                          |                         GKE Autopilot
                          |                    gmail-worker / slack-worker
                          |                              |
                          |                       Gmail / Slack APIs
                          |
                 tickets, messages, history,
                 failed jobs, outbound delivery,
                 encrypted Gmail credentials
```

Keep Cloud SQL, Memorystore, and worker pods on private addresses. Configure Cloud Run VPC egress so the API can reach both private services. The public load balancer is the only supported internet entry point.

## Prerequisites

Install and authenticate the Google Cloud CLI, select a project, and use one region consistently:

```cmd
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
gcloud config set run/region us-central1
```

Set shell variables using the syntax appropriate for the local shell. Examples below use placeholders so no production identifiers or secrets are committed.

Enable the required APIs:

```cmd
gcloud services enable artifactregistry.googleapis.com cloudbuild.googleapis.com run.googleapis.com sqladmin.googleapis.com redis.googleapis.com secretmanager.googleapis.com compute.googleapis.com container.googleapis.com monitoring.googleapis.com logging.googleapis.com pubsub.googleapis.com iamcredentials.googleapis.com vpcaccess.googleapis.com
```

## Artifact Registry And Builds

Create the Docker repository once:

```cmd
gcloud artifacts repositories create support-ticket-agent --repository-format=docker --location=us-central1 --description="Support ticket application images"
```

Build and push both images with the checked-in Cloud Build definition:

```cmd
gcloud builds submit --config cloudbuild.yaml --substitutions=_REGION=us-central1,_REPOSITORY=support-ticket-agent .
```

`cloudbuild.yaml` tags images with Cloud Build's immutable build ID, which is available to both manual submissions and triggers. Deploy the exact resulting tag or digest. Do not use `latest` as the only production release identifier.

## Networking

Create a custom-mode VPC with a regional application subnet. Reserve private service access for Cloud SQL and Memorystore. Use Direct VPC egress for Cloud Run where available; otherwise create a Serverless VPC Access connector in the same region.

Required paths are:

- Load balancer to Cloud Run through serverless network endpoint groups.
- API Cloud Run and GKE workers to Cloud SQL on TCP 5432 over private IP.
- API Cloud Run and GKE workers to Memorystore on TCP 6379 or the selected TLS port.
- API and workers to Gmail, Groq, Slack, and Google APIs over TCP 443.
- No public IP on Cloud SQL or Memorystore.

Use Cloud NAT for controlled GKE pod egress when the private cluster has no external node addresses. Apply hierarchical firewall policy and VPC firewall rules using service accounts or network tags where supported.

## Cloud SQL

Provision Cloud SQL for PostgreSQL 16 with:

- Regional high availability for production.
- Private IP only.
- Automated backups, point-in-time recovery, and deletion protection.
- At least seven days of retained backups, adjusted to the organization's retention policy.
- Data encryption with a customer-managed Cloud KMS key where policy requires it.
- Query Insights and connection monitoring.
- TLS enforcement.

Provide a SQLAlchemy URL in the `DATABASE_URL` secret, for example:

```text
postgresql+psycopg://APP_USER:ENCODED_PASSWORD@PRIVATE_IP:5432/support_tickets?sslmode=require
```

URL-encode credentials before constructing the URL. Size SQLAlchemy pools against the combined maximum Cloud Run instances and worker replicas. Set an explicit Cloud Run maximum instance count to protect the database connection budget. Use a connection pooler only after measuring connection pressure.

Run Alembic once before each application rollout. Do not run migrations in every Cloud Run instance. A Cloud Run Job using the backend image can execute:

```text
python -m app.database.migrate
```

Give the migration job the same VPC path, database secret, and runtime service account as the API, then require its successful execution before deploying new revisions.

## Memorystore

Provision a Standard Tier Memorystore for Redis instance with a replica and automatic failover. Enable in-transit encryption and authentication where supported by the selected service tier, and store the complete `rediss://` connection URL in Secret Manager as `REDIS_URL`.

The production configuration validator requires `rediss`. Confirm Celery connectivity and certificate behavior in staging before cutover. Monitor memory use, evictions, connected clients, replication health, and rejected connections.

Redis transports work but is not the system of record. Durable delivery status, retries, and failures remain in PostgreSQL. After a Redis recovery, reconcile queued work against PostgreSQL state.

## Secrets And IAM

Create separate user-managed service accounts for:

- API Cloud Run service.
- Dashboard Cloud Run service.
- Gmail worker Kubernetes service account.
- Slack worker Kubernetes service account.
- Migration Cloud Run Job.
- Pub/Sub push authentication.
- Cloud Build deployment automation.

Use Workload Identity Federation for GKE so pods do not carry service-account keys. Grant each service account only the Secret Manager secret accessor roles and service permissions it needs. The dashboard must not receive backend secrets.

Store these values in Secret Manager:

- `DATABASE_URL`.
- `REDIS_URL`.
- `OPERATOR_API_KEY`.
- `GROQ_API_KEY`.
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
- `GOOGLE_OAUTH_STATE_SECRET`.
- `GMAIL_CREDENTIALS_ENCRYPTION_KEY`.
- `SLACK_WEBHOOK_URL`.

Set these as ordinary runtime configuration:

- `APP_ENV=production`.
- `LOG_LEVEL`.
- `GROQ_MODEL` and `EMBEDDING_MODEL`.
- `FAQ_SOURCE_PATH` and `FAQ_INDEX_PATH`.
- `GOOGLE_REDIRECT_URI`.
- `GOOGLE_PUBSUB_AUDIENCE`.
- `GOOGLE_PUBSUB_SERVICE_ACCOUNT`.
- Routing thresholds and retry settings.

Pin secret versions for controlled releases or document an intentional `latest` rotation process. Never put production secret values in Cloud Build substitutions, image build arguments, Kubernetes manifests, or source control.

## API Cloud Run Service

Deploy the backend image with:

- Internal-and-Cloud-Load-Balancing ingress.
- Authentication required at the Cloud Run service boundary, with the load-balancer path configured according to the chosen serverless NEG authentication model.
- VPC egress to private Cloud SQL and Memorystore addresses.
- A dedicated runtime service account.
- At least one warm instance when webhook latency is important.
- Concurrency and maximum instances sized against database connections and model memory.
- Startup and liveness checks using `/ready` and `/health` as appropriate.
- Secret Manager references for sensitive environment variables.

The backend image honors Cloud Run's injected `PORT`. The image includes the sentence-transformer runtime, so measure cold-start memory and latency in staging. Build the FAISS index as an immutable image artifact before production or download a versioned artifact from Cloud Storage during a controlled startup step.

## Dashboard Cloud Run Service

Deploy the frontend image as a separate Cloud Run service. It honors the injected `PORT`. Set `API_UPSTREAM` to a syntactically valid backend URL; normal production browser API traffic should be routed directly to the API serverless NEG by the external load balancer, preserving the same-origin contract.

Configure the load balancer URL map with API-owned paths before the frontend catch-all:

- `/tickets*`.
- `/failed-jobs*`.
- `/health` and `/ready`.
- `/webhooks/*`.
- `/auth/*`.
- `/docs*`, `/redoc*`, and `/openapi.json` if intentionally enabled outside production.
- All remaining paths to the dashboard service.

Use `/frontend-health` for the dashboard backend health check. Cache fingerprinted static assets at the edge while keeping `index.html` short-lived.

## Celery Workers On GKE Autopilot

Create one regional GKE Autopilot cluster with private nodes and Workload Identity Federation. Deploy the backend image in two separate Kubernetes Deployments:

```text
celery -A app.tasks:celery_app worker --loglevel=INFO --queues=gmail_outbound --hostname=gmail-worker@%h
```

```text
celery -A app.tasks:celery_app worker --loglevel=INFO --queues=slack_outbound --hostname=slack-worker@%h
```

Use at least two replicas per queue when delivery availability is business-critical. Configure pod disruption budgets, topology spread constraints, resource requests, graceful termination, and startup probes. Scale on Redis queue depth with a supported external metrics adapter or KEDA after validating that the scaler can reach Memorystore.

The Gmail worker needs database, Redis, Google OAuth, and Gmail credential encryption configuration. The Slack worker needs database, Redis, and Slack webhook configuration. Keep the queue-specific separation used by Docker Compose so one provider outage cannot occupy every worker slot.

## Gmail And Pub/Sub

Create a dedicated Pub/Sub push service account and grant the Pub/Sub service agent permission to mint identity tokens for it. Configure the push subscription endpoint as:

```text
https://support.example.com/webhooks/gmail
```

Set:

```text
GOOGLE_PUBSUB_AUDIENCE=https://support.example.com/webhooks/gmail
GOOGLE_PUBSUB_SERVICE_ACCOUNT=pubsub-push@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

The API validates the token issuer, audience, and service-account identity. Keep the subscription and API in controlled projects, grant only the required token creation and invocation permissions, and retain the database uniqueness constraint because Pub/Sub delivery is at least once.

Register the production OAuth callback exactly as:

```text
https://support.example.com/auth/google/callback
```

Renew Gmail watch registrations before expiration and alert on renewal failures. Test history expiration and cursor recovery with a non-production mailbox.

## Load Balancing, DNS, And Edge Security

Create serverless NEGs for both Cloud Run services, attach them to separate backend services, and configure the path-based URL map. Reserve a global static IP, attach a Google-managed certificate, and point the Cloud DNS record to that address.

Attach a Cloud Armor policy with managed WAF rules and rate limits for public webhook and authentication paths. Restrict OAuth bootstrap and operator APIs with an identity-aware administrative access layer. The existing shared operator key is not a substitute for user identity, authorization, or audit policy.

## Observability

Cloud Run and GKE write stdout and stderr to Cloud Logging. Preserve structured JSON and include request ID, Gmail message and thread IDs, ticket number, Celery task ID, queue, retry attempt, and provider status category without provider response bodies or secrets.

Create Cloud Monitoring dashboards and alerts for:

- Load balancer request volume, latency, 4xx, and 5xx rates.
- Cloud Run instance count, startup latency, request latency, and container failures.
- GKE worker availability, restarts, CPU, memory, and unschedulable pods.
- Cloud SQL CPU, memory, storage, connections, replication, and deadlocks.
- Memorystore memory, evictions, clients, and replication health.
- Queue depth, oldest queued-job age, retries, terminal failures, and uncertain outcomes.
- Gmail webhook volume, failures, cursor lag, and watch renewal.
- Auto-resolved versus escalated ticket counts.

Route actionable alerts through Notification Channels to the incident system. Configure log exclusions only after confirming they do not remove security, delivery, or audit events.

## CI/CD Release Flow

1. Run Ruff, compilation, pytest, ESLint, the frontend build, dependency audit, and container validation.
2. Build both images through `cloudbuild.yaml` and push immutable tags to Artifact Registry.
3. Scan images with Artifact Analysis and block releases that violate the vulnerability policy.
4. Execute the migration Cloud Run Job and require success.
5. Roll out GKE worker Deployments using the exact backend image digest.
6. Deploy the API and dashboard Cloud Run revisions using exact image digests.
7. Update serverless NEG traffic only after readiness checks pass.
8. Smoke-test `/health`, the dashboard shell, authenticated ticket reads, Pub/Sub authentication, and non-production delivery paths.
9. Promote the same image digests between environments.

Keep the previous Cloud Run revision and Kubernetes Deployment revision available for rollback. Database changes must remain backward compatible during a rolling application release.

## Environments And Recovery

Use separate Google Cloud projects for staging and production. At minimum, separate VPCs, Cloud SQL instances, Memorystore instances, GKE clusters, Cloud Run services, secrets, Pub/Sub subscriptions, OAuth clients, and Gmail test mailboxes.

Recovery procedures:

- Cloud SQL: perform point-in-time recovery into a new instance, validate it, update the database secret, run health checks, and shift workloads deliberately.
- Memorystore: fail over, then reconcile queue state against PostgreSQL durable delivery records.
- Bad Cloud Run release: move traffic to the prior healthy revision.
- Bad worker release: roll back the Kubernetes Deployment.
- Provider outage: allow bounded retries, monitor queue age, and replay only eligible failed records after recovery. Reconcile uncertain outcomes manually.
- Gmail cursor corruption: stop the affected ingestion path, reset or restore the durable cursor deliberately, replay history, and rely on Gmail message uniqueness for deduplication.

Exercise restore, failover, and rollback in staging on a schedule.

## Production Readiness Gates

Do not expose the production system until all of these are complete:

- Alembic migrations run once before rollout.
- Dashboard and mutation routes have user authentication and role-based authorization.
- OAuth bootstrap routes are restricted to trusted administrators.
- Pub/Sub OIDC validation is tested against the production audience and service account.
- Gmail OAuth credentials are encrypted with managed key material and access is audited.
- The FAISS index is a deterministic, versioned deployment artifact.
- Cloud Run and GKE scaling limits fit Cloud SQL and Memorystore capacity.
- Cloud Armor rules and rate limits have been tested without blocking Pub/Sub.
- Monitoring covers API, workers, database, Redis, Gmail watch renewal, retries, and terminal failures.
- Staging has passed Gmail, Slack, retry, failover, restore, and rollback exercises.
