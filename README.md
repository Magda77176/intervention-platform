# 🏠 AI Intervention Platform — Property Management on GCP

AI-powered property maintenance platform for social housing. Tenants report issues, AI pre-qualifies and routes to the right contractor, managers validate, SLA engine enforces deadlines.

## The Problem

Property managers handle hundreds of maintenance tickets monthly. Manual triage is slow, inconsistent, and error-prone. Who pays (landlord vs tenant)? What's the urgency? Which contractor? How to track SLA?

## The Solution

12 automated processes, from tenant ticket to closure, with AI at every decision point.

## Architecture

```
Tenant App → AI Pre-qualification → Manager Validation → Provider Assignment → Intervention → CSAT
     │              │                                           │                    │
     │         Vertex AI Gemini                          Scoring Engine         SLA Engine
     │         (payer + urgency                          (skills, zone,        (deadlines,
     │          + category)                               CSAT, load)          escalation)
     │              │                                           │                    │
     └──── Cloud Storage (photos) ──→ Gemini Vision (severity + hazards)             │
                                                                                     │
                              Pub/Sub ←──── Notifications ←──── Cloud Scheduler ─────┘
                                │
                           BigQuery (KPI trends, reporting)
```

## AI Modules

| Module | What it does | Method |
|--------|-------------|--------|
| **Pre-qualification** | Determines who pays + urgency + category | Vertex AI Gemini structured prompt |
| **Photo Analysis** | Assesses severity from incident photos | Gemini Vision (multimodal) |
| **Provider Scoring** | Ranks contractors by 5 criteria | Weighted scoring (skills 30%, zone 20%, perf 25%, availability 15%, pricing 10%) |
| **SLA Engine** | Monitors deadlines at every stage | Cloud Scheduler + Pub/Sub escalation |
| **Notifications** | 16 types, targeted to the right person | Pub/Sub decoupled, templated |
| **Analytics** | KPIs, trends, pre-qualification accuracy | Firestore + BigQuery |

## 12 Processes

| # | Process | Description |
|---|---------|-------------|
| 1 | **Déclaration** | Tenant reports issue + photos → AI pre-qualification |
| 2 | **Validation** | Manager validates (landlord pays) or refuses |
| 3 | **Refus** | Tenant responsibility → ticket closed, no intervention |
| 4 | **Assignation** | AI scores providers → auto-dispatch or manual pick |
| 5 | **Planification** | Provider proposes slot, tenant confirms |
| 6 | **Intervention** | Multi-step: diagnostic → work → control |
| 7 | **CSAT** | Tenant rates (1-5), confirms resolution or reopens |
| 8 | **Clôture** | Archive + KPI update + BigQuery snapshot |
| 9 | **Ré-ouverture** | Unsatisfied tenant → back to validation |
| 10 | **SLA** | Deadline monitoring + automatic escalation |
| 11 | **Notifications** | Contextual alerts at every status change |
| 12 | **Qualité** | Weekly KPI review, provider pool optimization |

## Key Rules

- 🔒 **No intervention without manager validation** (mandatory gate)
- ⛔ **Tenant responsibility = full stop** (no assignment, no planning)
- 🔔 **Emergency = priority tag** from pre-qualification (gas, fire, flood)
- 📊 **AI accuracy tracked** (pre-qualification vs manager decision)

## GCP Services (10)

| # | Service | Purpose |
|---|---------|---------|
| 1 | **Cloud Run** | FastAPI hosting (serverless) |
| 2 | **Vertex AI** | Gemini 2.0 Flash (pre-qual + photo analysis) |
| 3 | **Firestore** | Tickets, providers, sessions |
| 4 | **Cloud Storage** | Photos, documents, proofs |
| 5 | **Pub/Sub** | Notifications, SLA events |
| 6 | **Cloud Logging** | Structured JSON logs |
| 7 | **Cloud Scheduler** | SLA checks (15min), KPI snapshots (daily) |
| 8 | **BigQuery** | KPI trends, reporting |
| 9 | **Secret Manager** | API keys |
| 10 | **Cloud Build** | CI/CD |

## API Endpoints

### Tenant
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/tickets` | Create ticket (triggers AI pre-qualification) |
| POST | `/api/tickets/{id}/photos` | Upload + AI photo analysis |
| GET | `/api/tickets/{id}` | Get ticket status + SLA info |
| POST | `/api/tickets/{id}/csat` | Submit satisfaction score |

### Manager
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/manager/pending` | List tickets pending validation |
| POST | `/api/manager/tickets/{id}/decide` | Validate / refuse / request info |
| POST | `/api/manager/tickets/{id}/assign` | Assign provider (auto or manual) |

### Provider
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/provider/tickets/{id}/respond` | Accept / refuse assignment |
| POST | `/api/provider/tickets/{id}/complete` | Mark intervention complete |

### System
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/system/sla-check` | Cloud Scheduler trigger |
| GET | `/api/analytics/kpis` | KPIs (configurable period) |
| GET | `/api/health` | Health check |

## Tests

```bash
pytest tests/ -v
# 27 tests: pre-qualification, scoring, deduplication, SLA
```

## Deploy

```bash
gcloud run deploy intervention-platform \
    --source . \
    --region us-central1 \
    --allow-unauthenticated
```

## File Structure

```
├── src/
│   ├── ai/
│   │   ├── prequalification.py   # AI ticket triage (Gemini)
│   │   ├── photo_analysis.py     # Incident photo analysis (Gemini Vision)
│   │   ├── provider_scoring.py   # Contractor ranking engine
│   │   ├── sla_engine.py         # Deadline monitoring + escalation
│   │   ├── notifications.py      # 16 notification types via Pub/Sub
│   │   └── analytics.py          # KPI computation + BigQuery
│   ├── api/
│   │   └── main.py               # FastAPI endpoints (tenant/manager/provider)
│   └── lib/                      # Shared utilities
├── tests/
│   └── test_prequalification.py  # 27 tests
├── docs/
│   └── ARCHITECTURE.md           # System architecture + process map
├── .github/workflows/
│   └── deploy.yml                # CI/CD (test → deploy)
├── Dockerfile
└── requirements.txt
```

## Cost (MVP, free tier)

| Service | Free Tier |
|---------|-----------|
| Cloud Run | 2M req/month |
| Vertex AI Gemini | 15 req/min |
| Firestore | 1 GiB |
| Cloud Storage | 5 GB |
| Pub/Sub | 10 GB/month |
| BigQuery | 1 TB query/month |
| **Total** | **$0** (development) |
