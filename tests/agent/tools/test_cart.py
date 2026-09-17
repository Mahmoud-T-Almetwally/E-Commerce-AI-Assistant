import pytest
from agent.tools.cart import (
    view_cart, 
    add_to_cart, 
    update_cart_quantity, 
    remove_from_cart, 
    checkout
)
from database.models import Order, OrderStatus, Product
from utils.exceptions import OutOfStockError, RecordNotFoundError

class TestCartTools:
    
    def test_add_and_view_cart(self, db_session, seed_data):
        user_id = seed_data["user_id"]
        product_id = seed_data["product_in_stock_id"]
        
        initial_cart = view_cart.func(user_id=user_id)
        assert initial_cart["data"]["item_count"] == 0
        
        add_result = add_to_cart.func(product_id=product_id, quantity=2, user_id=user_id)
        assert add_result["status"] == "success"
        assert add_result["data"]["quantity_in_cart"] == 2
        
        final_cart = view_cart.func(user_id=user_id)
        assert final_cart["data"]["item_count"] == 1
        assert final_cart["data"]["total"] > 0

    def test_add_to_cart_out_of_stock(self, db_session, seed_data):
        """Test domain exception is thrown when limits exceeded."""
        user_id = seed_data["user_id"]
        product_id = seed_data["product_out_of_stock_id"]
        
        with pytest.raises(OutOfStockError):
            add_to_cart.func(product_id=product_id, quantity=1, user_id=user_id)

    def test_update_and_remove_cart_items(self, db_session, seed_data):
        user_id = seed_data["user_id"]
        product_id = seed_data["product_in_stock_id"]
        
        add_to_cart.func(product_id=product_id, quantity=1, user_id=user_id)
        
        update_result = update_cart_quantity.func(product_id=product_id, quantity=5, user_id=user_id)
        assert update_result["status"] == "success"
        assert update_result["data"]["quantity"] == 5
        
        remove_result = remove_from_cart.func(product_id=product_id, user_id=user_id)
        assert remove_result["status"] == "success"
        
        cart = view_cart.func(user_id=user_id)
        assert cart["data"]["item_count"] == 0

    def test_checkout_success_and_stock_deduction(self, db_session, seed_data):
        """Verify checkout creates an order and atomically reduces product stock."""
        user_id = seed_data["user_id"]
        product_id = seed_data["product_in_stock_id"]
        
        add_to_cart.func(product_id=product_id, quantity=10, user_id=user_id)
        
        initial_stock = db_session.get(Product, product_id).stock_quantity
        
        result = checkout.func(user_id=user_id)
        assert result["status"] == "success"
        
        order_id = result["data"]["order_id"]
        order = db_session.get(Order, order_id)
        assert order is not None
        assert order.customer_id == user_id
        
        final_stock = db_session.get(Product, product_id).stock_quantity
        assert final_stock == initial_stock - 10
        
        cart = view_cart.func(user_id=user_id)
        assert cart["data"]["item_count"] == 0

    def test_checkout_empty_cart(self, db_session, seed_data):
        """Checkout should return an error envelope if cart is empty."""
        user_id = seed_data["user_id"]
        result = checkout.func(user_id=user_id)
        
        assert result["status"] == "error"
        assert result["error"]["code"] == "cart_empty"