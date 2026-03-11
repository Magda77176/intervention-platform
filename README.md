# 🏠 AI Intervention Platform — Multi-Agent Property Management on GCP

Multi-agent system for property maintenance management. 4 specialized agents + MCP server on Cloud Run, communicating via Pub/Sub. Vertex AI Gemini for pre-qualification, Cloud DLP for PII protection, OpenTelemetry for distributed tracing.

Same architecture pattern as enterprise AI agent pipelines: orchestrator → specialized agents → MCP tools.

## Architecture

```
                          ┌──────────────────┐
                          │    Client HTTP    │
                          └────────┬─────────┘
                                   │ POST /tickets
                                   ▼
                    ┌───────────────────────────────┐
                    │    ORCHESTRATOR (Cloud Run)    │
                    │    Route + coordinate          │
                    └──┬──────────┬──────────┬──────┘
                       │          │          │
              ┌────────▼───┐ ┌───▼────────┐ ┌▼───────────────┐
              │ PREQUAL    │ │ ASSIGNMENT │ │  NOTIFICATION  │
              │ (Cloud Run)│ │ (Cloud Run)│ │  (Cloud Run)   │
              │ Vertex AI  │ │ Scoring    │ │  DLP + Email   │
              └─────┬──────┘ └─────┬──────┘ └────────────────┘
                    │              │
              ┌─────▼──────────────▼──────┐
              │    MCP TOOLS (Cloud Run)   │
              │ 5 tools: address, dedup,   │
              │ photo (Vision), providers, │
              │ notifications              │
              └────────────────────────────┘
```

## Services

| Service | Port | Cloud Run | Role |
|---------|------|-----------|------|
| Orchestrator | 8000 | orchestrator-*.run.app | Entry point, routing |
| MCP Tools | 8001 | mcp-tools-*.run.app | Shared tools (5 tools) |
| Pre-qualification | 8002 | prequalification-*.run.app | Vertex AI ticket triage |
| Assignment | 8003 | assignment-*.run.app | Provider scoring + dispatch |
| Notification | 8004 | notification-*.run.app | DLP scan + send |

## Pipeline Flow

```
1. Client POST /tickets {description, photos, address}
2. Orchestrator → MCP: search_address (validate + postal code)
3. Orchestrator → MCP: check_duplicate (same address + type in 7 days?)
4. Orchestrator → Pre-qualification agent:
   4a. DLP scan description (flag PII in tenant text)
   4b. Vertex AI Gemini: classify payer + urgency + category
   4c. Rule-based fallback if Gemini unavailable
5. Orchestrator → MCP: analyze_photo (Gemini Vision)
   5a. Detect severity, safety hazards
   5b. Override urgency if emergency detected
6. Store ticket in Firestore (status: pending_validation)
7. Notification agent → DLP scan outgoing text → email manager
8. Pub/Sub event: ticket.created

--- Manager validates ---

9. Orchestrator → Assignment agent:
   9a. MCP: lookup_providers (skills + zone filter)
   9b. Score on 5 criteria (skills 30%, zone 20%, perf 25%, avail 15%, pricing 10%)
   9c. Auto-dispatch to best provider
10. Notification agent → DLP scan → email provider + tenant
11. Pub/Sub event: ticket.assigned
```

## GCP Services (10)

| # | Service | Purpose |
|---|---------|---------|
| 1 | **Cloud Run** | 5 microservices (serverless, auto-scale) |
| 2 | **Vertex AI** | Gemini 2.0 Flash (pre-qualification + photo analysis) |
| 3 | **Firestore** | Tickets, providers, audit log |
| 4 | **Cloud Storage** | Incident photos, intervention proofs |
| 5 | **Pub/Sub** | Inter-agent events (ticket.created, ticket.assigned, sla.breach) |
| 6 | **Cloud DLP** | Scan tenant descriptions + outgoing notifications for PII |
| 7 | **Cloud Logging** | Structured JSON logs per agent |
| 8 | **Cloud Trace** | OpenTelemetry distributed tracing across agents |
| 9 | **Secret Manager** | API keys, service account credentials |
| 10 | **Cloud Build** | CI/CD from GitHub |

## Key Technical Patterns

### MCP Server (Model Context Protocol)
5 discoverable tools via `GET /tools`:
- `search_address` — validate + normalize French addresses
- `check_duplicate` — Firestore deduplication (7-day window)
- `analyze_photo` — Gemini Vision multimodal analysis
- `lookup_providers` — query provider pool with filters
- `send_notification` — email delivery

### Pydantic Schemas (shared contract)
All agents share `shared/schemas.py`:
- `Ticket` — central data object flowing through the pipeline
- `AgentMessage` — Pub/Sub payload for async communication
- `Provider` — provider data model
- Strict typing, JSON-serializable for Pub/Sub

### OpenTelemetry Tracing
Every agent instruments key operations:
```
orchestrator.create_ticket
  ├── mcp.search_address (2ms)
  ├── mcp.check_duplicate (5ms)
  ├── agent.prequalification
  │     ├── dlp.scan_description (80ms)
  │     └── vertex_ai.generate (450ms)
  ├── mcp.analyze_photo (600ms)
  └── agent.notification
        └── dlp.scan_notification (80ms)
```

### Cloud DLP Integration
Two scan points:
1. **Incoming**: scan tenant descriptions for PII (phone, email, NIR, IBAN)
2. **Outgoing**: scan every notification before sending — redact if PII found

## 12 Business Processes

| # | Process | Agent | GCP Services |
|---|---------|-------|-------------|
| 1 | Déclaration & Pré-qualification | Orchestrator + Prequal | Vertex AI, DLP, Firestore, Storage |
| 2 | Validation Gestionnaire | Orchestrator | Firestore |
| 3 | Refusé (locataire) | Notification | DLP, Pub/Sub |
| 4 | Assignation Prestataire | Assignment + MCP | Firestore |
| 5 | Planification | Orchestrator | Firestore, Pub/Sub |
| 6 | Intervention | - | Storage (photos) |
| 7 | CSAT Locataire | - | Firestore |
| 8 | Clôture | - | Firestore, BigQuery |
| 9 | Ré-ouverture | Orchestrator | Pub/Sub |
| 10 | SLA & Escalades | Cloud Scheduler | Pub/Sub |
| 11 | Notifications | Notification | DLP, Pub/Sub |
| 12 | Qualité | Analytics | BigQuery |

## Tests

```bash
pytest tests/ -v
# 21 tests: schemas, pre-qualification fallback, MCP tools, scoring
```

## Deploy

```bash
# Deploy all 5 services
for svc in mcp-tools prequalification assignment notification orchestrator; do
  gcloud run deploy $svc --source . --region us-central1
done
```

## File Structure

```
├── shared/
│   ├── schemas.py          # Pydantic models (Ticket, Provider, AgentMessage)
│   ├── tracing.py          # OpenTelemetry → Cloud Trace
│   └── pubsub.py           # Pub/Sub publish utilities
├── mcp_server/
│   └── server.py           # MCP tools server (5 tools)
├── agents/
│   ├── orchestrator/
│   │   └── main.py         # Entry point, pipeline coordination
│   ├── prequalification/
│   │   └── main.py         # Vertex AI Gemini + DLP scan
│   ├── assignment/
│   │   └── main.py         # Provider scoring + auto-dispatch
│   └── notification/
│       └── main.py         # DLP redaction + email delivery
├── tests/
│   └── test_platform.py    # 21 tests
├── docs/
│   └── ARCHITECTURE.md     # System architecture + process map
├── .github/workflows/
│   └── deploy.yml          # CI/CD (test → deploy)
├── Dockerfile
└── requirements.txt
```
