"""
Notification Engine — Contextual notifications via Pub/Sub.

Every ticket status change triggers targeted notifications to
the right people (tenant, manager, provider) via the right channel.

Pub/Sub decouples the notification logic from the ticket workflow.
"""
import os
import json
import logging
from datetime import datetime
from enum import Enum

from google.cloud import pubsub_v1

logger = logging.getLogger("intervention")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
publisher = pubsub_v1.PublisherClient()
NOTIFICATION_TOPIC = f"projects/{PROJECT_ID}/topics/notifications"


class NotificationType(Enum):
    TICKET_CREATED = "ticket_created"
    PENDING_VALIDATION = "pending_validation"
    TICKET_VALIDATED = "ticket_validated"
    TICKET_REFUSED = "ticket_refused"
    PROVIDER_ASSIGNED = "provider_assigned"
    PROVIDER_ACCEPTED = "provider_accepted"
    PROVIDER_REFUSED = "provider_refused"
    INTERVENTION_PLANNED = "intervention_planned"
    INTERVENTION_REMINDER = "intervention_reminder"
    INTERVENTION_COMPLETED = "intervention_completed"
    CSAT_REQUEST = "csat_request"
    CSAT_REMINDER = "csat_reminder"
    SLA_WARNING = "sla_warning"
    SLA_BREACH = "sla_breach"
    COMPLEMENT_NEEDED = "complement_needed"
    TICKET_REOPENED = "ticket_reopened"


# ============================================================
# NOTIFICATION TEMPLATES (French)
# ============================================================

TEMPLATES = {
    NotificationType.TICKET_CREATED: {
        "tenant": {
            "subject": "Votre demande a été enregistrée",
            "body": "Votre signalement '{summary}' a bien été reçu. "
                    "Un gestionnaire va l'examiner sous peu. "
                    "Numéro de suivi : {ticket_id}.",
        },
        "manager": {
            "subject": "🔔 Nouveau ticket à valider",
            "body": "Ticket {ticket_id} — {summary}\n"
                    "Pré-qualification : {payer_indication} (confiance : {confidence}%)\n"
                    "Urgence : {urgency}\n"
                    "→ Valider ou refuser dans votre console.",
        },
    },
    NotificationType.TICKET_VALIDATED: {
        "tenant": {
            "subject": "Votre demande est prise en charge",
            "body": "Bonne nouvelle ! Votre demande '{summary}' est validée. "
                    "Nous recherchons un prestataire disponible.",
        },
    },
    NotificationType.TICKET_REFUSED: {
        "tenant": {
            "subject": "Votre demande n'est pas prise en charge",
            "body": "Après examen, votre demande '{summary}' relève de votre responsabilité. "
                    "Raison : {refusal_reason}.",
        },
    },
    NotificationType.PROVIDER_ASSIGNED: {
        "provider": {
            "subject": "📋 Nouvelle intervention proposée",
            "body": "Intervention {ticket_id} — {summary}\n"
                    "Adresse : {address}\n"
                    "Urgence : {urgency}\n"
                    "Merci d'accepter ou refuser sous {deadline_hours}h.",
        },
    },
    NotificationType.INTERVENTION_PLANNED: {
        "tenant": {
            "subject": "Intervention planifiée",
            "body": "L'intervention pour '{summary}' est prévue le {date} "
                    "entre {time_slot}. Le prestataire {provider_name} viendra.",
        },
        "provider": {
            "subject": "📅 Créneau confirmé",
            "body": "Intervention {ticket_id} confirmée le {date} entre {time_slot}.\n"
                    "Adresse : {address}",
        },
    },
    NotificationType.INTERVENTION_COMPLETED: {
        "tenant": {
            "subject": "Intervention terminée — votre avis compte",
            "body": "L'intervention pour '{summary}' est terminée. "
                    "Merci de confirmer que tout est résolu et de noter la prestation.",
        },
        "manager": {
            "subject": "✅ Intervention terminée",
            "body": "Ticket {ticket_id} — intervention terminée par {provider_name}. "
                    "En attente de validation locataire.",
        },
    },
    NotificationType.SLA_WARNING: {
        "manager": {
            "subject": "⚠️ SLA : délai bientôt dépassé",
            "body": "Ticket {ticket_id} — {hours_remaining}h restantes "
                    "avant dépassement SLA (étape : {status}).",
        },
    },
    NotificationType.SLA_BREACH: {
        "manager": {
            "subject": "🚨 SLA DÉPASSÉ",
            "body": "Ticket {ticket_id} — SLA dépassé de {hours_overdue}h ! "
                    "Étape : {status}. Action immédiate requise.",
        },
    },
}


# ============================================================
# SEND NOTIFICATIONS
# ============================================================

def notify(notification_type: NotificationType, ticket_data: dict,
           recipients: list[str] = None):
    """
    Publish a notification event to Pub/Sub.
    
    Recipients are determined by the notification type if not specified.
    The notification worker will handle actual delivery (email, push, SMS).
    """
    templates = TEMPLATES.get(notification_type, {})
    
    if recipients is None:
        recipients = list(templates.keys())
    
    for recipient_type in recipients:
        template = templates.get(recipient_type)
        if not template:
            continue
        
        try:
            # Format template with ticket data
            subject = template["subject"].format(**ticket_data)
            body = template["body"].format(**ticket_data)
        except KeyError as e:
            logger.warning(f"Template format error: {e}")
            subject = template["subject"]
            body = template["body"]
        
        event = {
            "type": notification_type.value,
            "recipient_type": recipient_type,
            "recipient_id": ticket_data.get(f"{recipient_type}_id", ""),
            "subject": subject,
            "body": body,
            "ticket_id": ticket_data.get("ticket_id", ""),
            "channel": "email",  # MVP: email only
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        try:
            publisher.publish(
                NOTIFICATION_TOPIC,
                data=json.dumps(event).encode(),
                notification_type=notification_type.value,
                recipient_type=recipient_type,
            )
            
            logger.info("notification_published", extra={
                "json_fields": {
                    "type": notification_type.value,
                    "recipient": recipient_type,
                    "ticket_id": ticket_data.get("ticket_id", ""),
                }
            })
        
        except Exception as e:
            logger.error(f"Notification publish error: {e}")
