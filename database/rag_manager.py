import os
from typing import List

from langchain_core.documents import Document
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter

from agent.providers import ModelFactory
from utils.config import config


class RAGManager:
    """
    Manages text chunking, embeddings, and vector database operations.
    Keeps ChromaDB in sync with the relational database.
    """

    def __init__(self):
        self.embeddings = ModelFactory.get_embeddings(config.embedding_config)

        persist_dir = config.database_config.chroma_persist_directory
        os.makedirs(persist_dir, exist_ok=True)

        self.vector_store = Chroma(
            collection_name=config.rag_config.collection_name,
            embedding_function=self.embeddings,
            persist_directory=persist_dir
        )

        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.rag_config.chunk_size,
            chunk_overlap=config.rag_config.chunk_overlap,
            separators=config.rag_config.separators
        )

    def _build_chunks(self, doc_id: int, title: str, content: str, doc_type: str):
        """
        Splits the content into chunk Documents with deterministic IDs.
        Pure local operation — raises BEFORE any vector-store mutation, so a
        failure here can never leave the store half-written.
        """
        chunks = self.text_splitter.split_text(content)
        if not chunks:
            raise ValueError(
                f"Document {doc_id} produced no chunks (content is empty or whitespace-only)."
            )

        documents, ids = [], []
        for i, chunk in enumerate(chunks):
            metadata = {
                "doc_id": doc_id,
                "title": title,
                "doc_type": doc_type,
                "chunk_index": i
            }
            documents.append(Document(page_content=f"{title}\n\n{chunk}", metadata=metadata))
            ids.append(f"doc_{doc_id}_chunk_{i}")
        return documents, ids

    def add_document(self, doc_id: int, title: str, content: str, doc_type: str) -> None:
        """
        Chunks the content and adds it to ChromaDB with metadata linking it to
        the SQL ID.

        Idempotent: existing chunks for this doc_id are removed first, so a
        retry after a partial failure can never create duplicate chunks.
        """
        documents, ids = self._build_chunks(doc_id, title, content, doc_type)

        self.delete_document(doc_id)
        self.vector_store.add_documents(documents, ids=ids)

    def delete_document(self, doc_id: int) -> None:
        """Deletes all vector chunks associated with a specific SQL Document ID."""
        self.vector_store.delete(where={"doc_id": doc_id})

    def update_document(self, doc_id: int, title: str, content: str, doc_type: str) -> None:
        """Replaces a document's chunks (add_document is already delete-then-add)."""
        self.add_document(doc_id, title, content, doc_type)

    def search(self, query: str, k: int = 4) -> List[Document]:
        """Retrieves the top k most relevant chunks for a given user query."""
        return self.vector_store.similarity_search(query, k=k)

    def resync(self, documents) -> int:
        """
        Rebuilds the vector store from SQL rows: re-embeds every document and
        deletes vectors whose doc_id no longer exists in SQL.
        Returns the number of documents re-embedded.
        """
        valid_ids = set()
        for doc in documents:
            self.add_document(doc_id=doc.id, title=doc.title, content=doc.content, doc_type=doc.doc_type)
            valid_ids.add(doc.id)

        stored_ids = set()
        offset = 0
        limit = 1000
        
        while True:
            result = self.vector_store.get(limit=limit, offset=offset, include=["metadatas"])
            metadatas = result.get("metadatas") or []
            
            if not metadatas:
                break
                
            for md in metadatas:
                doc_id = md.get("doc_id")
                if doc_id is not None:
                    stored_ids.add(doc_id)
                    
            if len(metadatas) < limit:
                break

            offset += limit

        for stale_id in stored_ids - valid_ids:
            self.delete_document(stale_id)

        return len(valid_ids)


rag_manager = RAGManager()