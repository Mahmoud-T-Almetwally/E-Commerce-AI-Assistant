import pytest
from typing import Annotated
from langchain_core.tools import InjectedToolArg

from agent.tooling import (
    agent_tool, 
    success, 
    error, 
    resolve_retries, 
    call_fingerprint,
    classify_exception,
    get_event_writer,
    TOOL_REGISTRY
)
from utils.exceptions import OutOfStockError, RecordNotFoundError, RAGError


class TestAgentToolDecorator:
    
    @pytest.mark.usefixtures("clean_tool_registry")
    def test_tool_registration_and_schema(self):
        """Test that @agent_tool populates TOOL_REGISTRY correctly."""
        
        @agent_tool(sensitive=True)
        def dummy_tool(query: str, user_id: Annotated[int, InjectedToolArg] = 0):
            """A dummy tool."""
            return success({"query": query, "user_id": user_id})

        assert "dummy_tool" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["dummy_tool"]
        
        assert spec.name == "dummy_tool"
        assert spec.sensitive is True
        assert spec.needs_user_id is True  # Proves decorator identified the arg
        assert spec.confirmation is None
        
        # We ensure standard args are validated properly
        schema = spec.tool.args_schema.schema()
        assert "query" in schema["properties"]

    @pytest.mark.usefixtures("clean_tool_registry")
    def test_duplicate_registration_raises(self):
        """Test that registering two tools with the same name raises an error."""
        
        @agent_tool()
        def clash_tool():
            """_summary_
            """
            pass

        with pytest.raises(RuntimeError, match="Duplicate agent tool registration"):
            @agent_tool()
            def clash_tool():
                """_summary_
                """
                pass

    @pytest.mark.usefixtures("clean_tool_registry")
    def test_custom_confirmation_renderer(self):
        """Test that custom confirmation callbacks are saved in the spec."""
        def custom_confirm(args, user_id):
            return f"Are you sure you want to {args.get('action')}?"

        @agent_tool(sensitive=True, confirmation=custom_confirm)
        def another_tool():
            """_summary_
            """
            pass
        
        spec = TOOL_REGISTRY["another_tool"]
        assert spec.confirmation({"action": "jump"}, 1) == "Are you sure you want to jump?"


class TestToolingHelpers:

    def test_success_envelope(self):
        result = success(data={"key": "value"}, ui_event={"event": "pop"})
        assert result == {
            "status": "success",
            "data": {"key": "value"},
            "ui_event": {"event": "pop"}
        }

    def test_error_envelope(self):
        result = error("not_found", "Item missing", retryable=False, hint="Try again")
        assert result == {
            "status": "error",
            "error": {
                "code": "not_found",
                "message": "Item missing",
                "retryable": False,
                "hint": "Try again"
            }
        }

    def test_error_envelope_default_retryable(self):
        """Test that RETRYABLE_CODES correctly infers the retryable flag when omitted."""
        result_retryable = error("internal_error", "DB Down")
        assert result_retryable["error"]["retryable"] is True
        
        result_non_retryable = error("invalid_arguments", "Bad args")
        assert result_non_retryable["error"]["retryable"] is False
        
        result_unknown = error("bizarre_code", "What?")
        assert result_unknown["error"]["code"] == "internal_error"
        assert result_unknown["error"]["retryable"] is True

    def test_resolve_retries(self, mock_config):
        """Test retry resolution boundary conditions."""
        mock_config.agent.max_tool_retries = 2
        
        assert resolve_retries(None) == 2
        assert resolve_retries({}) == 2
        assert resolve_retries({"retries": 1}) == 1
        assert resolve_retries({"retries": 10}) == 5
        assert resolve_retries({"retries": -5}) == 0
        assert resolve_retries({"retries": "invalid"}) == 2

    def test_call_fingerprint(self):
        """Test that hashing ignores the 'retries' key and is deterministic."""
        args1 = {"product_id": 123, "qty": 1, "retries": 2}
        args2 = {"qty": 1, "product_id": 123, "retries": 5}
        
        fp1 = call_fingerprint("add_to_cart", args1)
        fp2 = call_fingerprint("add_to_cart", args2)
        assert fp1 == fp2

        fp3 = call_fingerprint("checkout", args1)
        assert fp1 != fp3


class TestClassifyException:

    def test_classify_out_of_stock(self):
        exc = OutOfStockError("Mug", requested=5, available=2)
        env = classify_exception(exc)
        assert env["error"]["code"] == "out_of_stock"
        assert env["error"]["retryable"] is False

    def test_classify_not_found(self):
        exc = RecordNotFoundError("Product", 999)
        env = classify_exception(exc)
        assert env["error"]["code"] == "not_found"
        assert env["error"]["retryable"] is False

    def test_classify_value_error(self):
        exc = ValueError("Invalid int")
        env = classify_exception(exc)
        assert env["error"]["code"] == "invalid_arguments"
        assert env["error"]["retryable"] is False

    def test_classify_rag_error(self):
        exc = RAGError("Chroma down")
        env = classify_exception(exc)
        assert env["error"]["code"] == "rag_unavailable"
        assert env["error"]["retryable"] is True

    def test_classify_generic_exception(self):
        exc = Exception("System melted")
        env = classify_exception(exc)
        assert env["error"]["code"] == "internal_error"
        assert env["error"]["retryable"] is True


class TestEventWriter:
    
    def test_event_writer_success(self, monkeypatch):
        """Verify get_event_writer calls the langgraph stream writer if available."""
        mock_writer = set()
        
        def dummy_get_stream_writer():
            return lambda payload: mock_writer.add(payload.get("type"))
            
        monkeypatch.setattr("langgraph.config.get_stream_writer", dummy_get_stream_writer)
        
        writer = get_event_writer()
        writer({"type": "test_event", "data": "yes"})
        
        assert "test_event" in mock_writer

    def test_event_writer_degrades_headless(self, monkeypatch):
        """Verify the writer degrades gracefully to a no-op outside a langgraph stream."""
        def dummy_get_stream_writer():
            raise RuntimeError("Not in a stream")
            
        monkeypatch.setattr("langgraph.config.get_stream_writer", dummy_get_stream_writer)
        
        writer = get_event_writer()
        writer({"type": "test_event", "data": "yes"})