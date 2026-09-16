import pytest
from decimal import Decimal

from database.models import Product, Order, OrderItem, User, UserRole, OrderStatus, Tag


class TestAdminAuth:
    """Ensures unauthorized users cannot access admin paths."""
    
    @pytest.mark.parametrize("path", [
        "/admin/",
        "/admin/products",
        "/admin/orders",
        "/admin/customers"
    ])
    def test_admin_routes_protected(self, client, path):
        response = client.get(path)
        # Should redirect to login or throw a 401/403
        assert response.status_code in [302, 401, 403]


class TestAdminDashboard:
    def test_dashboard_renders(self, admin_client, db_session):
        """Dashboard should render correctly and compile stats."""
        # Create a mock order to ensure stats calculations don't crash
        user = User(name="Test", email="test@test.com", role=UserRole.CUSTOMER)
        order = Order(customer=user, total_amount=Decimal("150.00"), status=OrderStatus.SHIPPED)
        db_session.add_all([user, order])
        db_session.commit()

        response = admin_client.get("/admin/")
        assert response.status_code == 200
        assert b"150.0" in response.data or b"150" in response.data


class TestAdminProducts:
    def test_list_products(self, admin_client, db_session):
        product = Product(name="Admin Phone", description="Description", category="Electronics", price=Decimal("599.99"))
        db_session.add(product)
        db_session.commit()

        response = admin_client.get("/admin/products?search=Admin&category=Electronics")
        assert response.status_code == 200
        assert b"Admin Phone" in response.data

    def test_add_product_success(self, admin_client, db_session):
        data = {
            "name": "Wireless Mouse",
            "category": "Peripherals",
            "description": "Ergonomic wireless mouse",
            "price": "29.99",
            "stock_quantity": "50",
            "tags": "wireless, mouse, office"
        }
        response = admin_client.post("/admin/products/add", data=data, follow_redirects=True)
        assert response.status_code == 200
        
        # Verify Database Insert
        product = db_session.query(Product).filter_by(name="Wireless Mouse").first()
        assert product is not None
        assert product.price == Decimal("29.99")
        assert product.stock_quantity == 50
        assert len(product.tags) == 3
        
        tag_names = [t.name.lower() for t in product.tags]
        assert "wireless" in tag_names

    @pytest.mark.parametrize("form_data, expected_error", [
        ({"name": "", "category": "cat"}, b"Name and category are required."),
        ({"name": "a", "category": "cat", "price": "invalid"}, b"Price must be a valid number"),
        ({"name": "a", "category": "cat", "price": "10.001"}, b"Price supports at most 2 decimal places"),
        ({"name": "a", "category": "cat", "price": "-5.00"}, b"Price cannot be negative."),
        ({"name": "a", "category": "cat", "price": "10", "stock_quantity": "-2"}, b"Stock quantity cannot be negative."),
        ({"name": "a", "category": "cat", "price": "10", "stock_quantity": "1.5"}, b"Stock quantity must be a whole number."),
    ])
    def test_add_product_validations(self, admin_client, form_data, expected_error):
        """Testing `parse_product_form()` boundary constraints via the endpoint."""
        response = admin_client.post("/admin/products/add", data=form_data)
        assert expected_error in response.data

    def test_edit_product_and_orphan_tags(self, admin_client, db_session):
        # Create initial product and tag
        tag = Tag(name="legacy")
        product = Product(name="Old Item", description="Description", category="Cat", price=Decimal("10.00"), tags=[tag])
        db_session.add(product)
        db_session.commit()

        # Update product and change tags
        response = admin_client.post(f"/admin/products/{product.id}/edit", data={
            "name": "New Item",
            "category": "Cat",
            "price": "15.00",
            "stock_quantity": "10",
            "tags": "modern"
        }, follow_redirects=True)
        
        assert response.status_code == 200
        
        db_session.refresh(product)
        assert product.name == "New Item"
        assert product.price == Decimal("15.00")
        assert len(product.tags) == 1
        assert product.tags[0].name == "modern"

        # Verify orphan tag "legacy" was purged (`purge_orphan_tags`)
        legacy_tag = db_session.query(Tag).filter_by(name="legacy").first()
        assert legacy_tag is None

    def test_delete_product(self, admin_client, db_session):
        product = Product(name="To Delete", description="Description", category="Cat", price=Decimal("1.00"))
        db_session.add(product)
        db_session.commit()

        response = admin_client.post(f"/admin/products/{product.id}/delete", follow_redirects=True)
        assert response.status_code == 200
        assert db_session.query(Product).filter_by(id=product.id).first() is None


class TestAdminOrders:
    @pytest.fixture
    def test_order(self, db_session):
        customer = User(email="buyer@test.com", name="Buyer", role=UserRole.CUSTOMER)
        product = Product(name="Gadget", description="Description", category="Tech", price=Decimal("100.00"), stock_quantity=10)
        db_session.add_all([customer, product])
        db_session.commit()

        order = Order(customer_id=customer.id, status=OrderStatus.PENDING, total_amount=Decimal("200.00"))
        db_session.add(order)
        db_session.commit()
        
        item = OrderItem(order_id=order.id, product_id=product.id, quantity=2, unit_price=Decimal("100.00"))
        db_session.add(item)
        db_session.commit()

        return order

    def test_list_orders(self, admin_client, test_order):
        response = admin_client.get(f"/admin/orders?status={OrderStatus.PENDING.value}&customer=buyer")
        assert response.status_code == 200
        assert b"buyer@test.com" in response.data

    def test_update_order_status_valid(self, admin_client, db_session, test_order):
        """Test a valid state transition PENDING -> PROCESSING"""
        response = admin_client.post(f"/admin/orders/{test_order.id}/status", data={"status": OrderStatus.PROCESSING.value}, follow_redirects=True)
        
        db_session.refresh(test_order)
        assert test_order.status == OrderStatus.PROCESSING
        assert b"status updated" in response.data.lower()

    def test_update_order_status_invalid_transition(self, admin_client, db_session, test_order):
        """Test an invalid state transition from PENDING -> DELIVERED directly."""
        response = admin_client.post(f"/admin/orders/{test_order.id}/status", data={"status": OrderStatus.DELIVERED.value}, follow_redirects=True)
        
        db_session.refresh(test_order)
        # Should refuse the transition and remain PENDING
        assert test_order.status == OrderStatus.PENDING
        assert b"Cannot move order" in response.data

    def test_cancel_order_restores_inventory(self, admin_client, db_session, test_order):
        """Crucial Business Logic: Cancelling an order must restore the stock quantities."""
        product = test_order.items[0].product
        original_stock = product.stock_quantity  # Should be 10 from fixture
        qty_in_order = test_order.items[0].quantity # Should be 2 from fixture

        response = admin_client.post(f"/admin/orders/{test_order.id}/status", data={"status": OrderStatus.CANCELLED.value}, follow_redirects=True)
        assert response.status_code == 200
        
        db_session.refresh(test_order)
        db_session.refresh(product)
        
        assert test_order.status == OrderStatus.CANCELLED
        # 10 existing stock + 2 returned from order = 12
        assert product.stock_quantity == original_stock + qty_in_order


class TestAdminCustomers:
    def test_list_customers(self, admin_client, db_session):
        user = User(name="Loyal Customer", email="loyal@test.com", role=UserRole.CUSTOMER, is_active=True)
        db_session.add(user)
        db_session.commit()

        response = admin_client.get("/admin/customers?search=Loyal&is_active=true")
        assert response.status_code == 200
        assert b"loyal@test.com" in response.data

    def test_create_admin_success(self, admin_client, db_session):
        response = admin_client.post("/admin/users/create-admin", data={
            "name": "New Admin",
            "email": "newadmin@test.com",
            "password": "securepassword123",
            "phone": "123456789"
        }, follow_redirects=True)

        assert response.status_code == 200
        
        new_admin = db_session.query(User).filter_by(email="newadmin@test.com").first()
        assert new_admin is not None
        assert new_admin.role == UserRole.ADMIN
        assert new_admin.is_active is True
        assert b"created successfully" in response.data

    def test_create_admin_validations(self, admin_client, db_session):
        # 1. Test duplicate email handling
        existing = User(name="Existing", email="exist@test.com", role=UserRole.ADMIN)
        db_session.add(existing)
        db_session.commit()

        res1 = admin_client.post("/admin/users/create-admin", data={
            "name": "Dup", "email": "exist@test.com", "password": "password123"
        }, follow_redirects=True)
        assert b"already exists" in res1.data

        # 2. Test invalid phone formatting
        res2 = admin_client.post("/admin/users/create-admin", data={
            "name": "Bad Phone", "email": "phone@test.com", "password": "password123", "phone": "123"  # invalid length
        }, follow_redirects=True)
        assert b"9 or 11 digits" in res2.data

        # 3. Test short password
        res3 = admin_client.post("/admin/users/create-admin", data={
            "name": "Bad Pass", "email": "pass@test.com", "password": "123"
        }, follow_redirects=True)
        assert b"at least 6 characters" in res3.data