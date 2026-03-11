"""
MCP Server — Shared tools for all agents.
Same pattern as agent-pipeline: FastAPI exposing discoverable tools.

Tools:
1. search_address    → Geocode + validate address
2. check_duplicate   → Find similar recent tickets
3. analyze_photo     → Gemini Vision for incident photos
4. lookup_provider   → Query provider pool from Firestore
5. send_notification → Email via SendGrid / SMTP
"""
import os
import json
import re
import logging
from datetime import datetime, timedelta

from fastapi import FastAPI
from pydantic import BaseModel

import vertexai
from vertexai.generative_models import GenerativeModel, Part
from google.cloud import firestore

logger = logging.getLogger("intervention")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
MODEL_NAME = os.getenv("VERTEX_MODEL", "gemini-2.0-flash-001")

vertexai.init(project=PROJECT_ID, location="us-central1")
model = GenerativeModel(MODEL_NAME)
db = firestore.Client(project=PROJECT_ID)

app = FastAPI(title="MCP Tools Server", version="1.0.0")


# ============================================================
# TOOL DISCOVERY (MCP pattern)
# ============================================================

TOOLS = [
    {
        "name": "search_address",
        "description": "Validate and normalize a French address, extract postal code",
        "parameters": {"address": "string"},
    },
    {
        "name": "check_duplicate",
        "description": "Check if a similar ticket exists for the same address within 7 days",
        "parameters": {"address": "string", "category": "string"},
    },
    {
        "name": "analyze_photo",
        "description": "Analyze incident photo using Gemini Vision — detect severity and hazards",
        "parameters": {"gcs_uri": "string", "description": "string"},
    },
    {
        "name": "lookup_providers",
        "description": "Find available providers matching skills and zone",
        "parameters": {"skills": "list[string]", "postal_code": "string"},
    },
    {
        "name": "send_notification",
        "description": "Send email notification to tenant/manager/provider",
        "parameters": {"to": "string", "subject": "string", "body": "string"},
    },
]


@app.get("/tools")
async def list_tools():
    """MCP discovery endpoint — agents call this to know what tools exist."""
    return {"tools": TOOLS}


# ============================================================
# TOOL 1: ADDRESS VALIDATION
# ============================================================

class AddressRequest(BaseModel):
    address: str

@app.post("/tools/search_address")
async def search_address(req: AddressRequest):
    """Validate and normalize a French address."""
    address = req.address.strip()
    
    # Extract postal code
    postal_match = re.search(r'\b(\d{5})\b', address)
    postal_code = postal_match.group(1) if postal_match else ""
    
    # Normalize
    normalized = address.lower().strip()
    for noise in ["rue de la ", "rue du ", "rue des ", "avenue ", "boulevard ", "place "]:
        normalized = normalized.replace(noise, "")
    normalized = " ".join(normalized.split())
    
    return {
        "address": address,
        "normalized": normalized,
        "postal_code": postal_code,
        "department": postal_code[:2] if postal_code else "",
        "valid": bool(postal_code),
    }


# ============================================================
# TOOL 2: DUPLICATE CHECK
# ============================================================

class DuplicateRequest(BaseModel):
    address: str
    category: str

CATEGORY_KEYWORDS = {
    "plumbing": {"fuite", "eau", "robinet", "tuyau", "canalisation", "plomberie"},
    "electrical": {"électricité", "prise", "disjoncteur", "lumière", "court-circuit"},
    "heating": {"chaudière", "chauffage", "radiateur", "eau chaude", "climatisation"},
    "locksmith": {"serrure", "porte", "clé", "verrou"},
    "pest_control": {"cafard", "souris", "rat", "nuisible", "insecte"},
    "water_damage": {"dégât des eaux", "inondation", "fuite"},
}

@app.post("/tools/check_duplicate")
async def check_duplicate(req: DuplicateRequest):
    """Check for similar recent tickets at the same address."""
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    
    try:
        # Normalize address for matching
        addr_key = req.address.lower().strip()[:50]
        
        docs = db.collection("tickets").where(
            "created_at", ">=", cutoff
        ).limit(50).stream()
        
        for doc in docs:
            data = doc.to_dict()
            if (data.get("address", "").lower().strip()[:50] == addr_key
                and data.get("category") == req.category
                and data.get("status") not in ["closed", "refused"]):
                return {
                    "is_duplicate": True,
                    "duplicate_ticket_id": doc.id,
                    "original_status": data.get("status"),
                }
        
        return {"is_duplicate": False, "duplicate_ticket_id": None}
    
    except Exception as e:
        logger.error(f"Duplicate check error: {e}")
        return {"is_duplicate": False, "error": str(e)}


# ============================================================
# TOOL 3: PHOTO ANALYSIS (Gemini Vision)
# ============================================================

PHOTO_PROMPT = """Analyse cette photo d'un incident signalé par un locataire.
Description : "{description}"

Réponds en JSON :
{{
    "severity": "low | medium | high | critical",
    "safety_hazards": ["risques visibles"],
    "visible_damage": "description courte",
    "needs_emergency": true | false,
    "confidence": 0.0-1.0
}}"""

class PhotoRequest(BaseModel):
    gcs_uri: str
    description: str = ""

@app.post("/tools/analyze_photo")
async def analyze_photo(req: PhotoRequest):
    """Analyze incident photo using Gemini Vision."""
    try:
        image = Part.from_uri(uri=req.gcs_uri, mime_type="image/jpeg")
        prompt = PHOTO_PROMPT.format(description=req.description[:200])
        
        response = model.generate_content(
            [prompt, image],
            generation_config={"max_output_tokens": 200, "temperature": 0.1},
        )
        
        match = re.search(r'\{[\s\S]*\}', response.text)
        if match:
            return json.loads(match.group())
    
    except Exception as e:
        logger.error(f"Photo analysis error: {e}")
    
    return {"severity": "medium", "confidence": 0.0, "error": "analysis_failed"}


# ============================================================
# TOOL 4: PROVIDER LOOKUP
# ============================================================

class ProviderLookupRequest(BaseModel):
    skills: list[str]
    postal_code: str = ""

@app.post("/tools/lookup_providers")
async def lookup_providers(req: ProviderLookupRequest):
    """Find available providers matching skills and zone."""
    try:
        docs = db.collection("providers").where(
            "is_active", "==", True
        ).stream()
        
        matches = []
        for doc in docs:
            data = doc.to_dict()
            p_skills = [s.lower() for s in data.get("skills", [])]
            
            # Check skill match
            skill_match = any(
                s.lower() in p_skills for s in req.skills
            )
            
            # Check zone match
            zone_match = (
                not req.postal_code
                or req.postal_code in data.get("zones", [])
                or any(z.startswith(req.postal_code[:2]) for z in data.get("zones", []))
            )
            
            if skill_match and zone_match:
                matches.append({
                    "id": doc.id,
                    "name": data.get("name"),
                    "skills": data.get("skills"),
                    "avg_csat": data.get("avg_csat", 0),
                    "active_tickets": data.get("active_tickets", 0),
                })
        
        return {"providers": matches, "count": len(matches)}
    
    except Exception as e:
        return {"providers": [], "error": str(e)}


# ============================================================
# TOOL 5: NOTIFICATION
# ============================================================

class NotificationRequest(BaseModel):
    to: str
    subject: str
    body: str

@app.post("/tools/send_notification")
async def send_notification(req: NotificationRequest):
    """Send email notification (placeholder — integrate SendGrid/SMTP)."""
    # Log notification (actual sending via SendGrid in production)
    logger.info("notification_sent", extra={
        "json_fields": {
            "to": req.to,
            "subject": req.subject,
        }
    })
    
    return {"status": "sent", "to": req.to}


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():
    return {"status": "healthy", "tools": len(TOOLS), "service": "mcp-tools"}
