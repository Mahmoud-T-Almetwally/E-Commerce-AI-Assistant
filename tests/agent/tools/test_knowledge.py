import pytest
from unittest.mock import patch, MagicMock
from agent.tools.knowledge import search_knowledge_base

class TestKnowledgeTool:
    
    @patch("database.rag_manager.get_rag_manager")
    def test_search_knowledge_base_success(self, mock_get_rag):
        mock_rag = MagicMock()
        mock_doc = MagicMock()
        mock_doc.metadata = {"title": "Shipping Policy", "doc_type": "Policy"}
        mock_doc.page_content = "We ship within 2 days."
        
        mock_rag.search.return_value = [mock_doc]
        mock_get_rag.return_value = mock_rag
        
        result = search_knowledge_base.func(query="shipping")
        
        assert result["status"] == "success"
        assert result["data"]["count"] == 1
        assert result["data"]["results"][0]["title"] == "Shipping Policy"

    @patch("database.rag_manager.get_rag_manager")
    def test_search_knowledge_base_failure_graceful(self, mock_get_rag):
        mock_rag = MagicMock()
        mock_rag.search.side_effect = Exception("Chroma DB offline")
        mock_get_rag.return_value = mock_rag
        
        result = search_knowledge_base.func(query="shipping")
        
        assert result["status"] == "error"
        assert result["error"]["code"] == "rag_unavailable"
        assert result["error"]["retryable"] is True