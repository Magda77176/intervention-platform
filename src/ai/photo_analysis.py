"""
Photo Analysis — Vertex AI Vision for incident assessment.

Analyzes tenant-uploaded photos to:
1. Confirm the reported category (plumbing, electrical, etc.)
2. Assess visible severity
3. Detect safety hazards
4. Extract useful details for the property manager
"""
import os
import json
import logging
import re
import base64

import vertexai
from vertexai.generative_models import GenerativeModel, Part

logger = logging.getLogger("intervention")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
MODEL_NAME = os.getenv("VERTEX_MODEL", "gemini-2.0-flash-001")

vertexai.init(project=PROJECT_ID, location="us-central1")
model = GenerativeModel(MODEL_NAME)


ANALYSIS_PROMPT = """Analyse cette photo d'un incident signalé par un locataire.

Description du locataire : "{description}"
Catégorie déclarée : {category}

Analyse la photo et réponds UNIQUEMENT en JSON :
{{
    "photo_confirms_category": true | false,
    "detected_category": "plumbing | electrical | heating | structural | water_damage | other",
    "severity_visual": "low | medium | high | critical",
    "safety_hazards": ["liste des risques de sécurité visibles"],
    "visible_damage": "description courte des dégâts visibles",
    "needs_emergency": true | false,
    "confidence": 0.0-1.0,
    "notes_for_manager": "info utile pour le gestionnaire"
}}

Règles :
- critical = danger immédiat (eau active, fils dénudés, moisissure noire étendue)
- high = dégâts importants mais pas dangereux
- medium = problème visible mais contenu
- low = trace légère, usure normale
- Si la photo est floue ou non pertinente → confidence < 0.3"""


def analyze_incident_photo(image_data: bytes, description: str = "",
                           category: str = "other",
                           mime_type: str = "image/jpeg") -> dict:
    """
    Analyze an incident photo using Gemini Vision.
    
    Args:
        image_data: Raw image bytes
        description: Tenant's text description
        category: Declared category from pre-qualification
        mime_type: Image MIME type
    
    Returns:
        Analysis dict with severity, hazards, and manager notes.
    """
    try:
        image_part = Part.from_data(data=image_data, mime_type=mime_type)
        
        prompt = ANALYSIS_PROMPT.format(
            description=description[:200],
            category=category,
        )
        
        response = model.generate_content(
            [prompt, image_part],
            generation_config={"max_output_tokens": 300, "temperature": 0.1},
        )
        
        json_match = re.search(r'\{[\s\S]*\}', response.text)
        if json_match:
            result = json.loads(json_match.group())
            
            logger.info("photo_analyzed", extra={
                "json_fields": {
                    "detected_category": result.get("detected_category"),
                    "severity": result.get("severity_visual"),
                    "hazards": len(result.get("safety_hazards", [])),
                    "emergency": result.get("needs_emergency"),
                    "confidence": result.get("confidence"),
                }
            })
            
            return result
    
    except Exception as e:
        logger.error(f"Photo analysis error: {e}")
    
    return {
        "photo_confirms_category": False,
        "detected_category": "other",
        "severity_visual": "medium",
        "safety_hazards": [],
        "visible_damage": "Analyse impossible",
        "needs_emergency": False,
        "confidence": 0.0,
        "notes_for_manager": "Photo non analysable — vérification manuelle requise",
    }


def analyze_from_gcs(gcs_uri: str, description: str = "",
                     category: str = "other") -> dict:
    """
    Analyze a photo stored in Cloud Storage.
    
    Args:
        gcs_uri: gs://bucket/path/to/photo.jpg
    """
    try:
        image_part = Part.from_uri(uri=gcs_uri, mime_type="image/jpeg")
        
        prompt = ANALYSIS_PROMPT.format(
            description=description[:200],
            category=category,
        )
        
        response = model.generate_content(
            [prompt, image_part],
            generation_config={"max_output_tokens": 300, "temperature": 0.1},
        )
        
        json_match = re.search(r'\{[\s\S]*\}', response.text)
        if json_match:
            return json.loads(json_match.group())
    
    except Exception as e:
        logger.error(f"GCS photo analysis error: {e}")
    
    return {"confidence": 0.0, "notes_for_manager": "Analyse impossible"}
