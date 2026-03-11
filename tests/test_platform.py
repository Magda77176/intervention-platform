"""Tests for the intervention platform — same structure as agent-pipeline tests."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ============================================================
# SCHEMA TESTS
# ============================================================

class TestSchemas:
    def test_ticket_creation(self):
        from shared.schemas import Ticket
        t = Ticket(
            description="Chaudière en panne",
            location="logement",
            equipment_type="logement",
            address="12 rue de la Paix, 75002 Paris",
            tenant_id="tenant_1",
        )
        assert t.id is not None
        assert t.status == "received"
        assert t.urgency == "normal"
    
    def test_ticket_serialization(self):
        from shared.schemas import Ticket
        t = Ticket(
            description="Test",
            location="logement",
            equipment_type="logement",
            address="1 rue X",
            tenant_id="t1",
        )
        data = t.model_dump(mode="json")
        assert isinstance(data, dict)
        assert "id" in data
    
    def test_agent_message(self):
        from shared.schemas import AgentMessage, Ticket
        ticket = Ticket(
            description="Test",
            location="logement",
            equipment_type="logement",
            address="1 rue X",
            tenant_id="t1",
        )
        msg = AgentMessage(
            from_agent="orchestrator",
            to_agent="prequalification",
            action="prequalify",
            ticket=ticket,
        )
        assert msg.message_id is not None
        assert msg.retry_count == 0
    
    def test_provider_model(self):
        from shared.schemas import Provider
        p = Provider(
            id="p1", name="Jean Plombier",
            skills=["plombier"], zones=["75002"],
            avg_csat=4.5, sla_compliance=0.92,
        )
        assert p.max_concurrent == 5
        assert p.is_approved == True


# ============================================================
# PRE-QUALIFICATION FALLBACK TESTS
# ============================================================

class TestPrequalFallback:
    def _fallback(self, desc, severity=""):
        from agents.prequalification.main import _fallback
        from shared.schemas import Ticket
        t = Ticket(
            description=desc,
            location="logement",
            equipment_type="logement",
            severity_details=severity,
            address="1 rue X",
            tenant_id="t1",
        )
        return _fallback(t)
    
    def test_gas_emergency(self):
        r = self._fallback("Odeur de gaz dans la cuisine")
        assert r["urgency"] == "emergency"
    
    def test_electrical_emergency(self):
        r = self._fallback("Étincelles au tableau", "Court-circuit")
        assert r["urgency"] == "emergency"
    
    def test_flood_emergency(self):
        r = self._fallback("Inondation dans la salle de bain")
        assert r["urgency"] == "emergency"
    
    def test_boiler_landlord(self):
        r = self._fallback("La chaudière ne marche plus")
        assert r["payer"] == "landlord"
    
    def test_radiator_landlord(self):
        r = self._fallback("Le radiateur ne chauffe pas")
        assert r["payer"] == "landlord"
    
    def test_common_areas_landlord(self):
        r = self._fallback("Problème parties communes escalier")
        assert r["payer"] == "landlord"
    
    def test_washing_machine_tenant(self):
        r = self._fallback("Mon lave-linge fait du bruit")
        assert r["payer"] == "tenant"
    
    def test_lightbulb_tenant(self):
        r = self._fallback("L'ampoule du salon est grillée")
        assert r["payer"] == "tenant"
    
    def test_ambiguous_to_confirm(self):
        r = self._fallback("Il y a un problème")
        assert r["payer"] == "to_confirm"
    
    def test_normal_urgency_default(self):
        r = self._fallback("Le robinet goutte un peu")
        assert r["urgency"] == "normal"


# ============================================================
# MCP TOOLS TESTS
# ============================================================

class TestMCPTools:
    def test_address_normalization(self):
        """Test that address validation extracts postal code."""
        import re
        address = "12 Rue de la Paix, 75002 Paris"
        match = re.search(r'\b(\d{5})\b', address)
        assert match is not None
        assert match.group(1) == "75002"
    
    def test_category_keywords(self):
        """Test category keyword matching for deduplication."""
        from mcp_server.server import CATEGORY_KEYWORDS
        assert "fuite" in CATEGORY_KEYWORDS["plumbing"]
        assert "chaudière" in CATEGORY_KEYWORDS["heating"]
        assert "disjoncteur" in CATEGORY_KEYWORDS["electrical"]


# ============================================================
# ASSIGNMENT SCORING TESTS
# ============================================================

class TestAssignmentScoring:
    def test_weights_sum_to_one(self):
        from agents.assignment.main import WEIGHTS
        total = sum(WEIGHTS.values())
        assert abs(total - 1.0) < 0.001
    
    def test_category_skills_mapping(self):
        from agents.assignment.main import CATEGORY_TO_SKILLS
        assert "plombier" in CATEGORY_TO_SKILLS["plumbing"]
        assert "chauffagiste" in CATEGORY_TO_SKILLS["heating"]
        assert "électricien" in CATEGORY_TO_SKILLS["electrical"]
