import io
import pytest
from unittest.mock import MagicMock

from database.models import KnowledgeDocument


@pytest.fixture(autouse=True)
def mock_dependencies(monkeypatch):
    """Mocks RAG Manager, File Extractor, and Config for all tests in this file."""
    mock_rag = MagicMock()
    monkeypatch.setattr("routes.rag.get_rag_manager", lambda: mock_rag)
    
    monkeypatch.setattr("routes.rag.extract_text", lambda data, ext: data.decode("utf-8"))
    
    class MockRAGConfig:
        supported_extensions = [".txt", ".pdf", ".docx"]
        max_file_size_mb = 10
        max_content_chars = 100000

    class MockConfig:
        rag_config = MockRAGConfig()

    monkeypatch.setattr("routes.rag.config", MockConfig)
    
    return mock_rag


class TestRAGAuth:
    @pytest.mark.parametrize("path", [
        "/admin/knowledge",
        "/admin/knowledge/add",
        "/admin/knowledge/1/edit",
    ])
    def test_rag_routes_protected(self, client, path):
        """Ensure RAG routes block unauthenticated users."""
        response = client.get(path)
        assert response.status_code in [302, 401, 403]

    def test_rag_customer_blocked(self, customer_client):
        """Ensure RAG routes block non-admin customers."""
        response = customer_client.get("/admin/knowledge")
        assert response.status_code in [302, 401, 403]


class TestRAGViews:
    def test_list_knowledge(self, admin_client, sample_knowledge_doc):
        """Ensure the knowledge list renders and displays existing docs."""
        response = admin_client.get("/admin/knowledge")
        assert response.status_code == 200
        assert b"Store Return Policy" in response.data

    def test_add_knowledge_get(self, admin_client):
        """Ensure the add form renders correctly."""
        response = admin_client.get("/admin/knowledge/add")
        assert response.status_code == 200
        assert b"form" in response.data.lower()


class TestRAGMutations:
    def test_add_knowledge_manual_text(self, admin_client, db_session, mock_dependencies):
        """Adding a document via typed text should insert to DB and Chroma."""
        response = admin_client.post("/admin/knowledge/add", data={
            "title": "Shipping Times",
            "content": "Standard shipping takes 3-5 business days.",
            "doc_type": "FAQ"
        }, follow_redirects=True)
        
        assert response.status_code == 200
        assert b"successfully" in response.data
        
        doc = db_session.query(KnowledgeDocument).filter_by(title="Shipping Times").first()
        assert doc is not None
        assert doc.content == "Standard shipping takes 3-5 business days."
        
        mock_dependencies.add_document.assert_called_once_with(
            doc_id=doc.id, 
            title="Shipping Times", 
            content="Standard shipping takes 3-5 business days.", 
            doc_type="FAQ"
        )

    def test_add_knowledge_file_upload(self, admin_client, db_session, mock_dependencies):
        """Uploading a supported file should extract text and insert it."""
        data = {
            "title": "Uploaded Policy",
            "doc_type": "Policy",
            "file": (io.BytesIO(b"Extracted file content."), "test.txt")
        }
        
        response = admin_client.post("/admin/knowledge/add", data=data, content_type="multipart/form-data", follow_redirects=True)
        assert response.status_code == 200
        
        doc = db_session.query(KnowledgeDocument).filter_by(title="Uploaded Policy").first()
        assert doc is not None
        assert doc.content == "Extracted file content."
        
        mock_dependencies.add_document.assert_called_once()

    def test_add_knowledge_unsupported_file(self, admin_client, db_session, mock_dependencies):
        """Uploading an unsupported extension should flash an error."""
        data = {
            "title": "Bad File",
            "file": (io.BytesIO(b"hack"), "virus.exe")
        }
        response = admin_client.post("/admin/knowledge/add", data=data, content_type="multipart/form-data")
        
        assert b"Unsupported file type" in response.data
        assert db_session.query(KnowledgeDocument).count() == 0
        mock_dependencies.add_document.assert_not_called()

    def test_edit_knowledge(self, admin_client, db_session, sample_knowledge_doc, mock_dependencies):
        """Editing should update SQLite and sync to ChromaDB."""
        response = admin_client.post(f"/admin/knowledge/{sample_knowledge_doc.id}/edit", data={
            "title": "Updated Policy",
            "content": "Updated content here.",
            "doc_type": "Legal"
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        db_session.refresh(sample_knowledge_doc)
        assert sample_knowledge_doc.title == "Updated Policy"
        assert sample_knowledge_doc.content == "Updated content here."
        
        mock_dependencies.update_document.assert_called_once_with(
            doc_id=sample_knowledge_doc.id,
            title="Updated Policy",
            content="Updated content here.",
            doc_type="Legal"
        )

    def test_delete_knowledge(self, admin_client, db_session, sample_knowledge_doc, mock_dependencies):
        """Deleting should remove from SQLite and sync to ChromaDB."""
        doc_id = sample_knowledge_doc.id
        
        response = admin_client.post(f"/admin/knowledge/{doc_id}/delete", follow_redirects=True)
        assert response.status_code == 200
        
        assert db_session.query(KnowledgeDocument).filter_by(id=doc_id).first() is None
        
        mock_dependencies.delete_document.assert_called_once_with(doc_id=doc_id)