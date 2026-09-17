import pytest
from datetime import date

from agent.prompts import build_system_prompt, INTENT_GUIDANCE, FALLBACK_GUIDANCE


class TestPrompts:
    
    def test_base_system_prompt_structure(self, mock_config):
        """Tests that the foundation of the system prompt contains required instructions."""
        state = {}
        prompt = build_system_prompt(state)
        
        assert "You are TestStore, helpful and professional." in prompt
        
        assert f"Today's date: {date.today().isoformat()}." in prompt
        
        assert "How you work:" in prompt
        assert "Every tool returns JSON" in prompt

    def test_prompt_with_user_name(self, mock_config):
        """Tests that the user's name is dynamically added if present in state."""
        state = {"user_name": "Alice"}
        prompt = build_system_prompt(state)
        
        assert "The customer's name is Alice." in prompt

    def test_prompt_intent_injection(self, mock_config):
        """Tests that the specific intent guidance is injected."""
        state = {"intent": "cart_management"}
        prompt = build_system_prompt(state)
        
        assert "Current task focus:" in prompt
        assert INTENT_GUIDANCE["cart_management"] in prompt

    def test_prompt_fallback_intent(self, mock_config):
        """Tests that an unknown or None intent triggers the fallback guidance."""
        # Test with None
        state_none = {"intent": None}
        prompt_none = build_system_prompt(state_none)
        assert FALLBACK_GUIDANCE in prompt_none

        # Test with invalid intent string
        state_invalid = {"intent": "hacking_the_mainframe"}
        prompt_invalid = build_system_prompt(state_invalid)
        assert FALLBACK_GUIDANCE in prompt_invalid

    def test_prompt_rag_context_injection(self, mock_config):
        """Tests that retrieved documents are added ONLY for the customer_service intent."""
        rag_text = "Return policy: 30 days."
        
        # customer_service intent SHOULD inject it
        state_cs = {"intent": "customer_service", "rag_context": rag_text}
        prompt_cs = build_system_prompt(state_cs)
        assert "KNOWLEDGE BASE CONTEXT" in prompt_cs
        assert rag_text in prompt_cs
        
        # checkout intent SHOULD NOT inject it (context isolation)
        state_checkout = {"intent": "checkout", "rag_context": rag_text}
        prompt_checkout = build_system_prompt(state_checkout)
        assert "KNOWLEDGE BASE CONTEXT" not in prompt_checkout
        assert rag_text not in prompt_checkout

    def test_prompt_rag_unavailable_handling(self, mock_config):
        """Tests that RAG downtime gracefully tells the model not to hallucinate."""
        state = {
            "intent": "customer_service", 
            "rag_context": None, 
            "rag_unavailable": True
        }
        prompt = build_system_prompt(state)
        
        assert "KNOWLEDGE BASE CONTEXT" not in prompt
        assert "The knowledge base is temporarily unavailable." in prompt