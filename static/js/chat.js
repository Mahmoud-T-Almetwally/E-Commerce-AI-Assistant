/* Minimal chat client for the Socket.IO + SSE agent API.
 * Assumes the page is served by the Flask app (shares the session cookie). */
(function () {
  const socket = io("/chat");
  let threadId = localStorage.getItem("chat_thread_id") || null;
  let eventSource = null;

  const statusEl = document.getElementById("chat-status");
  const logEl = document.getElementById("chat-log");
  const confirmEl = document.getElementById("chat-confirmation");

  function setStatus(text) { if (statusEl) statusEl.textContent = text; }

  function appendBubble(role, text) {
    if (!logEl) return;
    const div = document.createElement("div");
    div.className = `bubble ${role}`;
    div.textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function connectEvents() {
    if (!threadId) return;
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/chat/conversations/${threadId}/events`);
    eventSource.addEventListener("status", (e) => {
      const d = JSON.parse(e.data);
      setStatus(d.stage === "tool_executing" ? `executing '${d.tool}'... (attempt ${d.attempt}/${d.max_attempts})`
        : d.stage === "tool_retrying" ? `retrying '${d.tool}'...`
        : d.stage === "awaiting_confirmation" ? "awaiting your confirmation..."
        : d.stage === "intent_classified" ? `intent: ${d.intent}`
        : d.stage);
    });
    eventSource.addEventListener("confirmation_required", (e) => {
      const d = JSON.parse(e.data);
      renderConfirmation(d.tool_calls);
    });
  }

  function renderConfirmation(toolCalls) {
    if (!confirmEl) return;
    confirmEl.innerHTML = "";
    const summary = document.createElement("p");
    summary.textContent = "Confirm action(s): " +
      toolCalls.map(tc => `${tc.name}(${JSON.stringify(tc.args)})`).join(", ");
    confirmEl.appendChild(summary);

    const yes = document.createElement("button");
    yes.textContent = "Confirm";
    const no = document.createElement("button");
    no.textContent = "Decline";

    const decide = (approved) => () => {
      confirmEl.innerHTML = "";
      fetch(`/chat/conversations/${threadId}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved }),
      });
    };
    yes.onclick = decide(true);
    no.onclick = decide(false);
    confirmEl.append(yes, no);
  }

  socket.on("connect", () => {
    setStatus("connected");
    if (threadId) { socket.emit("join", { thread_id: threadId }); connectEvents(); }
  });

  socket.on("assistant_message", (m) => {
    appendBubble("assistant", m.content);
    setStatus("done");
  });

  socket.on("error", (e) => {
    appendBubble("assistant", `⚠ ${e.error || "error"}`);
    setStatus("error");
  });

  window.sendChatMessage = function (text, attachments) {
    if (!threadId) {
      threadId = crypto.randomUUID();
      localStorage.setItem("chat_thread_id", threadId);
      socket.emit("join", { thread_id: threadId });
      connectEvents();
    }
    appendBubble("user", text || "[attachment]");
    socket.emit("send_message", { thread_id: threadId, content: text, attachments: attachments || [] });
    setStatus("thinking...");
  };
})();
