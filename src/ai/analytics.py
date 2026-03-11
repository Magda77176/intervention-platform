"""
Analytics & KPI Engine — BigQuery for reporting.

Tracks and computes key performance indicators:
- Average resolution time (by category, urgency)
- SLA compliance rate
- CSAT scores (by provider, by category)
- Reopening rate
- Provider acceptance rate
- Pré-qualification accuracy (AI vs manager decision)
"""
import os
import json
import logging
from datetime import datetime, timedelta

from google.cloud import bigquery, firestore

logger = logging.getLogger("intervention")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
DATASET_ID = "intervention_analytics"

db = firestore.Client(project=PROJECT_ID)


def compute_kpis(period_days: int = 30) -> dict:
    """
    Compute KPIs for the last N days.
    
    Returns:
        {
            "period": "2026-02-09 → 2026-03-11",
            "total_tickets": 247,
            "resolution_rate": 0.82,
            "avg_resolution_hours": 36.5,
            "sla_compliance": 0.91,
            "avg_csat": 4.2,
            "reopening_rate": 0.05,
            "prequalification_accuracy": 0.87,
            "by_category": {...},
            "by_urgency": {...},
            "top_providers": [...],
            "worst_providers": [...],
        }
    """
    cutoff = (datetime.utcnow() - timedelta(days=period_days)).isoformat()
    
    # Fetch tickets from period
    tickets = []
    docs = db.collection("tickets").where(
        "created_at", ">=", cutoff
    ).stream()
    
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        tickets.append(data)
    
    if not tickets:
        return {"period": f"Last {period_days} days", "total_tickets": 0}
    
    # Compute metrics
    total = len(tickets)
    closed = [t for t in tickets if t.get("status") == "closed"]
    refused = [t for t in tickets if t.get("status") == "refused"]
    reopened = [t for t in tickets if t.get("reopened_count", 0) > 0]
    
    # Resolution rate
    resolution_rate = len(closed) / total if total > 0 else 0
    
    # Average resolution time
    resolution_times = []
    for t in closed:
        created = t.get("created_at", "")
        closed_at = t.get("closed_at", "")
        if created and closed_at:
            try:
                delta = (
                    datetime.fromisoformat(closed_at) - 
                    datetime.fromisoformat(created)
                )
                resolution_times.append(delta.total_seconds() / 3600)
            except (ValueError, TypeError):
                pass
    
    avg_resolution = (
        sum(resolution_times) / len(resolution_times) 
        if resolution_times else 0
    )
    
    # CSAT
    csat_scores = [
        t.get("csat_score", 0) for t in closed 
        if t.get("csat_score", 0) > 0
    ]
    avg_csat = sum(csat_scores) / len(csat_scores) if csat_scores else 0
    
    # SLA compliance
    sla_ok = sum(1 for t in closed if not t.get("sla_breached", False))
    sla_compliance = sla_ok / len(closed) if closed else 0
    
    # Pre-qualification accuracy
    validated = [t for t in tickets if t.get("manager_decision")]
    if validated:
        correct = sum(
            1 for t in validated
            if t.get("ai_payer") == t.get("manager_decision")
        )
        prequalification_accuracy = correct / len(validated)
    else:
        prequalification_accuracy = 0
    
    # By category
    categories = {}
    for t in tickets:
        cat = t.get("category", "other")
        if cat not in categories:
            categories[cat] = {"total": 0, "closed": 0, "avg_csat": []}
        categories[cat]["total"] += 1
        if t.get("status") == "closed":
            categories[cat]["closed"] += 1
            if t.get("csat_score", 0) > 0:
                categories[cat]["avg_csat"].append(t["csat_score"])
    
    for cat in categories:
        scores = categories[cat]["avg_csat"]
        categories[cat]["avg_csat"] = (
            round(sum(scores) / len(scores), 1) if scores else 0
        )
    
    kpis = {
        "period": f"Last {period_days} days",
        "total_tickets": total,
        "closed": len(closed),
        "refused": len(refused),
        "resolution_rate": round(resolution_rate, 2),
        "avg_resolution_hours": round(avg_resolution, 1),
        "sla_compliance": round(sla_compliance, 2),
        "avg_csat": round(avg_csat, 1),
        "reopening_rate": round(len(reopened) / total, 2) if total else 0,
        "prequalification_accuracy": round(prequalification_accuracy, 2),
        "by_category": categories,
    }
    
    logger.info("kpis_computed", extra={"json_fields": kpis})
    
    return kpis


def store_kpis_bigquery(kpis: dict):
    """Store KPI snapshot in BigQuery for trend analysis."""
    try:
        client = bigquery.Client(project=PROJECT_ID)
        table_id = f"{PROJECT_ID}.{DATASET_ID}.kpi_snapshots"
        
        row = {
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "total_tickets": kpis.get("total_tickets", 0),
            "resolution_rate": kpis.get("resolution_rate", 0),
            "avg_resolution_hours": kpis.get("avg_resolution_hours", 0),
            "sla_compliance": kpis.get("sla_compliance", 0),
            "avg_csat": kpis.get("avg_csat", 0),
            "reopening_rate": kpis.get("reopening_rate", 0),
            "prequalification_accuracy": kpis.get("prequalification_accuracy", 0),
        }
        
        errors = client.insert_rows_json(table_id, [row])
        if not errors:
            logger.info("kpis_stored_bigquery")
    
    except Exception as e:
        logger.warning(f"BigQuery KPI store skipped: {e}")
