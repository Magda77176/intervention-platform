"""
Pydantic schemas — shared contract between all agents.
Same pattern as agent-pipeline: strict typing, JSON-serializable for Pub/Sub.
"""
from enum import Enum
from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
import uuid


# ============================================================
# ENUMS
# ============================================================

class TicketStatus(str, Enum):
    RECEIVED = "received"
    PREQUALIFYING = "prequalifying"
    PENDING_VALIDATION = "pending_validation"
    VALIDATED = "validated"
    REFUSED = "refused"
    ASSIGNING = "assigning"
    ASSIGNED = "assigned"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    PENDING_CSAT = "pending_csat"
    CLOSED = "closed"
    REOPENED = "reopened"


class PayerIndication(str, Enum):
    LANDLORD = "landlord"
    TENANT = "tenant"
    TO_CONFIRM = "to_confirm"


class UrgencyLevel(str, Enum):
    NORMAL = "normal"
    PRIORITY = "priority"
    EMERGENCY = "emergency"


class IncidentCategory(str, Enum):
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"
    HEATING = "heating"
    LOCKSMITH = "locksmith"
    APPLIANCE = "appliance"
    STRUCTURAL = "structural"
    COMMON_AREAS = "common_areas"
    PEST_CONTROL = "pest_control"
    WATER_DAMAGE = "water_damage"
    OTHER = "other"


# ============================================================
# CORE MODELS
# ============================================================

class Ticket(BaseModel):
    """Central data object — flows through every agent."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    description: str
    location: str                                   # logement | parties_communes
    equipment_type: str                             # logement | personnel
    severity_details: str = ""
    since_when: str = ""
    availability: str = ""
    address: str
    address_postal_code: str = ""
    tenant_id: str
    media_urls: list[str] = []

    # Filled by pre-qualification agent
    status: TicketStatus = TicketStatus.RECEIVED
    payer: Optional[PayerIndication] = None
    payer_confidence: float = 0.0
    payer_reason: str = ""
    urgency: UrgencyLevel = UrgencyLevel.NORMAL
    category: Optional[IncidentCategory] = None
    summary: str = ""
    suggested_skills: list[str] = []
    is_duplicate: bool = False
    duplicate_ticket_id: Optional[str] = None
    photo_analysis: Optional[dict] = None

    # Filled by assignment agent
    provider_id: Optional[str] = None
    provider_name: Optional[str] = None
    provider_score: float = 0.0

    # Filled by manager
    manager_decision: Optional[str] = None
    refusal_reason: str = ""

    # Timestamps
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    status_changed_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Provider(BaseModel):
    """Service provider (plumber, electrician, etc.)."""
    id: str
    name: str
    skills: list[str] = []
    zones: list[str] = []
    avg_csat: float = 0.0
    sla_compliance: float = 0.0
    acceptance_rate: float = 0.0
    active_tickets: int = 0
    max_concurrent: int = 5
    hourly_rate: float = 0.0
    is_approved: bool = True


# ============================================================
# AGENT MESSAGE (Pub/Sub payload)
# ============================================================

class AgentMessage(BaseModel):
    """Inter-agent message via Pub/Sub — same pattern as agent-pipeline."""
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    from_agent: str
    to_agent: str
    action: str
    ticket: Ticket
    retry_count: int = 0
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
