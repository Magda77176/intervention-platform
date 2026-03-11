# Architecture — Intervention Platform

## System Overview

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│   Tenant    │     │   Manager    │     │  Provider    │
│   (PWA)     │     │  (Console)   │     │  (Portal)    │
└──────┬──────┘     └──────┬───────┘     └──────┬───────┘
       │                   │                    │
       └───────────────────┼────────────────────┘
                           │
                    ┌──────▼───────┐
                    │  Cloud Run   │
                    │  (FastAPI)   │
                    └──┬──┬──┬──┬─┘
                       │  │  │  │
          ┌────────────┘  │  │  └────────────┐
          ▼               ▼  ▼               ▼
    ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
    │Vertex AI │  │Firestore │  │  Pub/Sub │  │  Cloud   │
    │(Gemini)  │  │(tickets, │  │(notifs,  │  │ Storage  │
    │          │  │providers)│  │ SLA,     │  │(photos)  │
    │- Prequal │  │          │  │ events)  │  │          │
    │- Photos  │  │          │  │          │  │          │
    │- Scoring │  │          │  │          │  │          │
    └──────────┘  └──────────┘  └──────────┘  └──────────┘
          │                          │
          │                   ┌──────▼───────┐
          │                   │   Workers    │
          │                   │- Email       │
          │                   │- SMS         │
          │                   │- Slack       │
          │                   └──────────────┘
          │
    ┌─────▼──────┐   ┌──────────┐   ┌──────────┐
    │  BigQuery  │   │  Cloud   │   │  Cloud   │
    │  (KPIs,    │   │ Logging  │   │Scheduler │
    │  trends)   │   │(struct.) │   │(SLA cron)│
    └────────────┘   └──────────┘   └──────────┘
```

## AI Modules

### 1. Pre-qualification (`prequalification.py`)
- Input: tenant description + questionnaire answers
- Output: payer indication, urgency, category, summary
- Method: Gemini 2.0 Flash structured prompt
- Fallback: rule-based classification (keywords)
- Deduplication: same address + same category within 7 days

### 2. Photo Analysis (`photo_analysis.py`)
- Input: incident photos (from Cloud Storage)
- Output: severity, safety hazards, category confirmation
- Method: Gemini Vision (multimodal)
- Use case: validate tenant claims, detect emergencies

### 3. Provider Scoring (`provider_scoring.py`)
- Input: ticket requirements + provider pool
- Output: ranked list of eligible providers
- Criteria: skills (30%), zone (20%), performance (25%), availability (15%), pricing (10%)
- Auto-dispatch: round-robin with scoring

### 4. SLA Engine (`sla_engine.py`)
- Monitors every ticket at every stage
- Configurable limits by urgency (emergency: 2h validation, normal: 24h)
- Pub/Sub escalation events on breach
- Cloud Scheduler triggers periodic checks

### 5. Notifications (`notifications.py`)
- 16 notification types (creation → close)
- Templates per recipient type (tenant/manager/provider)
- Pub/Sub decoupled delivery
- MVP: email only (extensible to SMS, push, WhatsApp)

### 6. Analytics (`analytics.py`)
- KPIs: resolution rate, SLA compliance, CSAT, reopening rate
- Pre-qualification accuracy (AI vs manager decision)
- BigQuery snapshots for trend analysis
- Per-category and per-provider breakdowns

## 12 Processes

| # | Process | AI Involved | GCP Services |
|---|---------|-------------|--------------|
| 1 | Déclaration & Pré-qualification | ✅ Gemini + Vision | Vertex AI, Firestore, Cloud Storage |
| 2 | Validation Gestionnaire | — | Firestore, Pub/Sub |
| 3 | Refusé (locataire payeur) | — | Firestore, Pub/Sub |
| 4 | Assignation Prestataire | ✅ Scoring engine | Firestore |
| 5 | Planification | — | Firestore, Pub/Sub |
| 6 | Intervention (multi-étapes) | — | Firestore, Cloud Storage |
| 7 | Validation Locataire (CSAT) | — | Firestore |
| 8 | Clôture & Archivage | — | Firestore, BigQuery |
| 9 | Ré-ouverture | — | Firestore, Pub/Sub |
| 10 | SLA & Escalades | ✅ SLA engine | Cloud Scheduler, Pub/Sub |
| 11 | Messagerie & Notifications | — | Pub/Sub |
| 12 | Qualité & Contrôles | ✅ Analytics | BigQuery |

## GCP Services (10)

1. **Cloud Run** — API hosting (FastAPI)
2. **Vertex AI** — Gemini for pre-qualification + photo analysis
3. **Firestore** — Tickets, providers, sessions
4. **Cloud Storage** — Photos, documents, intervention proofs
5. **Pub/Sub** — Notifications, SLA events, analytics events
6. **Cloud Logging** — Structured JSON logs
7. **Cloud Scheduler** — SLA checks (every 15min), KPI snapshots (daily)
8. **BigQuery** — KPI trends, reporting
9. **Secret Manager** — API keys
10. **Cloud Build** — CI/CD from GitHub
