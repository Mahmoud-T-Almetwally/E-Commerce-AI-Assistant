import pytest
from agent.tools.catalog import search_products, get_product_details
from utils.exceptions import RecordNotFoundError

class TestCatalogTools:
    
    def test_search_products_by_query(self, db_session, seed_data):
        """Test free-text search hitting names/descriptions."""
        result = search_products.func(query="Wireless Earbuds", max_results=5)
        
        assert result["status"] == "success"
        assert result["data"]["count"] == 1
        product = result["data"]["results"][0]
        assert product["name"] == "Test Wireless Earbuds"
        assert result["ui_event"]["event"] == "display_product_carousel"
        assert product["id"] in result["ui_event"]["product_ids"]

    def test_search_products_by_filters(self, db_session, seed_data):
        """Test tag and category filtering."""
        result = search_products.func(tags="sale", category="Electronics")
        assert result["status"] == "success"
        assert result["data"]["count"] == 1
        
        result_miss = search_products.func(tags="shoes")
        assert result_miss["status"] == "success"
        assert result_miss["data"]["count"] == 0

    def test_search_products_price_bounds(self, db_session, seed_data):
        """Test price bounding logic."""
        result = search_products.func(price_max=50.0)
        assert result["status"] == "success"
        assert result["data"]["count"] == 1
        assert result["data"]["results"][0]["name"] == "Test Coffee Mug"

    def test_search_products_invalid_arguments(self, db_session, seed_data):
        """Test that missing arguments triggers a model error envelope."""
        result = search_products.func()
        assert result["status"] == "error"
        assert result["error"]["code"] == "invalid_arguments"

    def test_get_product_details_success(self, db_session, seed_data):
        """Test retrieving full product details."""
        product_id = seed_data["product_in_stock_id"]
        result = get_product_details.func(product_id=product_id)
        
        assert result["status"] == "success"
        assert result["data"]["name"] == "Test Wireless Earbuds"
        assert result["data"]["in_stock"] is True

    def test_get_product_details_not_found(self, db_session, seed_data):
        """Test that a bad ID correctly raises the domain exception."""
        with pytest.raises(RecordNotFoundError):
            get_product_details.func(product_id=9999)