# Cignal — Authoritative Project Scope & Operating Directive

## 1. Executive Summary & Product Mantra

**Product Mantra: "From public — by public — for public."**

Cignal is a public disaster-intelligence ecosystem designed to deliver actionable, community-corroborated, and authoritative-source hazard intelligence to citizens and emergency stakeholders. Cignal is neither an administrator-curated news feed nor an internal incident-dispatch system; it is a decentralized, evidence-grounded, provenance-aware public intelligence platform.

This document serves as the authoritative, binding source of truth for all architectural, functional, security, and algorithmic decisions across the Cignal platform.

---

## 2. Geographical Rollout: Gujarat Pilot

Cignal is architected **Gujarat-first**. The initial pilot targets representative districts in Gujarat before expanding statewide and nationally.

### 2.1 Candidate Pilot Districts & Selection Criteria
Candidate districts are evaluated and ranked based on multi-hazard vulnerability, population density, authoritative telemetry coverage, and operational feasibility:

| District | Primary Hazard Exposures | Authoritative Data Quality | Urban / Rural Mix | Recommended Role |
|---|---|---|---|---|
| **Ahmedabad** | Urban waterlogging, heavy rainfall, heatwave, industrial/chemical risks | High (AMC sensors, IMD station, dense telecom) | Predominantly Urban | Primary Urban Pilot (High density, high community report volume) |
| **Surat** | Tapi river flooding, tidal inundation, chemical/industrial hazard | High (SMC flood gates, IMD, CWC river gauging) | Urban / Industrial | Secondary Urban & Hydrological Pilot |
| **Kutch (Bhuj)** | Seismic risk (Zone V), cyclone landfall, drought/heatwaves | Moderate to High (ISR seismographs, IMD radar) | Rural / Semi-arid / Coastal | Primary Geophysical & Coastal Extreme Pilot |
| **Rajkot** | Flash floods, severe weather, urban infrastructure stress | Moderate to High (RMC, IMD automated stations) | Semi-urban / Saurashtra hub | Inland Regional Pilot |
| **Navsari / Valsad** | Coastal cyclone surge, heavy monsoon riverine flooding (Ambika/Purna) | Moderate (State irrigation, IMD coastal alerts) | Coastal / Semi-rural | Coastal Monsoon Pilot |

### 2.2 Geography Configuration
Pilot geography boundaries (districts, talukas, and bounding boxes) are strictly configurable in application configuration and database seeds, enabling controlled staged rollout without code modification.

---

## 3. Core Domain Concepts & Taxonomy

To maintain evidentiary integrity and public safety, four distinct concepts are separated at the database, business logic, and API layers:

1. **Citizen Report**: An individual observation submitted by a registered user or anonymous contributor. Private by default; never published directly as a standalone public event.
2. **External Observation**: A normalized record ingested from an authorized external provider (e.g., USGS, GDACS, IMD, Sachet CAP). Retains complete provenance, content hashes, retrieval timestamps, and source URLs.
3. **Public Event**: An automatically aggregated, corroborated set of related observations published to the public feed and map. Governed by strict corroboration thresholds.
4. **Operational Incident**: A restricted operational record reserved for authorized emergency responder organizations. Responder workflows remain strictly separated and disabled until authorized responder organizations formally participate.

### 3.1 Event Categories & Hazard Taxonomy
The platform supports an extensible taxonomy of hazards, rolled out progressively:
- **Floods, Heavy Rainfall & Urban Waterlogging** (Priority 1)
- **Cyclones & Severe Weather** (Priority 1)
- **Earthquakes & Seismic Activity** (Priority 1)
- **Heatwaves & Environmental Extremes** (Priority 2)
- **Fires & Building Collapses** (Priority 2)
- **Industrial Accidents & Chemical Hazards** (Priority 2)
- **Landslides & Ground Failures** (Priority 3)
- **Infrastructure Disruptions & Failures** (Priority 3)

---

## 4. User Roles & Access Control

Access control enforces strict boundary separation across four roles:

### 4.1 Citizen / Public Contributor
- Discovers public corroborated events and official-source alerts.
- Submits structured hazard reports (registered or anonymous).
- Receives private bearer tracking codes for anonymous submissions.
- Tracks submission history and status updates.
- Submits structured follow-ups / updates to existing public events.
- Configures district, radius, and category-based alert preferences.

### 4.2 Platform Administrator
Responsible exclusively for technical platform maintenance and operational governance.
- **Permitted Capabilities**: System health monitoring, background job monitoring, approved connector configuration, credential/secret reference management, user provisioning and role assignment, account suspension for abuse, security settings, audit log inspection, retention policy configuration, backup and disaster recovery execution.
- **Explicit Prohibitions**:
  - Must NOT manually create, edit, verify, or resolve public events.
  - Must NOT review or approve citizen reports for factual truth.
  - Must NOT manipulate AI confidence or match scores.
  - Must NOT autonomously approve or declare emergency status.
  - Must NOT replace community or authoritative evidence with administrative opinion.

### 4.3 Responder (Future Restricted Role)
- Dedicated workspace for verified emergency response agencies.
- Kept **disabled** in the active pilot until formal agreements and authorized agency participation exist.

### 4.4 Analyst (Retired Legacy Role)
- **Retired from active product**: There is no human analyst review capacity in the operating model.
- Legacy analyst review screens and endpoints are retired.
- Historical database records and migration lineage are preserved without data destruction.

---

## 5. Community Publication & Event Intelligence Policy

### 5.1 Community Publication Policy (Mandatory Safeguards)
A single citizen report is **private by default** and is never exposed as a public event. Community publication requires meeting a dynamic, hazard-aware intelligence threshold:
- **Mandatory Floor**: At least **two independent citizen contributors** (distinct accounts, distinct network signatures, no cross-syndication).
- **Source Independence Analysis**: Syndicated reports, botnets, coordinated duplicate submissions, and duplicate accounts do not count toward corroboration.
- **Spatial & Temporal Consistency**: Reports must fall within calibrated geospatial cluster radiuses (e.g., 2–15 km depending on hazard type) and temporal windows (e.g., 1–6 hours).
- **Contradictory Evidence Evaluation**: Opposing reports or negative evidence dampen confidence scores.
- **Confidence & Uncertainty Handling**: The 2-contributor floor is an eligibility prerequisite, not an automatic guarantee of publication. Numerical scores are calibrated against baseline distributions.

### 5.2 Public Event Statuses
Public events are strictly categorized and presented with unambiguous labels:
1. **Possible Community Event**: Clustering indicates emerging observations, pending full corroboration threshold.
2. **Community Corroborated Event**: Meets the multi-contributor floor, spatial/temporal proximity, and consistency requirements. Clearly labeled: *"Community corroborated · Not official verification"*.
3. **Official-Source Alert**: Direct telemetry or bulletin from an authorized government or scientific provider (e.g., IMD, Sachet, GDACS, USGS). Clearly labeled with full source attribution, link, and timestamp.
4. **Inactive / Stale Event**: An event with no fresh corroborating reports or updates within a decay window (e.g., 6–24 hours). Inactivity represents decay of fresh signals, never official resolution.

### 5.3 Public Information & Privacy Safeguards
- **Public Views Expose**: Sanitized aggregate summaries, approximate centroid coordinates (fuzzed/truncated precision), hazard category, observation and update timestamps, source counts by type, official attribution links, and uncertainty indicators.
- **Strictly Prohibited from Public Exposure**: Contributor identities, phone numbers, email addresses, exact residential coordinates, raw unredacted citizen text, private attachments, unconsented personal images, or identifying metadata.

### 5.4 Official Authority Boundaries
Cignal never:
- Issues autonomous emergency evacuation orders.
- Declares official states of disaster or emergency.
- Dispatches emergency responders autonomously.
- Represents algorithmic predictions as official state warnings.

---

## 6. Reporting, Tracking & Anonymity

### 6.1 Anonymous Reporting
- Requires zero personal contact details.
- Issues a cryptographically secure, random 32-character bearer tracking code.
- Tracking code is treated as a secret bearer credential: encrypted or securely hashed in database storage, never logged in plaintext, never exposed in public feeds.
- Rate-limited and protected against enumeration attacks via constant-time lookup and brute-force throttling.

### 6.2 Registered Reporting
- Registered contributors track and manage their submission history.
- Multi-factor authentication (MFA) supported for account security.
- No unapproved mandatory email or phone verification barriers.

---

## 7. Evidence Processing & Compression Policy

### 7.1 Accepted File Types & Quotas
- **Supported Formats**: JPEG, PNG, WebP, PDF.
- **Default Limits**: Max 10 MB per file; max 5 files per report.
- **Evidence Visibility**: Evidence files remain **private by default**. Only sanitized evidence metadata is public.
- **Out of Scope for Initial Release**: Audio, video, and live streaming.

### 7.2 Safety & Privacy Controls
- Strict magic-byte and MIME format validation.
- Complete EXIF/metadata stripping to prevent location or personal data leakage.
- Decompression bomb and pixel dimension limit enforcement.
- SHA-256 cryptographic hashing for integrity validation and deduplication.
- Secure local or S3-compatible object storage.

### 7.3 Compression Policy
When an uploaded image exceeds size limits but is within tolerable raw size (e.g., 10–25 MB), safe lossless or bounded lossy optimization is applied. Accepted only if:
- Resulting file satisfies configured size limits.
- Format remains valid and usable.
- Security and integrity checks pass.
Original hashes and transformation provenance are immutably preserved.

---

## 8. External Data Sources & Connector Governance

All external data connectors operate under a formal **Source Registry**:
- Every connector records provider identity, data category, license URL, terms of use, permitted processing/redistribution scopes, rate limits, polling cadence, and health metrics.
- **Project-Owner Approval Gate**: Activation of new connectors requires explicit project-owner authorization. Platform Administrators may only configure approved connectors.
- **Active / Pilot Sources**:
  - USGS Earthquake Hazards (Public domain government scientific telemetry).
  - Open-Meteo Weather API (Permitted weather forecast and alert conditions).
  - GDACS (International disaster coordination alerts with required attribution).
  - NASA EONET (Natural event metadata with source linking).
  - NDMA / Sachet CAP (National disaster alert protocol; authorized feeds).
- **Pending / Inactive Sources**: IMD, Bhuvan, NASA FIRMS, social media feeds, and custom RSS feeds remain inactive or require explicit credential provisioning and license verification before operational activation.

---

## 9. AI/ML Intelligence Pipeline

Cignal's intelligence architecture is local, CPU-conscious, and evidence-grounded:
1. **Multilingual NLP (Gujarati & English)**:
   - Language and script detection.
   - Text normalization and script transliteration handling.
   - Multi-label hazard categorization.
   - Urgency scoring (separate from physical hazard severity).
   - Help-request and life-safety indicator extraction.
   - Named entity recognition for Gujarat administrative entities (districts, talukas, major cities).
2. **Duplicate & Source Intelligence**:
   - Lexical (token/n-gram) and semantic embedding similarity.
   - Geospatial haversine distance and temporal window matching.
   - Cross-source syndication and duplicate account detection.
   - Contributor independence validation.
3. **Event Intelligence & Clustering**:
   - Spatiotemporal event clustering.
   - Event evolution tracking (append-only timeline snapshots).
   - Evidence-grounded structured summaries (every claim backed by observation references).
   - Calibrated confidence and uncertainty bounds.
4. **Computer Vision**:
   - Perceptual image hashing for duplicate image detection.
   - Quality and blur assessment.
   - EXIF scrubbing and privacy validation.

---

## 10. User Interface & Progressive Web App (PWA)

### 10.1 Design System & Palette
The interface adheres to an accessible, restrained dark aesthetic:
- **Deep Graphite-Blue**: `#0E1726` (Primary dark background)
- **Slate Blue**: `#26384D` (Cards, drawers, navigation containers)
- **Ice Blue**: `#B9D2E8` (Primary typography and key accents)
- **Semantic Accents**: Restrained status indicators for critical, warning, positive, and info states.

### 10.2 Public Pages
- **Overview**: Public landing with Gujarat focus, hazard summary, quick report CTA.
- **Live Event Map**: Interactive Leaflet/MapLibre map showing corroborated events and official alerts.
- **Public Event Feed**: Chronological, searchable list of verified public events.
- **Event Details**: Structured event breakdown, approximate map, timeline, and source links.
- **Submit Report**: Responsive report submission form (anonymous or authenticated).
- **Track Report**: Anonymous tracking portal via secret tracking code.
- **Notifications**: In-app alert inbox and browser push settings.

### 10.3 Languages
- Full bilingual interface in **Gujarati** (`gu`) and **English** (`en`) with instantaneous user toggle.
- Architecture designed for additional Indian languages in subsequent phases.

### 10.4 PWA & Offline Support
- Service worker app shell caching.
- Offline report drafting stored in IndexedDB.
- Background sync and idempotent report upload upon reconnecting.
- Browser Web Push notification support for subscribed districts and hazard categories.

---

## 11. Technical Architecture & Resilience

- **Backend**: Python 3.12, FastAPI, Uvicorn, SQLAlchemy 2.0, Alembic.
- **Database**: PostgreSQL 16 with PostGIS extension for spatial querying.
- **Cache & Message Broker**: Redis 7.
- **Background Tasks**: Redis Queue (RQ) with dedicated lanes (`cignal-priority`, `cignal`, `cignal-bulk`).
- **Transactional Outbox**: Durable `background_job_outbox` table ensuring at-least-once task dispatch with advisory locking and delivery fences.
- **Frontend**: NiceGUI responsive UI integrated with PWA service worker and WebSockets for real-time telemetry.
- **Evidence Storage**: Local filesystem with atomic hashing or S3-compatible blob storage.
