import random

from sqlalchemy.exc import IntegrityError

from database.db_setup import engine, SessionLocal, init_db
from database.models import (
    Base, User, UserRole, OrderStatus, Tag, Product, 
    CartItem, Order, OrderItem, KnowledgeDocument
)
from database.rag_manager import get_rag_manager

from werkzeug.security import generate_password_hash

def reset_database():
    """Drops all existing tables and recreates them to ensure a clean state."""
    print("Dropping existing tables...")
    Base.metadata.drop_all(bind=engine)
    print("Creating tables...")
    init_db()

def generate_seed_data():
    """Generates mock data for all models and inserts it into the database."""
    db = SessionLocal()
    
    try:
        print("Seeding Tags...")
        tag_names = ["Sale", "Electronics", "Wireless", "Gaming", "Home", "Office", "New Arrival", "Refurbished", "Limited Edition", "Eco-friendly"]
        tags = [Tag(name=name) for name in tag_names]
        db.add_all(tags)
        db.flush()

        print("Seeding Products...")
        categories = {
            "Laptops": ["ProBook", "AirBook", "Gamer Pro", "DevMachine", "UltraSlim"],
            "Smartphones": ["Phone X", "Phone 11", "Foldable Z", "Note Max", "Pixelator"],
            "Audio": ["Noise Cancelling Headphones", "Earbuds Pro", "Studio Mics", "Bluetooth Speaker"],
            "Accessories": ["Wireless Mouse", "Mechanical Keyboard", "USB-C Hub", "Laptop Stand", "Webcam"]
        }
        
        products = []
        for i in range(1, 51):
            category = random.choice(list(categories.keys()))
            base_name = random.choice(categories[category])
            
            product = Product(
                name=f"{base_name} Gen {random.randint(1, 5)} - Model {i}",
                description=f"High-quality {category.lower()} designed for professionals and enthusiasts. Experience unmatched performance.",
                price=round(random.uniform(19.99, 1499.99), 2),
                stock_quantity=random.randint(0, 200),
                category=category
            )
            products.append(product)
        
        db.add_all(products)
        db.flush() 

        for tag in tags:
            sampled_products = random.sample(products, random.randint(5, 15))
            tag.products.extend(sampled_products)
        db.flush()

        print("Seeding Users...")
        users = []
        
        admin = User(
            email="admin@store.com",
            name="System Admin",
            password_hash=generate_password_hash("Admin123!"),
            role=UserRole.ADMIN,
            is_active=True,
            phone="12345678901"
        )
        users.append(admin)
        
        for i in range(1, 21):
            customer = User(
                email=f"customer{i}@example.com",
                name=f"Customer Name {i}",
                password_hash=generate_password_hash(f"dummy_hash_user_{i}"),
                role=UserRole.CUSTOMER,
                is_active=random.choices([True, False], weights=[90, 10])[0],
                phone=f"010{random.randint(10000000, 99999999)}"
            )
            users.append(customer)
            
        db.add_all(users)
        db.flush() 

        print("Seeding Cart Items...")
        cart_items = []
        for customer in users[1:]:
            if random.random() < 0.6:
                cart_products = random.sample(products, random.randint(1, 4))
                for product in cart_products:
                    cart_item = CartItem(
                        user_id=customer.id,
                        product_id=product.id,
                        quantity=random.randint(1, 3)
                    )
                    cart_items.append(cart_item)
        db.add_all(cart_items)
        
        print("Seeding Orders and Order Items...")
        order_items = []
        order_statuses = list(OrderStatus)
        
        for customer in users[1:]:
            for _ in range(random.randint(0, 4)):
                order = Order(
                    customer_id=customer.id,
                    status=random.choice(order_statuses),
                    total_amount=0.0 # Will be calculated below
                )

                db.add(order)
                db.flush()

                total_amount = 0.0
                purchased_products = random.sample(products, random.randint(1, 5))
                
                for product in purchased_products:
                    qty = random.randint(1, 3)
                    unit_price = product.price
                    
                    order_item = OrderItem(
                        order_id=order.id,
                        product_id=product.id,
                        quantity=qty,
                        unit_price=unit_price
                    )
                    order_items.append(order_item)
                    total_amount += (float(unit_price) * qty)
                
                order.total_amount = round(total_amount, 2)
        
        db.add_all(order_items)

        print("Seeding Knowledge Documents...")
        documents = [
            KnowledgeDocument(
                title="Return & Refund Policy",
                content="We offer a 30-day money-back guarantee on all products. If you are not satisfied, return the product in its original packaging.",
                doc_type="Policy"
            ),
            KnowledgeDocument(
                title="Shipping Information",
                content="Standard shipping takes 3-5 business days. Expedited shipping is available at checkout for an additional fee. Free shipping on orders over $50.",
                doc_type="FAQ"
            ),
            KnowledgeDocument(
                title="Warranty Coverage",
                content="All electronics come with a standard 1-year manufacturer warranty covering defects in materials and workmanship.",
                doc_type="Policy"
            ),
            KnowledgeDocument(
                title="How to track my order?",
                content="Once your order ships, you will receive an email with a tracking number. You can also view your tracking details in your Account Dashboard under 'Orders'.",
                doc_type="FAQ"
            )
        ]
        
        db.add_all(documents)
        db.flush()
        get_rag_manager().resync(documents)

        db.commit()
        print("Successfully seeded the database!")

    except IntegrityError as e:
        db.rollback()
        print(f"Database Integrity Error: {e}")
    except Exception as e:
        db.rollback()
        print(f"An error occurred: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    reset_database()
    generate_seed_data()