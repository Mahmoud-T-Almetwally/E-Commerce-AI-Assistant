import os

from types import SimpleNamespace

from flask import Blueprint, render_template, request, redirect, url_for, flash

from database.db_setup import SessionLocal
from database.models import KnowledgeDocument
from database.rag_manager import rag_manager

from routes.auth import admin_required

from utils.exceptions import TextExtractionError
from utils.file_extraction import extract_text
from utils.pagination import get_pagination
from utils.config import config

import logging
logger = logging.getLogger(__name__)

rag_bp = Blueprint('rag', __name__)


def _read_knowledge_form():
    """Reads and normalizes the knowledge form fields."""
    title = (request.form.get('title') or '').strip()
    content = (request.form.get('content') or '').strip()
    doc_type = (request.form.get('doc_type') or '').strip() or 'General'
    return title, content, doc_type


def _validate_knowledge_form(title: str, content: str):
    """Shared validation for add and edit (edit previously validated nothing)."""
    if not title:
        return "Title is required."
    if not content:
        return "Content is required."
    return None


def _file_stem(filename: str) -> str:
    """Basename without extension; safe against Windows-style and full paths."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return stem.strip()


def _rerender_add_form(title, content, doc_type):
    """Re-renders the add form, preserving everything the user submitted."""
    return render_template(
        'admin/knowledge_form.html',
        doc=None,
        form_data=SimpleNamespace(title=title, content=content, doc_type=doc_type)
    )


@rag_bp.route('/knowledge')
@admin_required
def list_knowledge():
    """List all RAG documents in the admin dashboard."""
    page = max(request.args.get('page', 1, type=int), 1)
    with SessionLocal() as db:
        query = (
            db.query(KnowledgeDocument)
            .order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id.desc())
        )
        documents, total, total_pages = get_pagination(query, page, per_page=20)

        return render_template(
            'admin/knowledge.html',
            documents=documents,
            page=page,
            total=total,
            total_pages=total_pages
        )


@rag_bp.route('/knowledge/add', methods=['GET', 'POST'])
@admin_required
def add_knowledge():
    """Add a new document — typed text or an uploaded .txt/.pdf/.docx — to both SQLite and ChromaDB."""
    if request.method == 'POST':
        title, content, doc_type = _read_knowledge_form()
        uploaded = request.files.get('file')
        
        if uploaded and uploaded.filename:
            filename = uploaded.filename
            ext = os.path.splitext(filename)[1].lower()

            if ext not in config.rag_config.supported_extensions:
                flash(f"Unsupported file type '{ext}'. Allowed: .txt, .pdf, .docx", "danger")
                return _rerender_add_form(title, content, doc_type)

            data = uploaded.read()
            if not data:
                flash("The uploaded file is empty.", "danger")
                return _rerender_add_form(title, content, doc_type)
            if len(data) > (config.rag_config.max_file_size_mb * 1024 * 1024):
                flash("File is too large. Maximum size is 10 MB.", "danger")
                return _rerender_add_form(title, content, doc_type)

            try:
                content = extract_text(data, ext)
            except TextExtractionError as e:
                flash(f"Could not read the uploaded file: {e}", "danger")
                return _rerender_add_form(title, content, doc_type)

            if len(content) > config.rag_config.max_content_chars:
                flash(
                    f"Extracted text is too long (over {config.rag_config.max_content_chars:,} characters). "
                    "Please split the document into smaller parts.",
                    "danger"
                )
                return _rerender_add_form(title, "", doc_type)

            if not title:
                title = _file_stem(filename) or "Untitled Document"

        error = _validate_knowledge_form(title, content)
        if error:
            flash(error, "danger")
            return _rerender_add_form(title, content, doc_type)

        with SessionLocal() as db:
            doc_id = None
            try:
                new_doc = KnowledgeDocument(title=title, content=content, doc_type=doc_type)
                db.add(new_doc)
                db.flush()
                doc_id = new_doc.id

                rag_manager.add_document(
                    doc_id=doc_id, title=title, content=content, doc_type=doc_type
                )

                db.commit()
                flash("Knowledge document added and embedded successfully!", "success")
                return redirect(url_for('rag.list_knowledge'))
            except Exception as e:
                db.rollback()
                if doc_id is not None:
                    try:
                        rag_manager.delete_document(doc_id)
                    except Exception as chroma_error:
                        logger.error(
                            f"URGENT: Failed to rollback ChromaDB vectors for Doc ID {doc_id}. "
                            f"Vectors are now orphaned. Chroma Error: {chroma_error}"
                        )

                flash(f"Error adding document: {e}", "danger")
                return _rerender_add_form(title, content, doc_type)

    return render_template('admin/knowledge_form.html', doc=None, form_data=None)


@rag_bp.route('/knowledge/<int:doc_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_knowledge(doc_id):
    """Edit an existing document in both databases."""
    with SessionLocal() as db:
        doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id).first()
        if not doc:
            flash("Document not found.", "danger")
            return redirect(url_for('rag.list_knowledge'))

        if request.method == 'POST':
            title, content, doc_type = _read_knowledge_form()

            error = _validate_knowledge_form(title, content)
            if error:
                flash(error, "danger")
                return render_template(
                    'admin/knowledge_form.html',
                    doc=doc,
                    form_data=SimpleNamespace(title=title, content=content, doc_type=doc_type)
                )

            
            old_title, old_content, old_doc_type = doc.title, doc.content, doc.doc_type

            try:
                doc.title = title
                doc.content = content
                doc.doc_type = doc_type
                db.flush()

                rag_manager.update_document(
                    doc_id=doc.id,
                    title=title,
                    content=content,
                    doc_type=doc_type
                )

                db.commit()
                flash("Knowledge document updated successfully!", "success")
                return redirect(url_for('rag.list_knowledge'))
            except Exception as e:
                db.rollback()
                try:
                    rag_manager.update_document(
                        doc_id=doc.id,
                        title=old_title,
                        content=old_content,
                        doc_type=old_doc_type
                    )
                except Exception as chroma_error:
                    logger.error(
                            f"URGENT: Failed to rollback ChromaDB vectors for Doc ID {doc_id}. "
                            f"Vectors are now orphaned. Chroma Error: {chroma_error}"
                        )
                flash(f"Error updating document: {e}", "danger")

        return render_template('admin/knowledge_form.html', doc=doc, form_data=None)


@rag_bp.route('/knowledge/<int:doc_id>/delete', methods=['POST'])
@admin_required
def delete_knowledge(doc_id):
    """Delete a document from both SQLite and ChromaDB."""
    with SessionLocal() as db:
        doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id).first()
        if not doc:
            flash("Document not found.", "danger")
            return redirect(url_for('rag.list_knowledge'))

        old_title, old_content, old_doc_type = doc.title, doc.content, doc.doc_type
        vectors_deleted = False
        try:
            rag_manager.delete_document(doc_id=doc.id)
            vectors_deleted = True

            db.delete(doc)
            db.commit()
            flash("Knowledge document deleted successfully.", "success")
        except Exception as e:
            db.rollback()
            if vectors_deleted:
                try:
                    rag_manager.add_document(
                        doc_id=doc.id,
                        title=old_title,
                        content=old_content,
                        doc_type=old_doc_type
                    )
                except Exception as chroma_error:
                    logger.error(
                            f"URGENT: Failed to rollback ChromaDB vectors for Doc ID {doc_id}. "
                            f"Vectors are now orphaned. Chroma Error: {chroma_error}"
                        )
            flash(f"Error deleting document: {e}", "danger")

    return redirect(url_for('rag.list_knowledge'))