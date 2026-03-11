"""
Intervention Platform API — FastAPI + GCP services.

Endpoints for:
- Tenant: create ticket, upload photos, check status, CSAT
- Manager: validate/refuse, assign provider, view tickets
- Provider: accept/refuse, plan, complete intervention
- System: SLA checks, analytics, notifications
"""
import os
import json
import logging
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from google.cloud import firestore, storage

# AI modules
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from ai.prequalification import prequalify_ticket
from ai.provider_scoring import score_providers, auto_dispatch
from ai.photo_analysis import analyze_incident_photo
from ai.sla_engine import check_ticket_sla, check_all_active_tickets
from ai.notifications import notify, NotificationType
from ai.analytics import compute_kpis

logger = logging.getLogger("intervention")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
BUCKET_NAME = os.getenv("GCS_MEDIA_BUCKET", f"{PROJECT_ID}-intervention-media")

db = firestore.Client(project=PROJECT_ID)
gcs = storage.Client(project=PROJECT_ID)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Intervention Platform API started")
    yield
    logger.info("Intervention Platform API stopped")


app = FastAPI(
    title="Intervention Platform API",
    description="Property management intervention platform with AI pre-qualification",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# MODELS
# ============================================================

class TicketCreate(BaseModel):
    description: str
    location: str                   # "logement" | "parties_communes"
    equipment_type: str             # "logement" | "personnel"
    severity_details: str
    since_when: str
    availability: str
    address: str
    address_postal_code: str = ""
    tenant_id: str


class ManagerDecision(BaseModel):
    decision: str                   # "validated" | "refused" | "needs_info"
    refusal_reason: str = ""
    budget_approved: float = 0.0
    notes: str = ""


class ProviderResponse(BaseModel):
    accepted: bool
    proposed_date: str = ""
    proposed_time_slot: str = ""
    decline_reason: str = ""


class InterventionComplete(BaseModel):
    notes: str
    time_spent_minutes: int
    parts_used: list[str] = []
    needs_followup: bool = False
    followup_reason: str = ""


class CSATSubmit(BaseModel):
    score: int                      # 1-5
    comment: str = ""
    is_resolved: bool = True


# ============================================================
# TENANT ENDPOINTS
# ============================================================

@app.post("/api/tickets")
async def create_ticket(ticket: TicketCreate):
    """
    Process #1: Tenant creates a ticket.
    → AI pre-qualification
    → Photo analysis (if media attached)
    → Deduplication check
    → Status: "pending_validation"
    """
    # AI pre-qualification
    qualification = prequalify_ticket(ticket.dict())
    
    # Create ticket in Firestore
    ticket_data = {
        **ticket.dict(),
        "status": "pending_validation",
        "urgency": qualification.get("urgency", "normal"),
        "category": qualification.get("category", "other"),
        "ai_payer": qualification.get("payer", "to_confirm"),
        "ai_confidence": qualification.get("payer_confidence", 0),
        "ai_summary": qualification.get("summary", ""),
        "suggested_skills": qualification.get("suggested_skills", []),
        "is_duplicate": qualification.get("is_duplicate", False),
        "duplicate_ticket_id": qualification.get("duplicate_ticket_id"),
        "created_at": datetime.utcnow().isoformat(),
        "status_changed_at": datetime.utcnow().isoformat(),
        "media_urls": [],
    }
    
    doc_ref = db.collection("tickets").add(ticket_data)
    ticket_id = doc_ref[1].id
    
    # Notify
    notify(NotificationType.TICKET_CREATED, {
        **ticket_data,
        "ticket_id": ticket_id,
        "summary": qualification.get("summary", ticket.description[:50]),
        "payer_indication": qualification.get("payer", "to_confirm"),
        "confidence": int(qualification.get("payer_confidence", 0) * 100),
    })
    
    return {
        "ticket_id": ticket_id,
        "status": "pending_validation",
        "qualification": qualification,
    }


@app.post("/api/tickets/{ticket_id}/photos")
async def upload_photo(ticket_id: str, file: UploadFile = File(...)):
    """Upload and analyze incident photo."""
    # Read file
    content = await file.read()
    
    # Upload to Cloud Storage
    bucket = gcs.bucket(BUCKET_NAME)
    blob_name = f"tickets/{ticket_id}/{file.filename}"
    blob = bucket.blob(blob_name)
    blob.upload_from_string(content, content_type=file.content_type)
    
    gcs_uri = f"gs://{BUCKET_NAME}/{blob_name}"
    
    # AI analysis
    ticket_doc = db.collection("tickets").document(ticket_id).get()
    ticket_data = ticket_doc.to_dict() if ticket_doc.exists else {}
    
    analysis = analyze_incident_photo(
        image_data=content,
        description=ticket_data.get("description", ""),
        category=ticket_data.get("category", "other"),
        mime_type=file.content_type or "image/jpeg",
    )
    
    # Update ticket with photo + analysis
    db.collection("tickets").document(ticket_id).update({
        "media_urls": firestore.ArrayUnion([gcs_uri]),
        "photo_analysis": analysis,
    })
    
    return {"gcs_uri": gcs_uri, "analysis": analysis}


@app.get("/api/tickets/{ticket_id}")
async def get_ticket(ticket_id: str):
    """Get ticket details + SLA status."""
    doc = db.collection("tickets").document(ticket_id).get()
    if not doc.exists:
        raise HTTPException(404, "Ticket not found")
    
    data = doc.to_dict()
    data["ticket_id"] = ticket_id
    
    # Attach SLA info
    sla = check_ticket_sla(ticket_id)
    if sla:
        data["sla"] = {
            "hours_remaining": sla.hours_remaining,
            "is_breached": sla.is_breached,
            "breach_level": sla.breach_level,
        }
    
    return data


@app.post("/api/tickets/{ticket_id}/csat")
async def submit_csat(ticket_id: str, csat: CSATSubmit):
    """Process #7: Tenant submits CSAT."""
    if csat.is_resolved:
        new_status = "closed"
    else:
        new_status = "reopened"
    
    db.collection("tickets").document(ticket_id).update({
        "csat_score": csat.score,
        "csat_comment": csat.comment,
        "status": new_status,
        "status_changed_at": datetime.utcnow().isoformat(),
        "closed_at": datetime.utcnow().isoformat() if csat.is_resolved else None,
    })
    
    if not csat.is_resolved:
        notify(NotificationType.TICKET_REOPENED, {"ticket_id": ticket_id})
    
    return {"status": new_status}


# ============================================================
# MANAGER ENDPOINTS
# ============================================================

@app.get("/api/manager/pending")
async def get_pending_tickets():
    """Get all tickets pending validation."""
    docs = db.collection("tickets").where(
        "status", "==", "pending_validation"
    ).order_by("created_at", direction="DESCENDING").stream()
    
    tickets = []
    for doc in docs:
        data = doc.to_dict()
        data["ticket_id"] = doc.id
        tickets.append(data)
    
    return {"tickets": tickets, "count": len(tickets)}


@app.post("/api/manager/tickets/{ticket_id}/decide")
async def manager_decision(ticket_id: str, decision: ManagerDecision):
    """
    Process #2: Manager validates or refuses.
    🔒 No intervention without "Validé (bailleur)".
    """
    updates = {
        "manager_decision": decision.decision,
        "manager_notes": decision.notes,
        "status_changed_at": datetime.utcnow().isoformat(),
    }
    
    if decision.decision == "validated":
        updates["status"] = "validated"
        updates["budget_approved"] = decision.budget_approved
        
        notify(NotificationType.TICKET_VALIDATED, {
            "ticket_id": ticket_id,
            "summary": "",  # Will be filled from ticket data
        })
        
    elif decision.decision == "refused":
        # Process #3: Refused — tenant pays
        updates["status"] = "refused"
        updates["refusal_reason"] = decision.refusal_reason
        
        notify(NotificationType.TICKET_REFUSED, {
            "ticket_id": ticket_id,
            "summary": "",
            "refusal_reason": decision.refusal_reason,
        })
    
    elif decision.decision == "needs_info":
        updates["status"] = "pending_validation"  # Stays pending
        notify(NotificationType.COMPLEMENT_NEEDED, {
            "ticket_id": ticket_id,
            "notes": decision.notes,
        })
    
    db.collection("tickets").document(ticket_id).update(updates)
    
    return {"status": updates.get("status", "pending_validation")}


@app.post("/api/manager/tickets/{ticket_id}/assign")
async def assign_provider(ticket_id: str, provider_id: str = None):
    """
    Process #4: Assign provider (auto or manual).
    """
    ticket_doc = db.collection("tickets").document(ticket_id).get()
    if not ticket_doc.exists:
        raise HTTPException(404, "Ticket not found")
    
    ticket_data = ticket_doc.to_dict()
    
    if ticket_data.get("status") != "validated":
        raise HTTPException(400, "Ticket must be validated before assignment")
    
    if provider_id:
        # Manual assignment
        assignment = {"provider_id": provider_id, "deadline_hours": 24}
    else:
        # Auto-dispatch
        assignment = auto_dispatch(ticket_data)
        if not assignment:
            raise HTTPException(404, "No available provider found")
    
    db.collection("tickets").document(ticket_id).update({
        "status": "assigned",
        "provider_id": assignment["provider_id"],
        "assignment_deadline": assignment.get("deadline_hours", 24),
        "assigned_at": datetime.utcnow().isoformat(),
        "status_changed_at": datetime.utcnow().isoformat(),
    })
    
    notify(NotificationType.PROVIDER_ASSIGNED, {
        "ticket_id": ticket_id,
        "summary": ticket_data.get("ai_summary", ""),
        "address": ticket_data.get("address", ""),
        "urgency": ticket_data.get("urgency", "normal"),
        "deadline_hours": assignment.get("deadline_hours", 24),
    })
    
    return {"status": "assigned", "assignment": assignment}


# ============================================================
# PROVIDER ENDPOINTS
# ============================================================

@app.post("/api/provider/tickets/{ticket_id}/respond")
async def provider_respond(ticket_id: str, response: ProviderResponse):
    """Process #4 continued: Provider accepts or refuses."""
    if response.accepted:
        db.collection("tickets").document(ticket_id).update({
            "status": "planned" if response.proposed_date else "assigned",
            "planned_date": response.proposed_date,
            "planned_time_slot": response.proposed_time_slot,
            "status_changed_at": datetime.utcnow().isoformat(),
        })
        
        if response.proposed_date:
            notify(NotificationType.INTERVENTION_PLANNED, {
                "ticket_id": ticket_id,
                "date": response.proposed_date,
                "time_slot": response.proposed_time_slot,
            })
    else:
        # Re-dispatch to next provider
        db.collection("tickets").document(ticket_id).update({
            "status": "validated",  # Back to assignable
            "provider_decline_reason": response.decline_reason,
            "status_changed_at": datetime.utcnow().isoformat(),
        })
    
    return {"status": "accepted" if response.accepted else "declined"}


@app.post("/api/provider/tickets/{ticket_id}/complete")
async def complete_intervention(ticket_id: str, data: InterventionComplete):
    """Process #6: Provider marks intervention as complete."""
    db.collection("tickets").document(ticket_id).update({
        "status": "pending_csat",
        "intervention_notes": data.notes,
        "time_spent_minutes": data.time_spent_minutes,
        "parts_used": data.parts_used,
        "needs_followup": data.needs_followup,
        "completed_at": datetime.utcnow().isoformat(),
        "status_changed_at": datetime.utcnow().isoformat(),
    })
    
    notify(NotificationType.INTERVENTION_COMPLETED, {
        "ticket_id": ticket_id,
    })
    notify(NotificationType.CSAT_REQUEST, {
        "ticket_id": ticket_id,
    })
    
    return {"status": "pending_csat"}


# ============================================================
# SYSTEM ENDPOINTS
# ============================================================

@app.post("/api/system/sla-check")
async def sla_check():
    """Triggered by Cloud Scheduler — check all active ticket SLAs."""
    results = check_all_active_tickets()
    return {
        "checked": len(results),
        "warnings": sum(1 for r in results if r.breach_level == "warning"),
        "breaches": sum(1 for r in results if r.breach_level == "breach"),
        "critical": sum(1 for r in results if r.breach_level == "critical"),
    }


@app.get("/api/analytics/kpis")
async def get_kpis(days: int = 30):
    """Get KPIs for the last N days."""
    return compute_kpis(period_days=days)


@app.get("/api/health")
async def health():
    return {
        "status": "healthy",
        "project": PROJECT_ID,
        "timestamp": datetime.utcnow().isoformat(),
    }
