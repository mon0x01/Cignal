# Cignal

**Detect signals. Understand the crisis. Inform the response.**

Cignal is a self-hosted, modular disaster-intelligence and decision-support application. It keeps three concepts separate:

- **Report** — a submitted observation or external-source signal.
- **Evidence** — a file attached to a report.
- **Incident** — a real-world event an authorised analyst has verified and associated with one or more reports.

AI outputs are advisory triage hints. Cignal does not autonomously declare emergencies, dispatch responders, or replace official authorities.

## What is included

- Python-authored NiceGUI browser interface and FastAPI backend.
- PostgreSQL + PostGIS, Redis, RQ background worker and scheduler.
- Role-based access control: administrator, analyst, responder and citizen.
- Mandatory TOTP MFA by default, recovery codes, Argon2id password hashing, short-lived JWT access tokens and rotating refresh tokens.
- Public anonymous report submission and authenticated citizen reporting.
- Report triage, candidate duplicate matching, analyst verification/rejection/request-for-information, and report-to-incident links.
- Append-only incident activity timeline for responder observations, situation updates, resource requests, access constraints and handovers.
- Incident Operations Control Plane: durable assigned actions, priority/due dates, optimistic concurrency, append-only task history and explicit approval gates for public warnings, evacuation, dispatch and medical intervention.
- Versioned incident response coordination playbooks with idempotent plan creation, prerequisite-gated task sequencing, progress states and immutable plan snapshots; these coordinate human work without issuing warnings or dispatching resources.
- Evidence upload validation, image re-encoding to strip EXIF metadata, local storage and interchangeable S3-compatible adapter.
- Operational dashboard, incident map, report review, submission, analytics, source status, administration, audit log and settings pages.
- Public-data connectors for USGS earthquakes and GDACS alerts; configurable connectors for IMD, NASA FIRMS, permitted RSS and authorised social APIs; Open-Meteo weather context.
- Historical CSV import with provenance labels.
- Optional multilingual sentence-transformer NLP and optional generic MobileNet image context model.
- Cross-source event hypotheses with complete-link matching, optional multilingual embeddings, event-state history and source operation/review profiles.
- Offline, event-group-disjoint training/evaluation workflow for a supervised event-pair ranker, benchmarked against Cignal's existing heuristic scorer.
- In-app notifications, optional SMTP email and optional Web Push.
- PWA manifest/service worker and a limited anonymous-report offline draft bridge.
- Docker Compose, Caddy reverse proxy, scheduled data retention and daily database/evidence backups.
- Unit and API workflow tests, operational scripts and architecture/security documentation.

## Important scope boundary

This ZIP is the **functional first release foundation**, not a claim that every long-term research capability is validated for operational emergency response. The bundled CPU-friendly text classifier learns from a small built-in multilingual seed set; its confidence score is not calibrated. The optional MobileNet model is a generic ImageNet image classifier, not a disaster-damage model. The optional multilingual sentence-transformer improves semantic text similarity but is not a disaster-specific classifier. Historical trend charts describe stored records; predictive risk modelling is not included as a validated capability.

Several real integrations need user-provided credentials or endpoint details. Cignal reports source provenance and does not silently substitute simulated records for missing live data.

Event Intelligence v2's supervised model is an extensible training pipeline, not a claim of model superiority. No real-world labelled event-pair corpus is bundled; synthetic fixtures are software smoke tests only. Optional multilingual embeddings are disabled by default and load only from locally staged model files. See `docs/EVENT_INTELLIGENCE_V2.md` and `docs/INCIDENT_OPERATIONS_V1.md`.

## Requirements

Recommended:

- Arch Linux x86_64.
- Docker Engine and Docker Compose v2.
- 16 GB RAM; CPU-only operation supported.
- Internet access to pull container images and install Python dependencies.
- Enough disk for Postgres, Redis, model caches, evidence files and backups. Leave additional headroom beyond the application's files.

Docker is root-equivalent on a conventional rootful setup. The Arch Wiki warns that membership in the `docker` group grants root-equivalent control. Use `sudo docker ...` unless you have consciously accepted that trade-off. See [Arch Wiki: Docker](https://wiki.archlinux.org/title/Docker).

## Quick start on Arch Linux

### 1. Install and start Docker

```bash
sudo pacman -S docker docker-compose
sudo systemctl enable --now docker.service
sudo docker info
sudo docker compose version
```

### 2. Configure secrets

```bash
cd Cignal
cp .env.example .env
openssl rand -hex 32
openssl rand -hex 32
```

Put the two generated values into `APP_SECRET_KEY` and `JWT_SECRET_KEY` in `.env`. Back up `APP_SECRET_KEY` securely: it encrypts the MFA secrets at rest, and losing it prevents existing authenticator secrets from being decrypted. Generate a separate database password and Redis password using `openssl rand -hex 24`. Update `POSTGRES_PASSWORD` and `REDIS_PASSWORD`; keep the database password consistent with the `DATABASE_URL` line if you edit that line manually. The Compose file constructs its internal connection URLs from `POSTGRES_PASSWORD` and `REDIS_PASSWORD`.

The secure default is `DEMO_MODE=false`, which enables the one-time administrator setup wizard. For a disposable demonstration only, set `DEMO_MODE=true` and `APP_ENV=development` before the first startup to seed the four demonstration accounts. Protect the environment file:

```bash
chmod 600 .env
mkdir -p data/backups data/evidence
sudo chown -R 10001:10001 data/backups
```

For a non-demo installation, set `DEMO_MODE=false` and `APP_ENV=production`. The first visit then presents a one-time administrator setup wizard. Production startup refuses placeholder, short or reused application/JWT secrets, demo mode, and disabled MFA or fail-closed rate limiting. Keep `MFA_REQUIRED=true` and `RATE_LIMIT_FAIL_CLOSED=true`.

### 3. Build and start the stack

```bash
sudo docker compose up --build -d
sudo docker compose ps
sudo docker compose logs -f api frontend worker scheduler
```

Wait for the database and API health checks to become healthy. Open:

- Frontend: <http://localhost:8080>
- API health: <http://localhost:8000/api/health>
- Interactive API docs in development: <http://localhost:8000/api/docs>

The API and frontend are separate containers. The browser-facing UI uses the internal API address server-side. For the optional offline draft and push browser bridges during direct localhost use, `API_PUBLIC_URL` in `.env` should be `http://localhost:8000` (the example value). `CORS_ORIGINS` already includes both localhost UI origins.

### 4. Demonstration accounts

Only when `DEMO_MODE=true` and the database has no users, the first startup seeds these demonstration accounts:

| Role | Email |
|---|---|
| Administrator | `admin@cignal.demo` |
| Analyst | `analyst@cignal.demo` |
| Responder | `responder@cignal.demo` |
| Citizen | `citizen@cignal.demo` |

Shared demo-only password: `Cignal-Demo-2026!`

**Every authenticated demo account must enrol an authenticator at first sign-in.** Use Aegis, 2FAS, Google Authenticator or another TOTP-compatible app. Save the one-time recovery codes offline. These demo credentials must never be used for a public or production deployment. Set `DEMO_MODE=false` and recreate the database before exposing a real installation.

### 5. Optional public preview without purchasing a domain

A free Cloudflare Quick Tunnel can provide a temporary HTTPS URL without a Cloudflare account or domain. It is for testing, not stable hosting: the hostname is random, changes between runs, and stops when the tunnel process stops. The current Cloudflare documentation also states a 200 in-flight request limit and no uptime guarantee for Quick Tunnels. Anyone with the link can reach the application unless you add an access restriction.

First switch the browser API URL to same-origin mode in `.env`:

```dotenv
API_PUBLIC_URL=
```

Then start the local reverse proxy:

```bash
sudo docker compose --profile edge up -d caddy
```

Install `cloudflared` using its current official instructions, then run:

```bash
cloudflared tunnel --url http://localhost:8081
```

Open the `https://*.trycloudflare.com` URL printed in the terminal. Keep the tunnel process running. The tunnel routes `/api/*` to FastAPI and all other paths to NiceGUI through Caddy. Do not publish database, Redis, API or frontend ports directly to the public internet.

Cloudflare documents Quick Tunnels as temporary development/testing access, not production hosting: [Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/). A stable public service needs a named tunnel and a domain, or another controlled HTTPS ingress arrangement. A free Quick Tunnel is not a substitute for production hosting, access controls or backups.

### 6. Stop or inspect

```bash
sudo docker compose down
sudo docker compose logs --tail=200 api
sudo docker compose ps
```

`docker compose down` preserves named volumes. `docker compose down -v` deletes Postgres/Redis/evidence volumes and is destructive; do not use it casually.

## Common operations

```bash
# Follow API logs
sudo docker compose logs -f api

# Run the test suite inside the API image
sudo docker compose exec api pytest -q

# Trigger one-off source ingestion from the UI or API
# The UI has a "Run ingestion now" control for administrator/analyst roles.

# Create an immediate verified backup
sudo ./scripts/backup.sh

# Validate a backup by restoring its database into a temporary database
sudo ./scripts/test_restore.sh data/backups/cignal-backup-YYYYMMDDTHHMMSSZ
```

The scheduler runs ingestion periodically, performs the configured retention task once daily, and creates a daily Postgres/evidence backup. Backups are stored under `data/backups`; make sure that folder is writable by container UID `10001`, and copy backups off the host for disaster recovery.

See [Operations](docs/OPERATIONS.md), [Security](docs/SECURITY.md), [Evidence integrity](docs/EVIDENCE_INTEGRITY.md), [Incident activity](docs/INCIDENT_ACTIVITY.md), [Integrations](docs/INTEGRATIONS.md), [Architecture](docs/ARCHITECTURE.md), [API](docs/API.md), [Testing](docs/TESTING.md) and [Known limitations](docs/LIMITATIONS.md).

## License and data policy

The source code in this project is provided under the MIT License. Each external dataset/API has its own terms, rate limits, attribution and permitted-use rules. The operator is responsible for reviewing current provider terms before enabling an integration or exposing the service to a public audience. Do not ingest private social data, bypass platform restrictions, or use feeds you are not authorised to process.

## AI and multimodal intelligence

See [AI and Multimodal Intelligence](docs/AI_INTELLIGENCE.md) for the NLP, image evidence, geospatial fusion, model training and safety boundaries.

## Research datasets and model development

Cignal includes a governed event-pair dataset workflow. See
[`docs/RESEARCH_DATA_ENGINE_V1.md`](docs/RESEARCH_DATA_ENGINE_V1.md) for dataset
registration, analyst labelling, privacy review, immutable version sealing,
checksummed JSONL export and model training/evaluation commands. Dataset
licensing and legal-basis fields are operator-supplied governance metadata, not
legal approval. Regex PII redaction is limited; human privacy review is required.


## Research & Dataset Governance Engine v1

Cignal includes an admin/analyst workflow for registering research datasets,
recording reviewed event-pair labels and provenance, completing privacy review,
sealing immutable group-disjoint dataset versions, and exporting checksummed
JSONL plus a governance manifest into the shared model volume. See
`docs/RESEARCH_DATA_ENGINE_V1.md`.


Background-job delivery guarantees and operational procedures: `docs/BACKGROUND_JOB_OUTBOX_V1.md`.


### Continuous event discovery

Cignal can automatically schedule bounded cross-source event-hypothesis discovery
after analyzed reports from multiple independent source origins settle. Scheduling
is debounced and idempotent, and dispatch uses the durable background-job outbox.
The result is an unverified candidate that requires analyst review; it is not an
automatic incident or emergency response. Configuration is documented in
`.env.example` and `docs/CONTINUOUS_EVENT_DISCOVERY_V1.md`.
