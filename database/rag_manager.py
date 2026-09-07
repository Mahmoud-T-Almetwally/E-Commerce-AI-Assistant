import hashlib
import os
from functools import lru_cache
from typing import Dict, List, Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from agent.providers import ModelFactory
from utils.config import config


def _content_hash(title: str, content: str, doc_type: str) -> str:
    """
    Stable fingerprint of a document's embeddable content and metadata,
    stored on every chunk so drift detection costs zero embedding calls.
    If chunking settings ever change, fold a version string into this hash
    so existing chunks get re-split.
    """
    raw = f"{title}\x00{content}\x00{doc_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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

        content_hash = _content_hash(title, content, doc_type)

        documents, ids = [], []
        for i, chunk in enumerate(chunks):
            metadata = {
                "doc_id": doc_id,
                "title": title,
                "doc_type": doc_type,
                "chunk_index": i,
                "content_hash": content_hash
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

    def reconcile(self, documents) -> Dict[str, int]:
        """
        Cheap drift repair: compares stored content hashes (a metadata-only
        Chroma read — no embeddings fetched, no API calls) against SQL and
        only re-embeds documents that are new or changed, deleting vectors
        whose SQL rows no longer exist. Heals the orphaned-vector cases the
        CRUD rollback paths log as URGENT.

        Returns a report: {"added": n, "updated": n, "removed": n}.
        """
        desired = {
            doc.id: _content_hash(doc.title, doc.content, doc.doc_type)
            for doc in documents
        }

        stored: Dict[int, Optional[str]] = {}
        offset, limit = 0, 1000
        while True:
            result = self.vector_store.get(limit=limit, offset=offset, include=["metadatas"])
            metadatas = result.get("metadatas") or []
            for md in metadatas:
                if md.get("doc_id") is not None:
                    stored[md["doc_id"]] = md.get("content_hash")
            if len(metadatas) < limit:
                break
            offset += limit

        report = {"added": 0, "updated": 0, "removed": 0}

        for stale_id in set(stored) - set(desired):
            self.delete_document(stale_id)
            report["removed"] += 1

        for doc in documents:
            # Chunks predating content_hash metadata return None -> re-embedded once.
            if stored.get(doc.id) != desired[doc.id]:
                self.add_document(doc.id, doc.title, doc.content, doc.doc_type)
                report["added" if doc.id not in stored else "updated"] += 1

        return report

    def resync(self, documents) -> int:
        """
        Full rebuild from SQL rows: re-embeds every document and deletes
        vectors whose doc_id no longer exists. Returns the number of
        documents re-embedded. Used by seeding and explicit rebuilds —
        prefer reconcile() for routine drift healing.
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


@lru_cache(maxsize=1)
def get_rag_manager() -> RAGManager:
    """
    Lazy singleton — Chroma and the embedding client are only initialised
    when a route actually touches RAG, never at import time. Failure to
    resolve an API key surfaces here on first use, not at app boot.
    """
    return RAGManager()