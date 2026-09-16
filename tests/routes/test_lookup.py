import pytest

class TestLookupCategories:
    def test_lookup_categories_no_query(self, client, lookup_data):
        """Should return all categories sorted by usage frequency (descending)."""
        response = client.get("/lookup/categories")
        assert response.status_code == 200
        data = response.get_json()
        
        assert len(data) == 2
        assert data[0] == {"value": "Electronics", "count": 3}
        assert data[1] == {"value": "Books", "count": 1}

    def test_lookup_categories_with_query(self, client, lookup_data):
        """Should filter categories by case-insensitive substring."""
        response = client.get("/lookup/categories?q=book")
        assert response.status_code == 200
        data = response.get_json()
        
        assert len(data) == 1
        assert data[0] == {"value": "Books", "count": 1}


class TestLookupTags:
    def test_lookup_tags_no_query(self, client, lookup_data):
        """Should return all tags sorted by product attachment frequency."""
        response = client.get("/lookup/tags")
        assert response.status_code == 200
        data = response.get_json()
        
        assert len(data) == 2
        assert data[0] == {"value": "Sale", "count": 2}
        assert data[1] == {"value": "New", "count": 1}

    def test_lookup_tags_with_query(self, client, lookup_data):
        """Should filter tags by case-insensitive substring."""
        response = client.get("/lookup/tags?q=sa")
        assert response.status_code == 200
        data = response.get_json()
        
        assert len(data) == 1
        assert data[0]["value"] == "Sale"


class TestLookupProducts:
    def test_lookup_products_valid_ids(self, client, lookup_data):
        """Should fetch product card details and preserve the exact order requested."""
        p1, p2, _, p4 = lookup_data["products"]
        
        response = client.get(f"/lookup/products?ids={p4.id},{p1.id}")
        assert response.status_code == 200
        data = response.get_json()
        
        assert len(data) == 2
        
        assert data[0]["id"] == p4.id
        assert data[0]["name"] == "Novel"
        assert data[0]["category"] == "Books"
        assert data[0]["in_stock"] is True
        
        assert data[1]["id"] == p1.id
        assert data[1]["name"] == "Laptop"

    def test_lookup_products_invalid_and_missing_ids(self, client, lookup_data):
        """Should gracefully ignore non-numeric, missing, and duplicate IDs."""
        p1 = lookup_data["products"][0]
        
        response = client.get(f"/lookup/products?ids={p1.id},9999,abc,,")
        assert response.status_code == 200
        data = response.get_json()
        
        assert len(data) == 1
        assert data[0]["id"] == p1.id

    def test_lookup_products_stock_status(self, client, lookup_data):
        """Verify the `in_stock` boolean calculates correctly based on stock_quantity."""
        p2, p3 = lookup_data["products"][1], lookup_data["products"][2]
        
        response = client.get(f"/lookup/products?ids={p2.id},{p3.id}")
        data = response.get_json()
        
        assert len(data) == 2
        assert data[0]["in_stock"] is True
        assert data[1]["in_stock"] is False

    def test_lookup_products_truncates_at_max(self, client):
        """Should not parse more than MAX_CAROUSEL_IDS (12) from the input string."""
        ids_str = ",".join(str(i) for i in range(1, 21))
        
        response = client.get(f"/lookup/products?ids={ids_str}")
        assert response.status_code == 200
        
        data = response.get_json()
        assert len(data) <= 12