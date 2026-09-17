import io
import uuid
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from database.models import Conversation
import routes.chat as chat_module


@pytest.fixture(autouse=True)
def reset_chat_globals():
    """
    Because routes/chat.py uses in-memory dictionaries for state management 
    between Socket.IO threads, we MUST clear them before and after every test
    to prevent cross-test contamination.
    """
    chat_module._ACTIVE_TURNS.clear()
    chat_module._PENDING.clear()
    chat_module._PENDING_BY_THREAD.clear()
    chat_module._ATTACHMENTS.clear()
    yield
    chat_module._ACTIVE_TURNS.clear()
    chat_module._PENDING.clear()
    chat_module._PENDING_BY_THREAD.clear()
    chat_module._ATTACHMENTS.clear()


@pytest.fixture
def mock_background_task(monkeypatch):
    """Mock background tasks to avoid executing real LLM turns during unit tests."""
    mock_task = MagicMock()
    monkeypatch.setattr("routes.chat.socketio.start_background_task", mock_task)
    return mock_task


@pytest.fixture
def mock_config(monkeypatch):
    """Mocks configuration flags required by the chat endpoints."""
    class MockLLMConfig:
        vision_capable = True

    class MockAgentConfig:
        max_upload_mb = 1
        allowed_upload_extensions = [".png", ".jpg", ".jpeg"]
        max_message_chars = 1000
        confirmation_timeout_seconds = 300
        status_events_enabled = True

    class MockConfig:
        llm_config = MockLLMConfig()
        agent = MockAgentConfig()

    monkeypatch.setattr("routes.chat.config", MockConfig())


@pytest.fixture
def sample_conversation(db_session, customer_user):
    """Provides an existing conversation owned by the customer fixture."""
    conv = Conversation(user_id=customer_user.id, thread_id=uuid.uuid4().hex)
    db_session.add(conv)
    db_session.commit()
    return conv


class TestChatHTTP:
    def test_admin_chat_access_unauth(self, client):
        """Unauthenticated users are redirected or denied."""
        assert client.get("/chat/admin").status_code in [302, 401]

    def test_admin_chat_access_customer(self, customer_client):
        """Standard customers cannot access the playground."""
        assert customer_client.get("/chat/admin").status_code in [302, 401, 403]

    def test_admin_chat_access_admin(self, admin_client):
        """Admins can access the playground."""
        assert admin_client.get("/chat/admin").status_code == 200

    def test_customer_chat_access(self, customer_client, sample_conversation):
        """Customers can access /chat and see their conversations."""
        response = customer_client.get("/chat")
        assert response.status_code == 200
        assert str(sample_conversation.id).encode() in response.data

    def test_chat_ping(self, customer_client):
        """Ping endpoint returns configuration flags."""
        response = customer_client.get("/chat/ping")
        assert response.status_code == 200
        data = response.get_json()
        assert data["ok"] is True
        assert data["authenticated"] is True


class TestChatUploads:
    def test_upload_success(self, customer_client, mock_config):
        """Valid image upload stores attachment in memory temporarily."""
        data = {"file": (io.BytesIO(b"fake image data"), "image.png")}
        response = customer_client.post("/chat/upload", data=data, content_type="multipart/form-data")
        
        assert response.status_code == 200
        json_data = response.get_json()
        attachment_id = json_data["attachment_id"]
        
        assert attachment_id in chat_module._ATTACHMENTS
        assert chat_module._ATTACHMENTS[attachment_id]["filename"] == "image.png"

    def test_upload_invalid_type_and_size(self, customer_client, mock_config, monkeypatch):
        """Unsupported extensions or overly large files are rejected."""
        bad_ext = {"file": (io.BytesIO(b"data"), "file.txt")}
        res1 = customer_client.post("/chat/upload", data=bad_ext, content_type="multipart/form-data")
        assert res1.status_code == 415
        assert b"Unsupported file type" in res1.data

        monkeypatch.setattr(chat_module.config.agent, "max_upload_mb", 0.0001)
        too_large = {"file": (io.BytesIO(b"A" * 1024), "image.png")}
        res2 = customer_client.post("/chat/upload", data=too_large, content_type="multipart/form-data")
        assert res2.status_code == 413
        assert b"exceeds" in res2.data


class TestChatHistory:
    def test_history_success(self, customer_client, sample_conversation, monkeypatch):
        """Valid request calls build_transcript and returns the trace."""
        mock_transcript = {"messages": [{"role": "assistant", "content": "Hi"}], "pending_confirmation": None}
        monkeypatch.setattr("routes.chat.build_transcript", lambda t_id: mock_transcript)
        
        response = customer_client.get(f"/chat/history/{sample_conversation.id}")
        assert response.status_code == 200
        
        data = response.get_json()
        assert data["conversation"]["id"] == sample_conversation.id
        assert data["messages"][0]["content"] == "Hi"

    def test_history_unauthorized(self, customer_client, admin_client, db_session, admin_user):
        """Users cannot view history of conversations they do not own."""
        admin_conv = Conversation(user_id=admin_user.id, thread_id="abc")
        db_session.add(admin_conv)
        db_session.commit()
        
        response = customer_client.get(f"/chat/history/{admin_conv.id}")
        assert response.status_code == 404
        assert b"Conversation not found" in response.data


class TestSocketIOConnection:
    def test_connect_unauth(self, socket_client):
        """Unauthenticated sockets are rejected instantly."""
        assert not socket_client.is_connected()

    def test_connect_auth(self, auth_socket_client):
        """Authenticated sockets join their room and get a connected event."""
        events = auth_socket_client.get_received()
        assert len(events) > 0
        connect_event = next(e for e in events if e["name"] == "connected")
        assert "user_id" in connect_event["args"][0]


class TestSocketIOMessages:
    def test_user_message_new_conversation(self, auth_socket_client, customer_user, mock_background_task, db_session, mock_config):
        """Sending a message creates a new Conversation and starts the background runner."""
        auth_socket_client.get_received() # Clear connection events
        
        auth_socket_client.emit("user_message", {"content": "Hello, bot!"})
        
        mock_background_task.assert_called_once()
        
        events = auth_socket_client.get_received()
        assert len(events) == 1
        assert events[0]["name"] == "conversation_started"
        
        conv_id = events[0]["args"][0]["conversation_id"]
        
        conv = db_session.query(Conversation).filter_by(id=conv_id).first()
        assert conv is not None
        assert conv.user_id == customer_user.id

        # Verify Lock was acquired
        assert customer_user.id in chat_module._ACTIVE_TURNS

    def test_user_message_existing_conversation(self, auth_socket_client, customer_user, sample_conversation, mock_background_task, mock_config):
        """Sending a message with a conversation_id routes to the existing thread."""
        auth_socket_client.get_received()
        
        auth_socket_client.emit("user_message", {
            "content": "Follow up", 
            "conversation_id": sample_conversation.id
        })
        
        mock_background_task.assert_called_once()
        args = mock_background_task.call_args[0]

        assert args[0] == chat_module._run_turn
        assert args[3] == sample_conversation.id 
        assert args[4] == sample_conversation.thread_id

    def test_user_message_concurrency_lock(self, auth_socket_client, customer_user, mock_background_task):
        """If a user already has an active turn, subsequent messages are rejected with an error."""
        auth_socket_client.get_received()
        
        chat_module._ACTIVE_TURNS.add(customer_user.id)
        
        auth_socket_client.emit("user_message", {"content": "Spam!"})
        
        mock_background_task.assert_not_called()
        
        events = auth_socket_client.get_received()
        error_event = next(e for e in events if e["name"] == "chat_error")
        assert error_event["args"][0]["code"] == "busy"


class TestSocketIOConfirmations:
    def test_confirmation_response_valid(self, auth_socket_client, customer_user, mock_background_task, mock_config):
        """Approving a pending confirmation triggers a resume background task."""
        auth_socket_client.get_received()
        
        req_id = "req-123"
        chat_module._PENDING[req_id] = {
            "request_id": req_id,
            "user_id": customer_user.id,
            "conversation_id": 99,
            "thread_id": "thread_abc",
            "created_at": datetime.now(timezone.utc),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=300),
            "tool": "checkout",
            "message": "Approve?",
            "args": {}
        }
        
        auth_socket_client.emit("confirmation_response", {"request_id": req_id, "accepted": True})
        
        assert customer_user.id in chat_module._ACTIVE_TURNS
        
        assert req_id not in chat_module._PENDING
        
        mock_background_task.assert_called_once()
        args = mock_background_task.call_args[0]

        assert args[0] == chat_module._run_resume
        assert args[4] is True  # Accepted
        assert args[5] == "accepted" # Reason

        events = auth_socket_client.get_received()
        resolved = next(e for e in events if e["name"] == "confirmation_resolved")
        assert resolved["args"][0]["accepted"] is True

    def test_confirmation_response_expired(self, auth_socket_client, customer_user, mock_background_task, mock_config):
        """Responding to a timed-out confirmation automatically forces a decline."""
        auth_socket_client.get_received()
        
        req_id = "req-expired"
        chat_module._PENDING[req_id] = {
            "request_id": req_id,
            "user_id": customer_user.id,
            "conversation_id": 99,
            "thread_id": "thread_abc",
            # Created 1 hour ago (expired)
            "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=50),
        }
        
        auth_socket_client.emit("confirmation_response", {"request_id": req_id, "accepted": True})
        
        mock_background_task.assert_called_once()
        args = mock_background_task.call_args[0]
        
        assert args[4] is False
        assert args[5] == "timeout"