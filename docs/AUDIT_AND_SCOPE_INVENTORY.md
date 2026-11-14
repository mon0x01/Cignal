# Cignal — Repository Audit & Scope Inventory (Phase 0)

## 1. Executive Audit Summary

A comprehensive line-by-line audit of the Cignal repository was performed against the Master Implementation Directive and `PROJECT_SCOPE.md`.

Baseline health:
- **Python Compilation**: All modules compile clean with zero syntax or import errors.
- **Static Code Analysis**: `ruff check app tests migrations scripts` passed with zero errors.
- **Security Vulnerability Scan**: `bandit -q -r app -lll` passed with zero high-severity findings.
- **Unit and Integration Test Suite**: 302/302 tests passed in 21.23 seconds.
- **Database Schema**: 22 Alembic migrations current and valid.
- **Git State**: Local worktree clean, synchronized with `main` at `ed174a1`, and upstream tracking established on `origin/agent/cignal-completion`.

---

## 2. Component Inventory & Classification

Every module, endpoint, model, and workflow has been classified according to the directive's five categories:
- **Keep**: Directly supports approved scope.
- **Refactor**: Useful but architecturally or operationally misaligned.
- **Retire/Remove**: Obsolete or outside the approved product scope.
- **Future/Disabled**: Reserved for future authorized phases; kept inactive in the pilot.
- **Blocked**: Requires external legal, source, or infrastructure authorization.

| Component / File | Classification | Rationale & Target Action |
|---|---|---|
| **`app/api/main.py` (Auth & MFA)** | **Keep** | Secure password hashing (Argon2id), JWT access/refresh token rotation, TOTP MFA, and rate limiting directly support platform security. |
| **`app/api/main.py` (Report Submission)** | **Refactor** | Refactor to enforce privacy-by-default for citizen reports, generate cryptographically random tracking codes treated as bearer credentials, and prevent public exposure of raw citizen reports. |
| **`app/api/main.py` (Report Review `/review`)** | **Retire/Remove** | Obsolete legacy model where administrators/analysts approved reports for truth. Remove from active routing. Community publication is automated via corroboration thresholds. |
| **`app/api/main.py` (Legacy Incidents `/incidents`)** | **Future/Disabled** | Operational incidents are reserved for verified responder organizations. Disable public incident modification. Replace public API surface with Public Events API. |
| **`app/api/incident_operations.py`** | **Future/Disabled** | Responder action management and approval gates. Retain schema for future responder integration; keep endpoints disabled for public pilot. |
| **`app/api/response_plans.py`** | **Future/Disabled** | Playbook orchestration for emergency responders. Retain models and schema; keep disabled in active public release. |
| **`app/api/main.py` (Public Events API)** | **Keep (New)** | New public endpoints for event discovery, live map data, event feeds, structured community updates, and anonymous tracking. |
| **`app/api/main.py` (Integrations)** | **Refactor** | Connector status and manual triggers refactored to enforce Source Registry authorization. Only approved sources can be enabled by administrators. |
| **`app/api/main.py` (Background Jobs Outbox)** | **Keep** | Outbox inspection and retry endpoints directly support administrative operations and reliability. |
| **`app/api/main.py` (Research Datasets)** | **Keep** | Governed active-learning dataset management and model evaluations. Ensure citizen reports are not automatically enrolled in training sets. |
| **`app/api/main.py` (Users & Audit)** | **Refactor** | User administration: remove `analyst` role creation from active product; enforce administrator-only technical maintenance boundaries. |
| **`app/api/main.py` (Notifications & Push)** | **Refactor** | Refactor to support district, radius, and hazard subscriptions; forbid notifications on single uncorroborated reports; maintain distinction between official alerts and community events. |
| **`app/db/models.py` (User, Outbox, RefreshToken)** | **Keep** | Core security and transactional outbox infrastructure. |
| **`app/db/models.py` (Report, Evidence)** | **Refactor** | Add fields for secure tracking code hashing, compression metadata, and privacy redaction. |
| **`app/db/models.py` (EventHypothesis, Snapshot)** | **Refactor** | Enhance with community corroboration fields (contributor count, source independence, publication status, stale state decay). |
| **`app/db/models.py` (Incident, ResponsePlan, Action)** | **Future/Disabled** | Keep existing models to prevent database migration or historical data breakage; isolate from active public flows. |
| **`app/db/models.py` (CommunityEventUpdate)** | **Keep (New)** | Model for structured, immutable citizen updates to public corroborated events. |
| **`app/integrations/registry.py` & `source_catalog.py`** | **Refactor** | Implement Source Registry with explicit authorization tracking, license and attribution requirements, and Gujarat-specific connector scoping. |
| **`app/integrations/usgs.py`** | **Keep** | Public scientific earthquake data; operational. |
| **`app/integrations/open_meteo.py`** | **Keep** | Weather forecast and atmospheric alerts; non-commercial permitted use. |
| **`app/integrations/gdacs.py`** | **Keep** | Global multi-hazard alerts; operational with attribution. |
| **`app/integrations/eonet.py`** | **Keep** | Natural event metadata with source linking; operational. |
| **`app/integrations/sachet_cap.py`** | **Keep** | NDMA Sachet CAP protocol parser; critical for Gujarat/India official alerts. |
| **`app/integrations/india.py` (IMD, Bhuvan, IndiaGov)** | **Blocked** | Awaiting authorized production API tokens and verified license agreements. Kept disabled until explicitly approved. |
| **`app/integrations/firms.py`** | **Blocked** | Requires project-owner authorized NASA FIRMS MAP_KEY. Kept disabled until approved. |
| **`app/integrations/social_api.py`** | **Blocked** | Kept disabled until specific licensed provider terms and endpoints are approved. |
| **`app/intelligence/analyzer.py`** | **Refactor** | Significantly expand Gujarati and English NLP: language identification, text normalization, multi-label hazard taxonomy, urgency vs severity, impact and help extraction, Gujarat geographical entities. |
| **`app/intelligence/event_hypotheses.py`** | **Refactor** | Implement corroboration engine: mandatory 2-contributor floor, source independence check, syndication detection, contradictory evidence dampening, stale event decay. |
| **`app/intelligence/vision.py`** | **Refactor** | File integrity, EXIF scrubbing, image deduplication via perceptual hashing, and safe file compression. Advanced visual ML remains disabled until representative licensed datasets are secured. |
| **`app/intelligence/pipeline.py`** | **Keep** | Multimodal analysis orchestration. |
| **`app/services/job_outbox.py` & `job_execution.py`** | **Keep** | Transactional outbox with lease expiration and execution fences. |
| **`app/services/event_discovery_schedule.py`** | **Refactor** | Scheduled discovery worker: align with continuous corroboration threshold and decay rules. |
| **`app/services/notifications.py`** | **Refactor** | Subscription-based routing (district/radius/hazard) and uncorroborated single-report notification blocks. |
| **`app/services/incident_review.py`** | **Retire/Remove** | Obsolete analyst review service. Retire from active execution. |
| **`app/ui/main.py`** | **Refactor** | Redesign completely to provide public disaster-intelligence interface: Overview, Live Map, Public Feed, Event Details, Submit Report, Track My Report, and Notifications. |
| **`app/ui/components/layout.py`** | **Refactor** | Apply approved minimal dark palette (`#0E1726`, `#26384D`, `#B9D2E8`), remove analyst navigation, add language toggle (Gujarati / English). |
| **`app/ui/static/*` (PWA)** | **Refactor** | Enhance PWA service worker with offline draft storage, reconnection sync, and Web Push notifications. |
| **Docker Compose & Services** | **Keep** | Keep modular architecture: `api`, `frontend`, `db` (PostGIS), `redis`, `worker`, `worker-bulk`, `scheduler`. |

---

## 3. Immediate Action Plan

1. **Phase 1 (Foundations & Roles)**: Retire Analyst role from active product, update permission boundaries for Administrator, ensure historical migration compatibility, and add migration `0023` for public event intelligence.
2. **Phase 2 (Citizen Reporting & Privacy)**: Implement private tracking code bearer security, structured submission, evidence compression, and privacy-safe citizen updates.
3. **Phase 3 (Data-Source Platform)**: Formalize Source Registry, enforce authorization gates, keep unapproved connectors inactive.
4. **Phase 4 (Public Event Intelligence)**: Corroboration threshold engine (2-contributor floor, source independence, state lifecycle).
5. **Phase 5 (AI/ML NLP & Vision)**: Gujarati + English NLP, Gujarat administrative entity extractor, calibrated scoring, EXIF stripping, and safe compression.
6. **Phase 6 (Public Web & PWA)**: Redesign NiceGUI UI with approved palette, Gujarati/English bilingual support, responsive public pages, and offline PWA drafts.
7. **Phase 7 (System Administration)**: Administrator-only technical maintenance workspace.
8. **Phase 8 (Testing & Validation)**: Full test execution, verification scripts, and comprehensive documentation update.
