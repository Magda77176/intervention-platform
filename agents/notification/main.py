"""
Notification Agent — DLP scan + contextual notifications.
Same role as Notification in agent-pipeline: scan for PII, then send.
"""
import os
import json
import logging

from fastapi import FastAPI
from pydantic import BaseModel

from google.cloud import dlp_v2
from shared.tracing import init_tracing
from shared.pubsub import publish_event

logger = logging.getLogger("intervention")
tracer = init_tracing("notification")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
dlp_client = dlp_v2.DlpServiceClient()

app = FastAPI(title="Notification Agent", version="1.0.0")


# DLP config — scan outgoing notifications for PII
DLP_INFO_TYPES = [
    {"name": "PHONE_NUMBER"},
    {"name": "EMAIL_ADDRESS"},
    {"name": "CREDIT_CARD_NUMBER"},
    {"name": "IBAN_CODE"},
    {"name": "FRANCE_NIR"},
]


# Notification templates (French)
TEMPLATES = {
    "ticket_created": {
        "manager": {
            "subject": "🔔 Nouveau ticket à valider — {ticket_id}",
            "body": "Ticket {ticket_id}\n\nRésumé : {summary}\nPré-qualification : {payer} (confiance {payer_confidence}%)\nUrgence : {urgency}\nCatégorie : {category}\n\n→ Valider ou refuser dans la console gestionnaire.",
        },
    },
    "ticket_validated": {
        "tenant": {
            "subject": "Votre demande est prise en charge",
            "body": "Bonne nouvelle, votre demande est validée. Un prestataire va être assigné.",
        },
        "provider": {
            "subject": "📋 Nouvelle intervention — {ticket_id}",
            "body": "Intervention {ticket_id}\nRésumé : {summary}\nAdresse : {address}\nUrgence : {urgency}\n\nMerci d'accepter ou refuser.",
        },
    },
    "ticket_refused": {
        "tenant": {
            "subject": "Votre demande n'est pas prise en charge",
            "body": "Après examen, votre demande relève de votre responsabilité.\nRaison : {refusal_reason}",
        },
    },
}


class NotifyRequest(BaseModel):
    type: str
    ticket: dict
    recipients: list[str] = []


@app.post("/notify")
async def notify(req: NotifyRequest):
    """
    Send contextual notifications with DLP scan.
    1. Pick template based on notification type
    2. Fill with ticket data
    3. DLP scan the outgoing text
    4. If clean → send (email)
    5. If PII found → log warning, redact, then send
    """
    with tracer.start_as_current_span("notification.send") as span:
        templates = TEMPLATES.get(req.type, {})
        sent = []
        blocked = []
        
        for recipient_type in req.recipients:
            template = templates.get(recipient_type)
            if not template:
                continue
            
            # Fill template
            try:
                subject = template["subject"].format(**req.ticket)
                body = template["body"].format(**req.ticket)
            except KeyError:
                subject = template["subject"]
                body = template["body"]
            
            # DLP scan outgoing notification
            with tracer.start_as_current_span("dlp.scan_notification"):
                scan_result = _dlp_scan(body)
            
            if scan_result["has_pii"]:
                logger.warning("pii_in_notification", extra={
                    "json_fields": {
                        "type": req.type,
                        "recipient": recipient_type,
                        "pii_types": scan_result["types"],
                    }
                })
                # Redact PII before sending
                body = _dlp_redact(body)
            
            # Send (log for now — integrate email service in prod)
            logger.info("notification_sent", extra={
                "json_fields": {
                    "type": req.type,
                    "recipient": recipient_type,
                    "subject": subject,
                    "ticket_id": req.ticket.get("id", ""),
                    "pii_redacted": scan_result["has_pii"],
                }
            })
            
            sent.append(recipient_type)
        
        # Pub/Sub event
        publish_event("notification.sent", req.ticket.get("id", ""), {
            "type": req.type,
            "recipients": sent,
        })
        
        return {"sent": sent, "blocked": blocked}


def _dlp_scan(text: str) -> dict:
    """Scan text for PII using Cloud DLP."""
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
            "has_pii": len(findings) > 0,
            "count": len(findings),
            "types": list(set(f.info_type.name for f in findings)),
        }
    except Exception:
        return {"has_pii": False, "count": 0, "types": []}


def _dlp_redact(text: str) -> str:
    """Redact PII from text using Cloud DLP deidentify."""
    try:
        response = dlp_client.deidentify_content(
            request={
                "parent": f"projects/{PROJECT_ID}/locations/global",
                "item": {"value": text},
                "deidentify_config": {
                    "info_type_transformations": {
                        "transformations": [{
                            "primitive_transformation": {
                                "replace_config": {
                                    "new_value": {"string_value": "[REDACTED]"}
                                }
                            }
                        }]
                    }
                },
                "inspect_config": {
                    "info_types": DLP_INFO_TYPES,
                },
            }
        )
        return response.item.value
    except Exception:
        return text


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "notification"}
