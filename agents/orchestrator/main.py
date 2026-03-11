"""
Orchestrator Agent — Entry point, routes tickets through the pipeline.
Same role as agent-pipeline orchestrator: coordinate, don't execute.

Flow:
  POST /tickets → Orchestrator
    → calls Pre-qualification agent (Vertex AI)
    → calls MCP tools (duplicate check, photo analysis)
    → if emergency → fast-track notification
    → stores ticket in Firestore (status: pending_validation)
    → publishes event to Pub/Sub

  POST /tickets/{id}/validate → Manager decision
    → if validated → calls Assignment agent
    → if refused → notification agent (tenant informed)

  POST /tickets/{id}/complete → Provider done
    → notification agent (CSAT request)
"""
import os
import json
import time
import logging
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

from shared.schemas import Ticket, TicketStatus, AgentMessage
from shared.tracing import init_tracing, get_tracer
from shared.pubsub import publish_message, publish_event

from google.cloud import firestore

logger = logging.getLogger("intervention")
tracer = init_tracing("orchestrator")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
PREQUAL_URL = os.getenv("PREQUALIFICATION_URL", "http://localhost:8002")
ASSIGNMENT_URL = os.getenv("ASSIGNMENT_URL", "http://localhost:8003")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "http://localhost:8004")
MCP_URL = os.getenv("MCP_TOOLS_URL", "http://localhost:8001")

db = firestore.Client(project=PROJECT_ID)

app = FastAPI(title="Orchestrator", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ============================================================
# CREATE TICKET (full pipeline)
# ============================================================

@app.post("/tickets")
async def create_ticket(ticket: Ticket):
    """
    Main entry point. Orchestrates:
    1. MCP: address validation + duplicate check
    2. Pre-qualification agent: Vertex AI classification
    3. MCP: photo analysis (if media attached)
    4. Store in Firestore
    5. Notify manager
    """
    with tracer.start_as_current_span("orchestrator.create_ticket") as span:
        span.set_attribute("ticket.id", ticket.id)
        t0 = time.time()
        
        async with httpx.AsyncClient(timeout=10) as client:
            
            # Step 1: Address validation (MCP)
            with tracer.start_as_current_span("mcp.search_address"):
                try:
                    addr_resp = await client.post(
                        f"{MCP_URL}/tools/search_address",
                        json={"address": ticket.address},
                    )
                    addr_data = addr_resp.json()
                    ticket.address_postal_code = addr_data.get("postal_code", "")
                except Exception as e:
                    logger.warning(f"Address validation skipped: {e}")
            
            # Step 2: Duplicate check (MCP)
            with tracer.start_as_current_span("mcp.check_duplicate"):
                try:
                    dup_resp = await client.post(
                        f"{MCP_URL}/tools/check_duplicate",
                        json={"address": ticket.address, "category": ticket.category or "other"},
                    )
                    dup_data = dup_resp.json()
                    ticket.is_duplicate = dup_data.get("is_duplicate", False)
                    ticket.duplicate_ticket_id = dup_data.get("duplicate_ticket_id")
                except Exception as e:
                    logger.warning(f"Duplicate check skipped: {e}")
            
            # Step 3: Pre-qualification (Vertex AI agent)
            with tracer.start_as_current_span("agent.prequalification"):
                try:
                    prequal_resp = await client.post(
                        f"{PREQUAL_URL}/prequalify",
                        json=ticket.model_dump(mode="json"),
                    )
                    prequal = prequal_resp.json()
                    ticket.payer = prequal.get("payer", "to_confirm")
                    ticket.payer_confidence = prequal.get("payer_confidence", 0)
                    ticket.payer_reason = prequal.get("payer_reason", "")
                    ticket.urgency = prequal.get("urgency", "normal")
                    ticket.category = prequal.get("category", "other")
                    ticket.summary = prequal.get("summary", "")
                    ticket.suggested_skills = prequal.get("suggested_skills", [])
                except Exception as e:
                    logger.error(f"Pre-qualification failed: {e}")
                    ticket.payer = "to_confirm"
                    ticket.urgency = "normal"
            
            # Step 4: Photo analysis (MCP + Gemini Vision)
            if ticket.media_urls:
                with tracer.start_as_current_span("mcp.analyze_photo"):
                    try:
                        photo_resp = await client.post(
                            f"{MCP_URL}/tools/analyze_photo",
                            json={
                                "gcs_uri": ticket.media_urls[0],
                                "description": ticket.description,
                            },
                        )
                        ticket.photo_analysis = photo_resp.json()
                        
                        # Override urgency if photo shows emergency
                        if ticket.photo_analysis.get("needs_emergency"):
                            ticket.urgency = "emergency"
                    except Exception as e:
                        logger.warning(f"Photo analysis skipped: {e}")
            
            # Step 5: Store in Firestore
            ticket.status = TicketStatus.PENDING_VALIDATION
            ticket.status_changed_at = datetime.utcnow().isoformat()
            
            db.collection("tickets").document(ticket.id).set(
                ticket.model_dump(mode="json")
            )
            
            # Step 6: Notify manager
            with tracer.start_as_current_span("agent.notification"):
                try:
                    await client.post(
                        f"{NOTIFICATION_URL}/notify",
                        json={
                            "type": "ticket_created",
                            "ticket": ticket.model_dump(mode="json"),
                            "recipients": ["manager"],
                        },
                    )
                except Exception as e:
                    logger.warning(f"Notification skipped: {e}")
            
            # Step 7: Pub/Sub event
            publish_event("ticket.created", ticket.id, {
                "urgency": ticket.urgency,
                "category": ticket.category,
                "payer": ticket.payer,
            })
        
        latency = int((time.time() - t0) * 1000)
        span.set_attribute("latency_ms", latency)
        
        logger.info("ticket_created", extra={
            "json_fields": {
                "ticket_id": ticket.id,
                "urgency": ticket.urgency,
                "category": ticket.category,
                "payer": ticket.payer,
                "confidence": ticket.payer_confidence,
                "is_duplicate": ticket.is_duplicate,
                "latency_ms": latency,
            }
        })
        
        return {
            "ticket_id": ticket.id,
            "status": ticket.status,
            "urgency": ticket.urgency,
            "payer": ticket.payer,
            "payer_confidence": ticket.payer_confidence,
            "category": ticket.category,
            "summary": ticket.summary,
            "is_duplicate": ticket.is_duplicate,
        }


# ============================================================
# MANAGER VALIDATION
# ============================================================

@app.post("/tickets/{ticket_id}/validate")
async def validate_ticket(ticket_id: str, decision: str, reason: str = ""):
    """
    Process #2: Manager validates or refuses.
    Validated → trigger assignment agent.
    Refused → notify tenant, close ticket.
    """
    with tracer.start_as_current_span("orchestrator.validate") as span:
        doc = db.collection("tickets").document(ticket_id).get()
        if not doc.exists:
            raise HTTPException(404, "Ticket not found")
        
        ticket = Ticket(**doc.to_dict())
        
        async with httpx.AsyncClient(timeout=10) as client:
            if decision == "validated":
                ticket.status = TicketStatus.VALIDATED
                ticket.manager_decision = "validated"
                
                # Trigger assignment agent
                with tracer.start_as_current_span("agent.assignment"):
                    try:
                        assign_resp = await client.post(
                            f"{ASSIGNMENT_URL}/assign",
                            json=ticket.model_dump(mode="json"),
                        )
                        assign_data = assign_resp.json()
                        ticket.provider_id = assign_data.get("provider_id")
                        ticket.provider_name = assign_data.get("provider_name")
                        ticket.provider_score = assign_data.get("score", 0)
                        ticket.status = TicketStatus.ASSIGNED
                    except Exception as e:
                        logger.error(f"Assignment failed: {e}")
                
                # Notify tenant + provider
                await client.post(f"{NOTIFICATION_URL}/notify", json={
                    "type": "ticket_validated",
                    "ticket": ticket.model_dump(mode="json"),
                    "recipients": ["tenant", "provider"],
                })
                
            elif decision == "refused":
                ticket.status = TicketStatus.REFUSED
                ticket.manager_decision = "refused"
                ticket.refusal_reason = reason
                
                # Notify tenant only
                await client.post(f"{NOTIFICATION_URL}/notify", json={
                    "type": "ticket_refused",
                    "ticket": ticket.model_dump(mode="json"),
                    "recipients": ["tenant"],
                })
        
        ticket.status_changed_at = datetime.utcnow().isoformat()
        db.collection("tickets").document(ticket_id).set(
            ticket.model_dump(mode="json")
        )
        
        publish_event(f"ticket.{decision}", ticket_id)
        
        return {"ticket_id": ticket_id, "status": ticket.status}


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "orchestrator"}
