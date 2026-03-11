"""
Provider Scoring Engine — AI-powered contractor assignment.

Scores and ranks service providers based on:
- Skills match (plumber for plumbing, electrician for electrical)
- Availability (calendar, current workload)
- Zone coverage (distance to intervention site)
- Historical performance (CSAT, SLA compliance, acceptance rate)
- Pricing tier

Used by Process #4 (Assignation Prestataire) for auto-dispatch
or to present a ranked list for manual selection.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict

from google.cloud import firestore

logger = logging.getLogger("intervention")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "jarvis-v2-488311")
db = firestore.Client(project=PROJECT_ID)


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class Provider:
    id: str
    name: str
    skills: list[str]           # ["plumbing", "heating"]
    zones: list[str]            # ["75002", "75003", "75004"]
    avg_csat: float = 0.0       # 0-5
    sla_compliance: float = 0.0  # 0-1 (% of interventions within SLA)
    acceptance_rate: float = 0.0  # 0-1
    active_tickets: int = 0
    max_concurrent: int = 5
    hourly_rate: float = 0.0
    is_approved: bool = True     # Réseau agréé


@dataclass
class ScoringResult:
    provider_id: str
    provider_name: str
    total_score: float          # 0-100
    breakdown: dict             # Detail per criterion
    available: bool
    reason: str = ""            # Why unavailable or why top ranked


# ============================================================
# SCORING WEIGHTS
# ============================================================

WEIGHTS = {
    "skills_match": 30,         # Does the provider have the right skills?
    "zone_match": 20,           # Is the provider in the right area?
    "performance": 25,          # CSAT + SLA compliance
    "availability": 15,         # Current workload vs capacity
    "pricing": 10,              # Cost efficiency
}


# ============================================================
# SCORING ENGINE
# ============================================================

def score_providers(ticket: dict, providers: list[Provider] = None) -> list[ScoringResult]:
    """
    Score and rank all eligible providers for a ticket.
    
    Input:
        ticket: {
            "category": "plumbing",
            "suggested_skills": ["plombier"],
            "urgency": "priority",
            "address_postal_code": "75002",
        }
    
    Output:
        Sorted list of ScoringResult (highest score first).
    """
    if providers is None:
        providers = _load_providers()
    
    required_skills = ticket.get("suggested_skills", [])
    category = ticket.get("category", "other")
    postal_code = ticket.get("address_postal_code", "")
    urgency = ticket.get("urgency", "normal")
    
    results = []
    
    for provider in providers:
        if not provider.is_approved:
            continue
        
        breakdown = {}
        
        # 1. Skills match (0-100)
        skills_score = _score_skills(provider.skills, required_skills, category)
        breakdown["skills_match"] = skills_score
        
        # 2. Zone match (0-100)
        zone_score = _score_zone(provider.zones, postal_code)
        breakdown["zone_match"] = zone_score
        
        # 3. Performance (0-100)
        perf_score = _score_performance(provider.avg_csat, provider.sla_compliance)
        breakdown["performance"] = perf_score
        
        # 4. Availability (0-100)
        avail_score = _score_availability(
            provider.active_tickets, provider.max_concurrent, urgency
        )
        breakdown["availability"] = avail_score
        
        # 5. Pricing (0-100) — lower is better for standard, less weight for emergency
        pricing_score = _score_pricing(provider.hourly_rate, urgency)
        breakdown["pricing"] = pricing_score
        
        # Weighted total
        total = sum(
            breakdown[k] * (WEIGHTS[k] / 100)
            for k in WEIGHTS
        )
        
        # Availability check
        available = (
            skills_score > 0
            and zone_score > 0
            and provider.active_tickets < provider.max_concurrent
        )
        
        reason = ""
        if not available:
            if skills_score == 0:
                reason = "Compétences non adaptées"
            elif zone_score == 0:
                reason = "Hors zone d'intervention"
            else:
                reason = "Capacité maximale atteinte"
        
        results.append(ScoringResult(
            provider_id=provider.id,
            provider_name=provider.name,
            total_score=round(total, 1),
            breakdown={k: round(v, 1) for k, v in breakdown.items()},
            available=available,
            reason=reason,
        ))
    
    # Sort: available first, then by score
    results.sort(key=lambda r: (r.available, r.total_score), reverse=True)
    
    logger.info("providers_scored", extra={
        "json_fields": {
            "category": category,
            "postal_code": postal_code,
            "total_providers": len(providers),
            "available": sum(1 for r in results if r.available),
            "top_provider": results[0].provider_name if results else None,
            "top_score": results[0].total_score if results else 0,
        }
    })
    
    return results


# ============================================================
# SCORING FUNCTIONS
# ============================================================

CATEGORY_TO_SKILLS = {
    "plumbing": ["plombier", "plomberie"],
    "electrical": ["électricien", "électricité"],
    "heating": ["chauffagiste", "climatisation", "chauffage"],
    "locksmith": ["serrurier", "serrurerie"],
    "appliance": ["électroménager", "dépanneur"],
    "structural": ["maçon", "maçonnerie", "gros œuvre"],
    "common_areas": ["multiservice", "entretien"],
    "pest_control": ["dératisation", "désinsectisation"],
    "water_damage": ["plombier", "assèchement", "dégât des eaux"],
}


def _score_skills(provider_skills: list[str], required: list[str], 
                  category: str) -> float:
    """Score 0-100 based on skill match."""
    if not provider_skills:
        return 0
    
    p_skills = [s.lower() for s in provider_skills]
    
    # Direct match with required skills
    if required:
        for req in required:
            if req.lower() in p_skills:
                return 100
    
    # Category-based match
    category_skills = CATEGORY_TO_SKILLS.get(category, [])
    for cs in category_skills:
        if cs in p_skills:
            return 80
    
    # "Multiservice" providers get a base score
    if "multiservice" in p_skills:
        return 40
    
    return 0


def _score_zone(provider_zones: list[str], postal_code: str) -> float:
    """Score 0-100 based on zone coverage."""
    if not postal_code or not provider_zones:
        return 50  # Unknown → neutral
    
    # Exact match
    if postal_code in provider_zones:
        return 100
    
    # Same department (first 2 digits)
    dept = postal_code[:2]
    if any(z.startswith(dept) for z in provider_zones):
        return 60
    
    # Same region (first digit for IDF)
    if any(z[0] == postal_code[0] for z in provider_zones):
        return 30
    
    return 0


def _score_performance(avg_csat: float, sla_compliance: float) -> float:
    """Score 0-100 based on historical performance."""
    # CSAT: 0-5 scale → 0-50 points
    csat_score = (avg_csat / 5) * 50 if avg_csat > 0 else 25
    
    # SLA compliance: 0-1 → 0-50 points
    sla_score = sla_compliance * 50 if sla_compliance > 0 else 25
    
    return csat_score + sla_score


def _score_availability(active_tickets: int, max_concurrent: int,
                        urgency: str) -> float:
    """Score 0-100 based on current workload."""
    if max_concurrent == 0:
        return 0
    
    load_ratio = active_tickets / max_concurrent
    
    if load_ratio >= 1.0:
        return 0  # Full
    
    base_score = (1 - load_ratio) * 100
    
    # Emergency bonus for low-load providers
    if urgency == "emergency" and load_ratio < 0.3:
        base_score = min(100, base_score + 20)
    
    return base_score


def _score_pricing(hourly_rate: float, urgency: str) -> float:
    """Score 0-100 based on pricing (lower = better, unless emergency)."""
    if hourly_rate <= 0:
        return 50  # Unknown
    
    # Benchmark: 50€/h = good, 100€/h = expensive
    if hourly_rate <= 40:
        score = 100
    elif hourly_rate <= 60:
        score = 80
    elif hourly_rate <= 80:
        score = 60
    elif hourly_rate <= 100:
        score = 40
    else:
        score = 20
    
    # For emergencies, pricing matters less
    if urgency == "emergency":
        score = max(score, 60)
    
    return score


# ============================================================
# AUTO-DISPATCH (round-robin with scoring)
# ============================================================

def auto_dispatch(ticket: dict) -> dict | None:
    """
    Automatically assign the best available provider.
    
    Returns:
        {
            "provider_id": "...",
            "provider_name": "...",
            "score": 85.2,
            "deadline_hours": 4,  # Hours to accept
        }
    or None if no provider available.
    """
    results = score_providers(ticket)
    available = [r for r in results if r.available]
    
    if not available:
        logger.warning("no_provider_available", extra={
            "json_fields": {"category": ticket.get("category")}
        })
        return None
    
    best = available[0]
    
    # Deadline based on urgency
    urgency = ticket.get("urgency", "normal")
    deadline_hours = {"emergency": 1, "priority": 4, "normal": 24}.get(urgency, 24)
    
    return {
        "provider_id": best.provider_id,
        "provider_name": best.provider_name,
        "score": best.total_score,
        "breakdown": best.breakdown,
        "deadline_hours": deadline_hours,
    }


# ============================================================
# HELPERS
# ============================================================

def _load_providers() -> list[Provider]:
    """Load active providers from Firestore."""
    providers = []
    
    try:
        docs = db.collection("providers").where("is_active", "==", True).stream()
        
        for doc in docs:
            data = doc.to_dict()
            providers.append(Provider(
                id=doc.id,
                name=data.get("name", ""),
                skills=data.get("skills", []),
                zones=data.get("zones", []),
                avg_csat=data.get("avg_csat", 0),
                sla_compliance=data.get("sla_compliance", 0),
                acceptance_rate=data.get("acceptance_rate", 0),
                active_tickets=data.get("active_tickets", 0),
                max_concurrent=data.get("max_concurrent", 5),
                hourly_rate=data.get("hourly_rate", 0),
                is_approved=data.get("is_approved", True),
            ))
    
    except Exception as e:
        logger.error(f"Provider load error: {e}")
    
    return providers
