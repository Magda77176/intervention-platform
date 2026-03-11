"""
AI Pre-qualification Engine — Vertex AI Gemini

Analyzes tenant incident reports and automatically determines:
1. Who pays (landlord / tenant / to confirm)
2. Urgency level (normal / priority / emergency)
3. Category (plumbing, electrical, heating, etc.)
4. Deduplication (same address + same type within 7 days)

Runs on every new ticket before reaching the property manager.
"""
import os
import json
import logging
import re
from datetime import datetime, timedelta
from enum import Enum

import vertexai
from vertexai.generative_models import GenerativeModel
from google.cloud import firestore

logger = logging.getLogger("intervention")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
MODEL_NAME = os.getenv("VERTEX_MODEL", "gemini-2.0-flash-001")

vertexai.init(project=PROJECT_ID, location="us-central1")
model = GenerativeModel(MODEL_NAME)
db = firestore.Client(project=PROJECT_ID)


# ============================================================
# ENUMS
# ============================================================

class PayerIndication(Enum):
    LANDLORD = "landlord"           # A priori bailleur
    TENANT = "tenant"               # A priori locataire
    TO_CONFIRM = "to_confirm"       # À confirmer par gestionnaire


class UrgencyLevel(Enum):
    NORMAL = "normal"
    PRIORITY = "priority"           # Needs attention within 48h
    EMERGENCY = "emergency"         # Safety risk — immediate


class IncidentCategory(Enum):
    PLUMBING = "plumbing"           # Plomberie
    ELECTRICAL = "electrical"       # Électricité
    HEATING = "heating"             # Chauffage / climatisation
    LOCKSMITH = "locksmith"         # Serrurerie
    APPLIANCE = "appliance"         # Électroménager
    STRUCTURAL = "structural"       # Structure / gros œuvre
    COMMON_AREAS = "common_areas"   # Parties communes
    PEST_CONTROL = "pest_control"   # Nuisibles
    WATER_DAMAGE = "water_damage"   # Dégât des eaux
    OTHER = "other"


# ============================================================
# QUALIFICATION PROMPT
# ============================================================

QUALIFICATION_PROMPT = """Tu es un expert en gestion immobilière. Analyse cette demande d'intervention d'un locataire.

DESCRIPTION DU PROBLÈME :
{description}

RÉPONSES AU QUESTIONNAIRE :
- Localisation : {location} (logement / parties communes)
- Équipement du logement ou appareil personnel : {equipment_type}
- Gravité/sécurité : {severity_details}
- Depuis quand : {since_when}
- Disponibilités : {availability}

RÈGLES DE PRÉ-QUALIFICATION :
1. BAILLEUR paie si : équipement du logement (chaudière, volets, plomberie encastrée), parties communes, structure, vétusté
2. LOCATAIRE paie si : appareil personnel (lave-linge, sèche-linge perso), dégâts causés par le locataire, entretien courant (joints, ampoules, détartrage)
3. À CONFIRMER si : ambiguïté sur la cause, besoin de diagnostic
4. URGENCE si : fuite de gaz, étincelles, inondation active, plus de chauffage en hiver, serrure bloquée (nuit)

Réponds UNIQUEMENT en JSON :
{{
    "payer": "landlord" | "tenant" | "to_confirm",
    "payer_confidence": 0.0-1.0,
    "payer_reason": "explication courte en français",
    "urgency": "normal" | "priority" | "emergency",
    "urgency_reason": "explication si priority/emergency",
    "category": "plumbing" | "electrical" | "heating" | "locksmith" | "appliance" | "structural" | "common_areas" | "pest_control" | "water_damage" | "other",
    "summary": "résumé en 1-2 phrases pour le gestionnaire",
    "suggested_skills": ["compétence prestataire requise"]
}}"""


# ============================================================
# MAIN QUALIFICATION FUNCTION
# ============================================================

def prequalify_ticket(ticket_data: dict) -> dict:
    """
    Run AI pre-qualification on a new ticket.
    
    Input:
        ticket_data: {
            "description": "Ma chaudière ne démarre plus...",
            "location": "logement",
            "equipment_type": "équipement du logement",
            "severity_details": "Plus de chauffage ni eau chaude",
            "since_when": "depuis hier soir",
            "availability": "tous les jours sauf mardi",
            "address": "12 rue de la Paix, 75002 Paris",
            "tenant_id": "tenant_123",
            "media_urls": ["gs://bucket/photo1.jpg"]
        }
    
    Output:
        {
            "payer": "landlord",
            "payer_confidence": 0.92,
            "payer_reason": "Chaudière = équipement du logement, panne non causée par le locataire",
            "urgency": "priority",
            "urgency_reason": "Plus de chauffage — confort de base compromis",
            "category": "heating",
            "summary": "Panne chaudière, plus de chauffage ni eau chaude depuis hier soir",
            "suggested_skills": ["chauffagiste"],
            "is_duplicate": false,
            "duplicate_ticket_id": null
        }
    """
    # Step 1: Check for duplicates
    duplicate = check_duplicate(
        address=ticket_data.get("address", ""),
        category_hint=ticket_data.get("description", ""),
        tenant_id=ticket_data.get("tenant_id", ""),
    )
    
    # Step 2: AI classification
    prompt = QUALIFICATION_PROMPT.format(
        description=ticket_data.get("description", ""),
        location=ticket_data.get("location", "non précisé"),
        equipment_type=ticket_data.get("equipment_type", "non précisé"),
        severity_details=ticket_data.get("severity_details", "non précisé"),
        since_when=ticket_data.get("since_when", "non précisé"),
        availability=ticket_data.get("availability", "non précisé"),
    )
    
    try:
        response = model.generate_content(
            prompt,
            generation_config={"max_output_tokens": 300, "temperature": 0.1},
        )
        
        json_match = re.search(r'\{[\s\S]*\}', response.text)
        if json_match:
            result = json.loads(json_match.group())
        else:
            result = _fallback_classification(ticket_data)
    
    except Exception as e:
        logger.error(f"Prequalification error: {e}")
        result = _fallback_classification(ticket_data)
    
    # Step 3: Attach duplicate info
    result["is_duplicate"] = duplicate is not None
    result["duplicate_ticket_id"] = duplicate
    
    # Step 4: Log to Firestore
    db.collection("prequalification_logs").add({
        "ticket_data": ticket_data,
        "result": result,
        "model": MODEL_NAME,
        "created_at": datetime.utcnow().isoformat(),
    })
    
    logger.info("prequalification_complete", extra={
        "json_fields": {
            "payer": result.get("payer"),
            "urgency": result.get("urgency"),
            "category": result.get("category"),
            "confidence": result.get("payer_confidence"),
            "is_duplicate": result.get("is_duplicate"),
        }
    })
    
    return result


# ============================================================
# DEDUPLICATION
# ============================================================

def check_duplicate(address: str, category_hint: str, tenant_id: str,
                    window_days: int = 7) -> str | None:
    """
    Check if a similar ticket exists for the same address within the last N days.
    Returns the ticket_id if duplicate found, None otherwise.
    """
    if not address:
        return None
    
    cutoff = (datetime.utcnow() - timedelta(days=window_days)).isoformat()
    
    try:
        # Query recent tickets at same address
        query = (
            db.collection("tickets")
            .where("address_normalized", "==", _normalize_address(address))
            .where("created_at", ">=", cutoff)
            .where("status", "not-in", ["closed", "refused"])
            .limit(5)
        )
        
        docs = query.stream()
        
        for doc in docs:
            data = doc.to_dict()
            # Check category similarity
            if _categories_match(category_hint, data.get("description", "")):
                logger.info("duplicate_detected", extra={
                    "json_fields": {
                        "original_ticket": doc.id,
                        "address": address[:30],
                    }
                })
                return doc.id
    
    except Exception as e:
        logger.warning(f"Deduplication query error: {e}")
    
    return None


def _normalize_address(address: str) -> str:
    """Normalize address for comparison."""
    addr = address.lower().strip()
    # Remove common noise
    for word in ["rue de la", "rue du", "rue des", "avenue", "boulevard", "place"]:
        addr = addr.replace(word, "")
    return " ".join(addr.split())


def _categories_match(desc1: str, desc2: str) -> bool:
    """Check if two descriptions likely refer to the same type of issue."""
    keywords_groups = [
        {"chaudière", "chauffage", "radiateur", "eau chaude"},
        {"fuite", "eau", "plomberie", "robinet", "tuyau", "canalisation"},
        {"électricité", "prise", "disjoncteur", "lumière", "court-circuit"},
        {"serrure", "porte", "clé", "verrou"},
        {"cafard", "souris", "rat", "nuisible", "insecte"},
    ]
    
    d1 = desc1.lower()
    d2 = desc2.lower()
    
    for group in keywords_groups:
        if any(kw in d1 for kw in group) and any(kw in d2 for kw in group):
            return True
    
    return False


# ============================================================
# FALLBACK (rule-based, no LLM)
# ============================================================

EMERGENCY_KEYWORDS = {
    "gaz", "fuite de gaz", "odeur de gaz",
    "étincelles", "court-circuit", "fumée",
    "inondation", "grosse fuite", "dégât des eaux",
    "serrure bloquée", "enfermé",
}

TENANT_KEYWORDS = {
    "lave-linge", "sèche-linge", "machine à laver",
    "ampoule", "joint", "détartrage",
    "appareil personnel",
}

LANDLORD_KEYWORDS = {
    "chaudière", "radiateur", "volet", "fenêtre",
    "plomberie", "canalisation", "toiture",
    "parties communes", "ascenseur", "interphone",
}


def _fallback_classification(ticket_data: dict) -> dict:
    """Rule-based fallback if Gemini is unavailable."""
    desc = ticket_data.get("description", "").lower()
    severity = ticket_data.get("severity_details", "").lower()
    
    # Urgency
    urgency = "normal"
    if any(kw in desc or kw in severity for kw in EMERGENCY_KEYWORDS):
        urgency = "emergency"
    
    # Payer
    payer = "to_confirm"
    if any(kw in desc for kw in TENANT_KEYWORDS):
        payer = "tenant"
    elif any(kw in desc for kw in LANDLORD_KEYWORDS):
        payer = "landlord"
    
    return {
        "payer": payer,
        "payer_confidence": 0.5,
        "payer_reason": "Classification par règles (LLM indisponible)",
        "urgency": urgency,
        "urgency_reason": "",
        "category": "other",
        "summary": desc[:100],
        "suggested_skills": [],
    }
