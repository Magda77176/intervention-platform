"""Tests for the AI pre-qualification engine."""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestFallbackClassification:
    """Test rule-based fallback (no LLM needed)."""
    
    def test_emergency_gas_leak(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "Odeur de gaz dans la cuisine",
            "severity_details": "Forte odeur depuis ce matin",
        })
        assert result["urgency"] == "emergency"
    
    def test_emergency_electrical(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "Étincelles au niveau du tableau électrique",
            "severity_details": "Court-circuit",
        })
        assert result["urgency"] == "emergency"
    
    def test_emergency_flooding(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "Grosse fuite sous l'évier, inondation cuisine",
            "severity_details": "Eau partout",
        })
        assert result["urgency"] == "emergency"
    
    def test_landlord_boiler(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "La chaudière ne démarre plus",
            "severity_details": "Plus de chauffage",
        })
        assert result["payer"] == "landlord"
    
    def test_landlord_shutters(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "Le volet roulant est bloqué en position ouverte",
            "severity_details": "",
        })
        assert result["payer"] == "landlord"
    
    def test_landlord_common_areas(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "L'interphone de l'immeuble ne fonctionne plus",
            "severity_details": "",
        })
        assert result["payer"] == "landlord"
    
    def test_tenant_washing_machine(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "Mon lave-linge fait un bruit bizarre",
            "severity_details": "",
        })
        assert result["payer"] == "tenant"
    
    def test_tenant_lightbulb(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "L'ampoule du salon ne marche plus",
            "severity_details": "",
        })
        assert result["payer"] == "tenant"
    
    def test_ambiguous_to_confirm(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "Il y a un problème dans la salle de bain",
            "severity_details": "",
        })
        assert result["payer"] == "to_confirm"
    
    def test_normal_urgency_default(self):
        from ai.prequalification import _fallback_classification
        result = _fallback_classification({
            "description": "Le robinet de la cuisine goutte un peu",
            "severity_details": "Quelques gouttes par heure",
        })
        assert result["urgency"] == "normal"


class TestDeduplication:
    """Test ticket deduplication logic."""
    
    def test_normalize_address(self):
        from ai.prequalification import _normalize_address
        assert _normalize_address("12 Rue de la Paix, 75002") == _normalize_address("12 rue de la paix, 75002")
    
    def test_categories_match_plumbing(self):
        from ai.prequalification import _categories_match
        assert _categories_match(
            "Fuite d'eau sous l'évier",
            "Le robinet de la cuisine fuit"
        ) == True
    
    def test_categories_match_electrical(self):
        from ai.prequalification import _categories_match
        assert _categories_match(
            "Plus d'électricité dans la chambre",
            "Le disjoncteur saute tout le temps"
        ) == True
    
    def test_categories_dont_match(self):
        from ai.prequalification import _categories_match
        assert _categories_match(
            "La chaudière ne marche plus",
            "Cafards dans la cuisine"
        ) == False


class TestProviderScoring:
    """Test provider scoring engine."""
    
    def test_skills_score_exact_match(self):
        from ai.provider_scoring import _score_skills
        score = _score_skills(["plombier"], ["plombier"], "plumbing")
        assert score == 100
    
    def test_skills_score_category_match(self):
        from ai.provider_scoring import _score_skills
        score = _score_skills(["chauffagiste"], [], "heating")
        assert score == 80
    
    def test_skills_score_multiservice(self):
        from ai.provider_scoring import _score_skills
        score = _score_skills(["multiservice"], [], "plumbing")
        assert score == 40
    
    def test_skills_score_no_match(self):
        from ai.provider_scoring import _score_skills
        score = _score_skills(["serrurier"], ["plombier"], "plumbing")
        assert score == 0
    
    def test_zone_exact_match(self):
        from ai.provider_scoring import _score_zone
        assert _score_zone(["75002", "75003"], "75002") == 100
    
    def test_zone_same_department(self):
        from ai.provider_scoring import _score_zone
        assert _score_zone(["75010"], "75002") == 60
    
    def test_zone_no_match(self):
        from ai.provider_scoring import _score_zone
        assert _score_zone(["69001"], "75002") == 30  # Same region digit
    
    def test_availability_empty(self):
        from ai.provider_scoring import _score_availability
        assert _score_availability(0, 5, "normal") == 100
    
    def test_availability_full(self):
        from ai.provider_scoring import _score_availability
        assert _score_availability(5, 5, "normal") == 0
    
    def test_performance_perfect(self):
        from ai.provider_scoring import _score_performance
        score = _score_performance(5.0, 1.0)
        assert score == 100
    
    def test_performance_zero(self):
        from ai.provider_scoring import _score_performance
        score = _score_performance(0, 0)
        assert score == 50  # Neutral defaults
