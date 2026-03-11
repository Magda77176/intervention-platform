"""
SLA Engine — Track deadlines, trigger escalations, enforce rules.

Monitors every ticket at every stage and enforces time limits:
- Pre-qualification → Manager validation: 24h (4h if emergency)
- Manager validation → Provider assignment: 4h
- Provider acceptance deadline: 1-24h (based on urgency)
- Assignment → Intervention: 48h normal, 24h priority, 4h emergency
- Intervention → CSAT: 48h auto-close

Triggers Cloud Scheduler jobs for deadline monitoring.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from enum import Enum
from dataclasses import dataclass

from google.cloud import firestore, pubsub_v1

logger = logging.getLogger("intervention")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
db = firestore.Client(project=PROJECT_ID)
publisher = pubsub_v1.PublisherClient()
ESCALATION_TOPIC = f"projects/{PROJECT_ID}/topics/sla-escalations"


# ============================================================
# SLA DEFINITIONS
# ============================================================

class TicketStatus(Enum):
    PENDING_VALIDATION = "pending_validation"
    VALIDATED = "validated"
    REFUSED = "refused"
    ASSIGNED = "assigned"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED_PROVIDER = "completed_provider"
    PENDING_CSAT = "pending_csat"
    CLOSED = "closed"
    REOPENED = "reopened"


# SLA limits in hours per status transition
SLA_LIMITS = {
    "normal": {
        "pending_validation": 24,       # Manager must validate within 24h
        "validated_to_assigned": 8,     # Assignment within 8h
        "provider_acceptance": 24,      # Provider must accept within 24h
        "assigned_to_completed": 72,    # Intervention within 72h
        "pending_csat": 48,             # Auto-close after 48h
    },
    "priority": {
        "pending_validation": 8,
        "validated_to_assigned": 4,
        "provider_acceptance": 8,
        "assigned_to_completed": 48,
        "pending_csat": 48,
    },
    "emergency": {
        "pending_validation": 2,
        "validated_to_assigned": 1,
        "provider_acceptance": 2,
        "assigned_to_completed": 24,
        "pending_csat": 48,
    },
}


@dataclass
class SLACheck:
    ticket_id: str
    status: str
    urgency: str
    deadline: datetime
    hours_remaining: float
    is_breached: bool
    breach_level: str           # "warning" (< 25% remaining) | "breach" | "critical" (2x over)


# ============================================================
# SLA MONITORING
# ============================================================

def check_ticket_sla(ticket_id: str) -> SLACheck | None:
    """
    Check SLA status for a specific ticket.
    """
    doc = db.collection("tickets").document(ticket_id).get()
    if not doc.exists:
        return None
    
    data = doc.to_dict()
    status = data.get("status", "")
    urgency = data.get("urgency", "normal")
    status_changed_at = data.get("status_changed_at", "")
    
    if not status_changed_at or status in ["closed", "refused"]:
        return None
    
    # Get SLA limit for this status + urgency
    limits = SLA_LIMITS.get(urgency, SLA_LIMITS["normal"])
    sla_key = _status_to_sla_key(status)
    
    if sla_key not in limits:
        return None
    
    limit_hours = limits[sla_key]
    
    # Calculate deadline
    if isinstance(status_changed_at, str):
        changed = datetime.fromisoformat(status_changed_at)
    else:
        changed = status_changed_at
    
    deadline = changed + timedelta(hours=limit_hours)
    now = datetime.utcnow()
    remaining = (deadline - now).total_seconds() / 3600
    
    # Determine breach level
    is_breached = remaining <= 0
    if remaining <= 0:
        if remaining < -limit_hours:  # 2x over SLA
            breach_level = "critical"
        else:
            breach_level = "breach"
    elif remaining < limit_hours * 0.25:  # Less than 25% time remaining
        breach_level = "warning"
    else:
        breach_level = "ok"
    
    return SLACheck(
        ticket_id=ticket_id,
        status=status,
        urgency=urgency,
        deadline=deadline,
        hours_remaining=round(remaining, 1),
        is_breached=is_breached,
        breach_level=breach_level,
    )


def check_all_active_tickets() -> list[SLACheck]:
    """
    Scan all active tickets for SLA status.
    Called periodically by Cloud Scheduler.
    """
    results = []
    
    active_statuses = [
        "pending_validation", "validated", "assigned",
        "planned", "in_progress", "completed_provider", "pending_csat",
    ]
    
    for status in active_statuses:
        docs = db.collection("tickets").where("status", "==", status).stream()
        
        for doc in docs:
            check = check_ticket_sla(doc.id)
            if check and check.breach_level != "ok":
                results.append(check)
                
                # Publish escalation event
                if check.breach_level in ["breach", "critical"]:
                    _publish_escalation(check)
    
    logger.info("sla_scan_complete", extra={
        "json_fields": {
            "warnings": sum(1 for r in results if r.breach_level == "warning"),
            "breaches": sum(1 for r in results if r.breach_level == "breach"),
            "critical": sum(1 for r in results if r.breach_level == "critical"),
        }
    })
    
    return results


# ============================================================
# ESCALATION
# ============================================================

def _publish_escalation(check: SLACheck):
    """Publish SLA breach event to Pub/Sub for notification."""
    try:
        event = {
            "type": "sla.breach",
            "ticket_id": check.ticket_id,
            "status": check.status,
            "urgency": check.urgency,
            "breach_level": check.breach_level,
            "hours_overdue": abs(check.hours_remaining),
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        publisher.publish(
            ESCALATION_TOPIC,
            data=json.dumps(event).encode(),
            ticket_id=check.ticket_id,
            breach_level=check.breach_level,
        )
        
        logger.warning("sla_breach_published", extra={
            "json_fields": event
        })
    
    except Exception as e:
        logger.error(f"Escalation publish error: {e}")


def _status_to_sla_key(status: str) -> str:
    """Map ticket status to SLA limit key."""
    mapping = {
        "pending_validation": "pending_validation",
        "validated": "validated_to_assigned",
        "assigned": "provider_acceptance",
        "planned": "assigned_to_completed",
        "in_progress": "assigned_to_completed",
        "completed_provider": "pending_csat",
        "pending_csat": "pending_csat",
    }
    return mapping.get(status, "")
