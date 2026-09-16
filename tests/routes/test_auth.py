import pytest
from flask import session
from werkzeug.security import check_password_hash

from database.models import User, UserRole

class TestLogin:
    def test_login_page_renders(self, client):
        """GET /login should render the login form."""
        response = client.get("/login")
        assert response.status_code == 200
        assert b"login" in response.data.lower()

    def test_login_customer_success(self, client, customer_user):
        """Valid customer login should establish a session and redirect to store index."""
        response = client.post("/login", data={
            "email": "customer@test.com",
            "password": "password123"
        })
        assert response.status_code == 302
        assert response.headers["Location"] == "/"
        
        assert session.get("user_id") == customer_user.id
        assert session.get("role") == UserRole.CUSTOMER.value

    def test_login_admin_success(self, client, admin_user):
        """Valid admin login should establish a session and redirect to admin dashboard."""
        response = client.post("/login", data={
            "email": "admin_login@test.com",
            "password": "adminpass123"
        })
        assert response.status_code == 302
        assert response.headers["Location"] == "/admin/"
        
        assert session.get("user_id") == admin_user.id
        assert session.get("role") == UserRole.ADMIN.value

    def test_login_invalid_credentials(self, client, customer_user):
        """Invalid passwords or emails should flash an error and deny session."""
        response = client.post("/login", data={
            "email": "customer@test.com",
            "password": "wrongpassword"
        })
        assert response.status_code == 200  # Renders the template again with flash
        assert b"Invalid email or password" in response.data
        assert not session.get("user_id")

    def test_login_inactive_user(self, client, db_session, customer_user):
        """Users marked as inactive should not be able to log in."""
        customer_user.is_active = False
        db_session.commit()

        response = client.post("/login", data={
            "email": "customer@test.com",
            "password": "password123"
        })
        assert response.status_code == 302
        assert response.headers["Location"] == "/login"
        
        # Follow the redirect to check the flash message
        flash_response = client.get("/login")
        assert b"account has been deactivated" in flash_response.data
        assert not session.get("user_id")

    def test_login_next_url_safe_redirect(self, client, customer_user):
        """If a valid local `next` parameter is provided, login should redirect there."""
        response = client.post("/login?next=/store/products", data={
            "email": "customer@test.com",
            "password": "password123"
        })
        assert response.status_code == 302
        assert response.headers["Location"] == "/store/products"

    def test_login_next_url_unsafe_redirect(self, client, customer_user):
        """If an external/malicious `next` parameter is provided, it should be ignored."""
        response = client.post("/login?next=http://evil.com/phishing", data={
            "email": "customer@test.com",
            "password": "password123"
        })
        assert response.status_code == 302
        # Should fallback to the default Customer redirect route
        assert response.headers["Location"] == "/"


class TestRegister:
    def test_register_page_renders(self, client):
        """GET /register should render the form."""
        response = client.get("/register")
        assert response.status_code == 200
        assert b"register" in response.data.lower()

    def test_register_success(self, client, db_session):
        """Valid data should create a CUSTOMER user and redirect to login."""
        response = client.post("/register", data={
            "name": "Jane Doe",
            "email": "jane@test.com",
            "phone": "123456789",
            "password": "securepassword",
            "confirm_password": "securepassword"
        })
        assert response.status_code == 302
        assert response.headers["Location"] == "/login"

        new_user = db_session.query(User).filter_by(email="jane@test.com").first()
        assert new_user is not None
        assert new_user.name == "Jane Doe"
        assert new_user.role == UserRole.CUSTOMER  # Must be customer
        assert check_password_hash(new_user.password_hash, "securepassword")

    @pytest.mark.parametrize("form_data, expected_error", [
        # Missing Name
        ({
            "name": "", "email": "j@t.com", "password": "pass", "confirm_password": "pass"
        }, b"Full name is required."),
        
        # Bad Email
        ({
            "name": "J", "email": "invalid-email", "password": "pass", "confirm_password": "pass"
        }, b"Please enter a valid email address."),
        
        # Passwords mismatch
        ({
            "name": "J", "email": "j@t.com", "password": "pass", "confirm_password": "different"
        }, b"Passwords do not match."),
        
        # Password too short
        ({
            "name": "J", "email": "j@t.com", "password": "123", "confirm_password": "123"
        }, b"Password must be at least 6 characters long."),
        
        # Invalid Phone
        ({
            "name": "J", "email": "j@t.com", "password": "password", "confirm_password": "password", "phone": "123"
        }, b"Phone number must be 9 or 11 digits (digits only)."),
    ])
    def test_register_validations(self, client, form_data, expected_error):
        """Test registration constraints like matching passwords, email formatting, and lengths."""
        response = client.post("/register", data=form_data)
        assert response.status_code == 200  # Rerenders form
        assert expected_error in response.data

    def test_register_duplicate_email(self, client, customer_user):
        """Attempting to register with an email that already exists should fail safely."""
        response = client.post("/register", data={
            "name": "Clone User",
            "email": "customer@test.com",
            "password": "password123",
            "confirm_password": "password123"
        })
        assert response.status_code == 200
        assert b"An account with this email already exists" in response.data


class TestSessionState:
    def test_already_logged_in_redirects(self, client, customer_user):
        """If a user is already logged in, they shouldn't access login/register pages."""
        client.post("/login", data={"email": "customer@test.com", "password": "password123"})
        
        login_response = client.get("/login")
        assert login_response.status_code == 302
        assert login_response.headers["Location"] == "/"

        register_response = client.get("/register")
        assert register_response.status_code == 302
        assert register_response.headers["Location"] == "/"

    def test_logout(self, client, customer_user):
        """Hitting /logout should drop the session and redirect."""
        client.post("/login", data={"email": "customer@test.com", "password": "password123"})
        assert session.get("user_id") is not None
        
        logout_response = client.get("/logout")
        assert logout_response.status_code == 302
        assert logout_response.headers["Location"] == "/"
        
        assert not session.get("user_id")