import pytest
from decimal import Decimal
from flask import session

from database.models import Product, CartItem, Order, OrderItem, OrderStatus

class TestStorePublic:
    def test_index_redirect(self, client):
        """Root URL should redirect to the products page."""
        response = client.get("/")
        assert response.status_code == 302
        assert response.headers["Location"] == "/products"

    def test_list_products(self, client, db_session, sample_product):
        """Product catalog should render and display available products."""
        p2 = Product(name="Keyboard", description="ke bord", price=Decimal("49.99"), category="Peripherals", stock_quantity=5)
        db_session.add(p2)
        db_session.commit()

        response = client.get("/products")
        assert response.status_code == 200
        assert b"Test Headphones" in response.data
        assert b"Keyboard" in response.data

        res_search = client.get("/products?search=Keyboard&category=Peripherals")
        assert b"Keyboard" in res_search.data
        assert b"Test Headphones" not in res_search.data

    def test_product_detail(self, client, sample_product):
        """Product detail page should render correctly."""
        response = client.get(f"/products/{sample_product.id}")
        assert response.status_code == 200
        assert b"Test Headphones" in response.data
        assert b"Noise cancelling" in response.data

    def test_product_detail_404(self, client):
        """Missing product should flash error and redirect."""
        response = client.get("/products/9999")
        assert response.status_code == 302
        assert response.headers["Location"] == "/products"


class TestStoreCart:
    def test_view_cart_unauthorized(self, client):
        """Anonymous users trying to view cart should be redirected to login."""
        response = client.get("/cart")
        assert response.status_code in [302, 401]

    def test_add_to_cart_success(self, customer_client, db_session, sample_product):
        """Adding a product to cart should work and respect stock."""
        response = customer_client.post("/cart/add", data={
            "product_id": sample_product.id,
            "quantity": 2
        })
        assert response.status_code == 302
        
        cart_item = db_session.query(CartItem).filter_by(product_id=sample_product.id).first()
        assert cart_item is not None
        assert cart_item.quantity == 2

    def test_add_to_cart_out_of_stock(self, customer_client, db_session, sample_product):
        """Cannot add more quantity to cart than what is available in stock."""
        response = customer_client.post("/cart/add", data={
            "product_id": sample_product.id,
            "quantity": 15  # Stock is only 10
        })
        assert response.status_code == 302
        
        # Verify cart is empty
        assert db_session.query(CartItem).count() == 0

    def test_update_cart_quantity(self, customer_client, db_session, customer_user, sample_product):
        """Updating cart quantities should succeed if stock permits."""
        item = CartItem(user_id=customer_user.id, product_id=sample_product.id, quantity=1)
        db_session.add(item)
        db_session.commit()

        customer_client.post("/cart/update", data={"cart_item_id": item.id, "quantity": 5})
        db_session.refresh(item)
        assert item.quantity == 5

        customer_client.post("/cart/update", data={"cart_item_id": item.id, "quantity": 11})
        db_session.refresh(item)
        assert item.quantity == 5

    def test_remove_from_cart(self, customer_client, db_session, customer_user, sample_product):
        """Removing an item should delete the CartItem row."""
        item = CartItem(user_id=customer_user.id, product_id=sample_product.id, quantity=1)
        db_session.add(item)
        db_session.commit()

        response = customer_client.post(f"/cart/remove/{item.id}")
        assert response.status_code == 302
        assert db_session.query(CartItem).count() == 0


class TestStoreCheckout:
    @pytest.fixture
    def cart_with_item(self, db_session, customer_user, sample_product):
        item = CartItem(user_id=customer_user.id, product_id=sample_product.id, quantity=2)
        db_session.add(item)
        db_session.commit()
        return item

    def test_checkout_get(self, customer_client, cart_with_item):
        """Checkout GET should display order summary."""
        response = customer_client.get("/checkout")
        assert response.status_code == 200
        assert b"Test Headphones" in response.data
        # 2 * 199.99 = 399.98
        assert b"399.98" in response.data

    def test_checkout_post_success(self, customer_client, db_session, cart_with_item, sample_product):
        """Successful checkout should drain cart, deduct stock, and create Order."""
        response = customer_client.post("/checkout")
        assert response.status_code == 302
        assert "/orders/" in response.headers["Location"]

        assert db_session.query(CartItem).count() == 0

        db_session.refresh(sample_product)
        assert sample_product.stock_quantity == 8

        order = db_session.query(Order).first()
        assert order is not None
        assert order.total_amount == Decimal("399.98")
        assert order.status == OrderStatus.PENDING
        
        assert len(order.items) == 1
        assert order.items[0].product_id == sample_product.id
        assert order.items[0].quantity == 2

    def test_checkout_race_condition_stock(self, customer_client, db_session, cart_with_item, sample_product):
        """
        If stock depletes between adding to cart and checking out, 
        checkout should fail safely using the optimistic lock.
        """
        sample_product.stock_quantity = 1
        db_session.commit()

        response = customer_client.post("/checkout", follow_redirects=False)
        
        assert response.status_code == 302
        assert "/cart" in response.headers["Location"]
        
        with customer_client.session_transaction() as sess:
            flashes = [msg for category, msg in sess.get('_flashes', [])]
            # Ensure the product name is mentioned in the out-of-stock flash message
            assert any("Test Headphones" in msg for msg in flashes)
        
        # 3. Verify nothing was changed in the database
        db_session.refresh(sample_product)
        assert sample_product.stock_quantity == 1
        assert db_session.query(Order).count() == 0
        assert db_session.query(CartItem).count() == 1


class TestStoreOrders:
    def test_list_orders(self, customer_client, db_session, customer_user, sample_product):
        """User can view their order history."""
        order = Order(customer_id=customer_user.id, status=OrderStatus.SHIPPED, total_amount=Decimal("199.99"))
        db_session.add(order)
        db_session.commit()

        response = customer_client.get("/orders")
        assert response.status_code == 200
        assert b"199.99" in response.data

    def test_order_detail(self, customer_client, db_session, customer_user, sample_product):
        """User can view specific details of an order they own."""
        order = Order(customer_id=customer_user.id, status=OrderStatus.DELIVERED, total_amount=Decimal("199.99"))
        db_session.add(order)
        db_session.flush()
        
        item = OrderItem(order_id=order.id, product_id=sample_product.id, quantity=1, unit_price=Decimal("199.99"))
        db_session.add(item)
        db_session.commit()

        response = customer_client.get(f"/orders/{order.id}")
        assert response.status_code == 200
        assert b"Test Headphones" in response.data

    def test_order_detail_unauthorized(self, customer_client, db_session, admin_user):
        """User cannot view another person's order."""
        order = Order(customer_id=admin_user.id, status=OrderStatus.PENDING, total_amount=Decimal("10.00"))
        db_session.add(order)
        db_session.commit()

        response = customer_client.get(f"/orders/{order.id}")
        assert response.status_code == 302
        assert response.headers["Location"] == "/orders"