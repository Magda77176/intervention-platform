"""
Assignment Agent — Score and assign providers.
Same role as Enrichment in agent-pipeline: call MCP tools, process, return result.
"""
import os
import time
import logging

from fastapi import FastAPI
import httpx

from shared.schemas import Ticket, Provider
from shared.tracing import init_tracing

logger = logging.getLogger("intervention")
tracer = init_tracing("assignment")

MCP_URL = os.getenv("MCP_TOOLS_URL", "http://localhost:8001")

app = FastAPI(title="Assignment Agent", version="1.0.0")


# Scoring weights (same as agent-pipeline scoring approach)
WEIGHTS = {
    "skills_match": 0.30,
    "zone_match": 0.20,
    "performance": 0.25,
    "availability": 0.15,
    "pricing": 0.10,
}

CATEGORY_TO_SKILLS = {
    "plumbing": ["plombier"], "electrical": ["électricien"],
    "heating": ["chauffagiste"], "locksmith": ["serrurier"],
    "water_damage": ["plombier"], "pest_control": ["dératisation"],
    "structural": ["maçon"], "common_areas": ["multiservice"],
}


@app.post("/assign")
async def assign_provider(ticket: Ticket):
    """
    Find and rank the best provider for this ticket.
    1. Call MCP lookup_providers tool
    2. Score each provider on 5 criteria
    3. Return the best match
    """
    with tracer.start_as_current_span("assignment.score_providers") as span:
        t0 = time.time()
        
        skills_needed = ticket.suggested_skills or CATEGORY_TO_SKILLS.get(
            ticket.category or "other", ["multiservice"]
        )
        
        # Step 1: Get providers from MCP
        with tracer.start_as_current_span("mcp.lookup_providers"):
            async with httpx.AsyncClient(timeout=5) as client:
                try:
                    resp = await client.post(f"{MCP_URL}/tools/lookup_providers", json={
                        "skills": skills_needed,
                        "postal_code": ticket.address_postal_code,
                    })
                    providers_data = resp.json().get("providers", [])
                except Exception as e:
                    logger.error(f"Provider lookup failed: {e}")
                    providers_data = []
        
        if not providers_data:
            return {"provider_id": None, "error": "No providers available"}
        
        # Step 2: Score each provider
        scored = []
        for p in providers_data:
            scores = {}
            
            # Skills match
            p_skills = [s.lower() for s in p.get("skills", [])]
            if any(s.lower() in p_skills for s in skills_needed):
                scores["skills_match"] = 100
            elif "multiservice" in p_skills:
                scores["skills_match"] = 40
            else:
                scores["skills_match"] = 0
            
            # Performance (CSAT)
            csat = p.get("avg_csat", 0)
            scores["performance"] = (csat / 5) * 100 if csat > 0 else 50
            
            # Availability (lower active = better)
            active = p.get("active_tickets", 0)
            scores["availability"] = max(0, 100 - (active * 20))
            
            # Zone (already filtered by MCP, so base 80)
            scores["zone_match"] = 80
            
            # Pricing (placeholder)
            scores["pricing"] = 60
            
            # Weighted total
            total = sum(scores[k] * WEIGHTS[k] for k in WEIGHTS)
            
            scored.append({
                "provider_id": p["id"],
                "provider_name": p["name"],
                "score": round(total, 1),
                "breakdown": scores,
            })
        
        # Step 3: Pick the best
        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0]
        
        latency = int((time.time() - t0) * 1000)
        span.set_attribute("provider", best["provider_name"])
        span.set_attribute("score", best["score"])
        
        logger.info("provider_assigned", extra={
            "json_fields": {
                "ticket_id": ticket.id,
                "provider": best["provider_name"],
                "score": best["score"],
                "candidates": len(scored),
                "latency_ms": latency,
            }
        })
        
        return best


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "assignment"}
