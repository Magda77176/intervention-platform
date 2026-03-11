"""
Pre-qualification Agent — Vertex AI Gemini for ticket triage.
Same role as Qualification agent in agent-pipeline: call LLM, return structured result.
"""
import os
import re
import json
import time
import logging

from fastapi import FastAPI
import vertexai
from vertexai.generative_models import GenerativeModel

from shared.schemas import Ticket
from shared.tracing import init_tracing
from google.cloud import dlp_v2

logger = logging.getLogger("intervention")
tracer = init_tracing("prequalification")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
MODEL_NAME = os.getenv("VERTEX_MODEL", "gemini-2.0-flash-001")

vertexai.init(project=PROJECT_ID, location="us-central1")
model = GenerativeModel(MODEL_NAME)

# DLP client for scanning tenant PII in descriptions
dlp_client = dlp_v2.DlpServiceClient()

app = FastAPI(title="Pre-qualification Agent", version="1.0.0")


QUALIFICATION_PROMPT = """Tu es un expert en gestion immobilière. Analyse cette demande d'intervention.

DESCRIPTION : {description}
LOCALISATION : {location}
ÉQUIPEMENT : {equipment_type}
GRAVITÉ : {severity_details}
DEPUIS QUAND : {since_when}

RÈGLES :
- BAILLEUR : équipement du logement (chaudière, volets, plomberie), parties communes, structure, vétusté
- LOCATAIRE : appareil personnel (lave-linge perso), dégâts causés par le locataire, entretien courant
- À CONFIRMER : ambiguïté, besoin diagnostic
- URGENCE : fuite de gaz, étincelles, inondation active, plus de chauffage en hiver

JSON uniquement :
{{"payer": "landlord|tenant|to_confirm", "payer_confidence": 0.0-1.0, "payer_reason": "...", "urgency": "normal|priority|emergency", "category": "plumbing|electrical|heating|locksmith|appliance|structural|common_areas|pest_control|water_damage|other", "summary": "résumé 1-2 phrases", "suggested_skills": ["..."]}}"""


# ============================================================
# DLP SCAN (strip PII from tenant description before logging)
# ============================================================

DLP_INFO_TYPES = [
    {"name": "PHONE_NUMBER"},
    {"name": "EMAIL_ADDRESS"},
    {"name": "FRANCE_NIR"},
    {"name": "IBAN_CODE"},
]

def scan_pii(text: str) -> dict:
    """Scan text for PII using Cloud DLP. Returns findings count."""
    try:
        response = dlp_client.inspect_content(
            request={
                "parent": f"projects/{PROJECT_ID}/locations/global",
                "item": {"value": text},
                "inspect_config": {
                    "info_types": DLP_INFO_TYPES,
                    "min_likelihood": dlp_v2.Likelihood.LIKELY,
                },
            }
        )
        findings = response.result.findings
        return {
            "pii_found": len(findings) > 0,
            "count": len(findings),
            "types": list(set(f.info_type.name for f in findings)),
        }
    except Exception:
        return {"pii_found": False, "count": 0, "types": []}


# ============================================================
# MAIN ENDPOINT
# ============================================================

@app.post("/prequalify")
async def prequalify(ticket: Ticket):
    """
    Classify a ticket using Vertex AI Gemini.
    Returns: payer, urgency, category, summary, suggested_skills.
    """
    with tracer.start_as_current_span("prequalification.classify") as span:
        t0 = time.time()
        
        # DLP scan on description (log PII presence, don't block)
        with tracer.start_as_current_span("dlp.scan_description"):
            pii = scan_pii(ticket.description)
            if pii["pii_found"]:
                logger.warning("pii_in_description", extra={
                    "json_fields": {"types": pii["types"], "count": pii["count"]}
                })
        
        # Call Gemini
        prompt = QUALIFICATION_PROMPT.format(
            description=ticket.description,
            location=ticket.location,
            equipment_type=ticket.equipment_type,
            severity_details=ticket.severity_details,
            since_when=ticket.since_when,
        )
        
        try:
            with tracer.start_as_current_span("vertex_ai.generate"):
                response = model.generate_content(
                    prompt,
                    generation_config={"max_output_tokens": 300, "temperature": 0.1},
                )
            
            match = re.search(r'\{[\s\S]*\}', response.text)
            if match:
                result = json.loads(match.group())
            else:
                result = _fallback(ticket)
        
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            result = _fallback(ticket)
        
        latency = int((time.time() - t0) * 1000)
        span.set_attribute("latency_ms", latency)
        span.set_attribute("payer", result.get("payer", ""))
        span.set_attribute("urgency", result.get("urgency", ""))
        
        logger.info("prequalification_done", extra={
            "json_fields": {
                "ticket_id": ticket.id,
                "payer": result.get("payer"),
                "urgency": result.get("urgency"),
                "category": result.get("category"),
                "confidence": result.get("payer_confidence"),
                "latency_ms": latency,
                "pii_found": pii.get("pii_found", False),
            }
        })
        
        return result


def _fallback(ticket: Ticket) -> dict:
    """Rule-based fallback if Gemini is unavailable."""
    desc = ticket.description.lower()
    
    EMERGENCY = {"gaz", "fuite de gaz", "étincelles", "inondation", "court-circuit"}
    TENANT = {"lave-linge", "sèche-linge", "ampoule", "joint", "détartrage"}
    LANDLORD = {"chaudière", "radiateur", "volet", "fenêtre", "canalisation", "parties communes"}
    
    urgency = "emergency" if any(k in desc for k in EMERGENCY) else "normal"
    payer = "to_confirm"
    if any(k in desc for k in TENANT):
        payer = "tenant"
    elif any(k in desc for k in LANDLORD):
        payer = "landlord"
    
    return {
        "payer": payer, "payer_confidence": 0.5,
        "payer_reason": "Classification par règles",
        "urgency": urgency, "category": "other",
        "summary": desc[:100], "suggested_skills": [],
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "prequalification"}
