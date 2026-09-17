import pytest
from langchain_core.messages import AIMessage

from agent.stats import TurnStatsCollector, StatsRegistry
from database.models import Conversation, AgentTurnStats, AgentToolCall


class TestTurnStatsCollector:

    @pytest.fixture
    def test_conversation(self, db_session, seed_data):
        """Seed a Conversation so the turn stats have a valid foreign key."""
        conv = Conversation(thread_id="thread_test_123", user_id=seed_data["user_id"])
        db_session.add(conv)
        db_session.commit()
        return conv

    def test_collect_tokens_and_llm_calls(self, test_conversation, seed_data):
        collector = TurnStatsCollector(
            user_id=seed_data["user_id"], 
            thread_id="thread_test_123", 
            conversation_id=test_conversation.id
        )
        
        mock_msg = AIMessage(
            content="Hello", 
            usage_metadata={"input_tokens": 50, "output_tokens": 15, "total_tokens": 65}
        )
        
        collector.note_update({"agent_node": {"messages": [mock_msg]}})
        
        assert collector.stats.llm_calls == 1
        assert collector.stats.tokens_in == 50
        assert collector.stats.tokens_out == 15

    def test_collect_tool_events(self, test_conversation, seed_data):
        collector = TurnStatsCollector(
            user_id=seed_data["user_id"], 
            thread_id="thread_test_123", 
            conversation_id=test_conversation.id
        )
        
        collector.note_custom({
            "type": "tool_status",
            "state": "done",
            "tool": "search_products",
            "duration_ms": 320,
            "attempt": 2
        })
        
        assert len(collector.stats.tool_calls) == 1
        assert collector.stats.tool_calls[0]["tool"] == "search_products"
        assert collector.stats.tool_calls[0]["duration_ms"] == 320

    def test_persistence_commits_rows(self, db_session, test_conversation, seed_data):
        collector = TurnStatsCollector(
            user_id=seed_data["user_id"], 
            thread_id="thread_test_123", 
            conversation_id=test_conversation.id
        )
        
        collector.stats.tokens_in = 100
        collector.stats.intent = "checkout"
        collector.note_custom({
            "type": "tool_status",
            "state": "done",
            "tool": "add_to_cart",
            "duration_ms": 100
        })
        
        collector.finalize(status="completed", values={"intent": "checkout", "guard_verdict": {"blocked": False}})
        
        turn = db_session.query(AgentTurnStats).filter_by(conversation_id=test_conversation.id).first()
        assert turn is not None
        assert turn.tokens_in == 100
        assert turn.intent == "checkout"
        
        tool_call = db_session.query(AgentToolCall).filter_by(turn_id=turn.id).first()
        assert tool_call is not None
        assert tool_call.tool_name == "add_to_cart"

    def test_persistence_skips_gracefully_if_no_conversation(self, db_session, seed_data):
        """Should NOT crash if the thread doesn't belong to a saved DB conversation."""
        collector = TurnStatsCollector(
            user_id=seed_data["user_id"], 
            thread_id="phantom_thread", 
            conversation_id=None
        )
        
        collector.finalize(status="completed")
        
        count = db_session.query(AgentTurnStats).count()
        assert count == 0


class TestStatsRegistry:
    
    def test_registry_aggregates_user_history(self):
        registry = StatsRegistry()
        
        collector = TurnStatsCollector(user_id=1, thread_id="t1", conversation_id=1)
        collector.stats.tokens_in = 50
        collector.stats.status = "completed"
        registry.add(collector.stats)
        
        collector2 = TurnStatsCollector(user_id=1, thread_id="t1", conversation_id=1)
        collector2.stats.tokens_in = 20
        collector2.stats.status = "interrupted"
        registry.add(collector2.stats)
        
        summary = registry.summary()
        assert summary["turns"] == 2
        assert summary["completed"] == 1
        assert summary["interrupted"] == 1
        assert summary["tokens_in"] == 70
        
        history = registry.user_turns(user_id=1)
        assert len(history) == 2
        assert history[0]["status"] == "completed"