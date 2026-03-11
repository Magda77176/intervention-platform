"""
Pub/Sub utilities — async event publishing between agents.
Same pattern as agent-pipeline: fire-and-forget, retry-safe.
"""
import os
import json
import logging

from google.cloud import pubsub_v1

logger = logging.getLogger("intervention")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
TOPIC_ID = os.getenv("PUBSUB_TOPIC", "intervention-events")

publisher = pubsub_v1.PublisherClient()
TOPIC_PATH = publisher.topic_path(PROJECT_ID, TOPIC_ID)


def publish_message(message: dict, attributes: dict = None):
    """
    Publish a message to the intervention events topic.
    
    Messages are AgentMessage objects serialized to JSON.
    Attributes are used for filtering by subscribers.
    """
    try:
        data = json.dumps(message, default=str).encode()
        attrs = attributes or {}
        
        future = publisher.publish(TOPIC_PATH, data=data, **attrs)
        message_id = future.result(timeout=5)
        
        logger.info("pubsub_published", extra={
            "json_fields": {
                "message_id": message_id,
                "to_agent": message.get("to_agent", ""),
                "action": message.get("action", ""),
            }
        })
        
        return message_id
    
    except Exception as e:
        logger.error(f"Pub/Sub publish error: {e}")
        return None


def publish_event(event_type: str, ticket_id: str, data: dict = None):
    """
    Publish a system event (SLA breach, notification trigger, etc.)
    """
    event = {
        "type": event_type,
        "ticket_id": ticket_id,
        **(data or {}),
    }
    return publish_message(event, attributes={"event_type": event_type})
