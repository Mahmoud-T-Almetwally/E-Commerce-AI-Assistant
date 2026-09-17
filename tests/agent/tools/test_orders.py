import pytest
from agent.tools.orders import list_recent_orders, get_order_status
from database.models import Order, OrderStatus
from utils.exceptions import RecordNotFoundError

class TestOrdersTools:

    def test_list_recent_orders(self, db_session, seed_data):
        user_id = seed_data["user_id"]
        order = Order(customer_id=user_id, status=OrderStatus.SHIPPED, total_amount=150.00)
        db_session.add(order)
        db_session.commit()
        
        result = list_recent_orders.func(user_id=user_id)
        assert result["status"] == "success"
        assert result["data"]["count"] == 1
        assert result["data"]["orders"][0]["status"] == OrderStatus.SHIPPED.value
        assert result["data"]["orders"][0]["total"] == 150.0

    def test_get_order_status_success(self, db_session, seed_data):
        user_id = seed_data["user_id"]
        order = Order(customer_id=user_id, status=OrderStatus.PROCESSING)
        db_session.add(order)
        db_session.commit()
        
        result = get_order_status.func(order_id=order.id, user_id=user_id)
        assert result["status"] == "success"
        assert result["data"]["status"] == OrderStatus.PROCESSING.value

    def test_get_order_status_not_found_or_forbidden(self, db_session, seed_data):
        other_order = Order(customer_id=999, status=OrderStatus.DELIVERED)
        db_session.add(other_order)
        db_session.commit()
        
        with pytest.raises(RecordNotFoundError):
            get_order_status.func(order_id=other_order.id, user_id=seed_data["user_id"])