from flask import Blueprint, flash, redirect, render_template, request, url_for

from routes.auth import admin_required, login_required
from utils.config import config

chat_bp = Blueprint('chat', __name__)


@chat_bp.route('/chat', methods=['GET', 'POST'])
@login_required
def customer_chat():
    """Customer-facing AI assistant. The LangGraph agent lands next — POSTs
    currently redirect with a friendly notice so the UI is testable today."""
    if request.method == 'POST':
        flash("The assistant's brain is still in training — the agent backend lands next.", "info")
        return redirect(url_for('chat.customer_chat'))
    return render_template('chat/customer.html')


@chat_bp.route('/admin/ai-assistant', methods=['GET', 'POST'])
@admin_required
def admin_chat():
    """Admin playground + model parameter settings (stubbed POST)."""
    if request.method == 'POST':
        flash("Settings are display-only until the agent backend is live.", "info")
        return redirect(url_for('chat.admin_chat'))
    return render_template(
        'chat/admin.html',
        llm_config=config.llm_config,
        embedding_config=config.embedding_config,
        rag_config=config.rag_config,
        system_context=config.system_context,
    )