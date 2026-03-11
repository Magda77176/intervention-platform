# Architecture — Intervention Platform

## Multi-Agent Pipeline

Same pattern as enterprise agent pipelines (Orchestrator → Agents → MCP Tools):

```
Client → Orchestrator → Pre-qualification (Vertex AI Gemini)
                      → Assignment (MCP + Scoring)
                      → Notification (DLP + Email)
                      → MCP Tools (5 shared tools)
```

5 microservices on Cloud Run, communicating via HTTP (sync) and Pub/Sub (async).

## Data Flow

```
Ticket created
  │
  ├── MCP: search_address → validate, extract postal code
  ├── MCP: check_duplicate → Firestore query (7-day window)
  │
  ├── Pre-qual Agent:
  │     ├── DLP scan description (PII detection)
  │     └── Vertex AI Gemini → payer + urgency + category
  │
  ├── MCP: analyze_photo → Gemini Vision (severity + hazards)
  │
  ├── Firestore: store ticket (pending_validation)
  │
  └── Notification Agent:
        ├── DLP scan outgoing text
        └── Email manager

Manager validates → Orchestrator:
  │
  ├── Assignment Agent:
  │     ├── MCP: lookup_providers (skills + zone)
  │     └── Score (5 criteria) → best provider
  │
  ├── Notification Agent → DLP → email provider + tenant
  │
  └── Pub/Sub: ticket.assigned
```

## Shared Components

### schemas.py (Pydantic)
- Ticket, Provider, AgentMessage
- Strict typing, enums for status/urgency/category
- JSON-serializable for Pub/Sub transport

### tracing.py (OpenTelemetry)
- Cloud Trace in production
- Console exporter in development
- Every agent instruments its key operations

### pubsub.py
- Fire-and-forget event publishing
- Attributes for subscriber filtering

## 10 GCP Services

1. Cloud Run (5 services)
2. Vertex AI (Gemini 2.0 Flash)
3. Firestore (tickets, providers)
4. Cloud Storage (photos)
5. Pub/Sub (events)
6. Cloud DLP (PII scan)
7. Cloud Logging (structured)
8. Cloud Trace (OpenTelemetry)
9. Secret Manager
10. Cloud Build (CI/CD)
