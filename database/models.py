import enum
from typing import List, Optional
from datetime import datetime, timezone
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Float, Integer, ForeignKey, Text, DateTime, Enum, Boolean


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy 2.0 declarative models.
    """
    pass

class UserRole(str, enum.Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"

class User(Base):
    """
    Unified User model for authentication and identity.

    Attributes:
        id (int): Primary Key.
        email (str): Email of the User.
        name (str): Name of the User.
        password_hash (str): Stored password hash of the User.
        role (UserRole): Role of the User, can be either 'customer' or 'admin'
        is_active (bool): whether or not the User's account is active.
        phone (Optional[str]): The phone number of the User, can be 9 or 11 digits.
        created_at (datetime): Creation date of the account, defaults to 'datetime.now(timezone.utc)'.
        cart_items (List[CartItem]): The list of Products in the User's Cart.
        orders (List[Order]): List of orders the User has made. 
    """
    __tablename__ = 'users'
    
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255)) # For Flask Admin login
    
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.CUSTOMER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc)
    )

    cart_items: Mapped[List["CartItem"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    
    orders: Mapped[List["Order"]] = relationship(
        back_populates="customer", 
        cascade="all, delete-orphan"
    )


class Product(Base):
    """
    Represents a purchasable item in the e-commerce store.
    
    Attributes:
        id (int): Primary key.
        name (str): Name of the product.
        description (str): Detailed text describing the product.
        price (float): Current price of the product.
        stock_quantity (int): Number of units currently available.
        category (str): Categorization for organizational purposes.
    """
    __tablename__ = 'products'
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(100), nullable=False)


class CartItem(Base):
    """
    Represents an active item in a user's shopping cart before checkout.

    Attributes:
        id (int): Primary Key.
        user_id (int): ID of the User who placed the product in their cart.
        product_id (int): ID of the product placed into the User's cart.
        quantity (int): Amount of the product placed into the cart.
        user (User): The User who placed the product in their cart.
        product (Product): The product placed into the User's cart.
    """
    __tablename__ = 'cart_items'
    
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    
    user: Mapped["User"] = relationship(back_populates="cart_items")
    product: Mapped["Product"] = relationship()


class Order(Base):
    """
    Represents a transaction made by a customer.
    
    Attributes:
        id (int): Primary key.
        customer_id (int): Foreign key referencing the Customer.
        status (str): Current state of the order (e.g., Pending, Shipped).
        total_amount (float): Total cost of the order.
        created_at (datetime): Timestamp of when the order was placed.
        customer (Customer): The customer who placed the order.
        items (List[OrderItem]): The specific products included in this order.
    """
    __tablename__ = 'orders'
    
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="Pending")
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc)
    )
    
    customer: Mapped["User"] = relationship(back_populates="orders")
    items: Mapped[List["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    """
    Represents a specific product and quantity within an Order.
    
    Attributes:
        id (int): Primary key.
        order_id (int): Foreign key referencing the parent Order.
        product_id (int): Foreign key referencing the purchased Product.
        quantity (int): Number of units purchased.
        unit_price (float): Price per unit at the time of purchase.
        order (Order): The parent order.
        product (Product): The associated product.
    """
    __tablename__ = 'order_items'
    
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, nullable=False)
    
    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()


class KnowledgeDocument(Base):
    """
    Stores RAG text and metadata for easy management via the admin dashboard.
    
    Updates to instances of this model should be synchronized with the 
    Vector Database (ChromaDB) to ensure search queries reflect current data.
    
    Attributes:
        id (int): Primary key. Matches the document ID in ChromaDB.
        title (str): Descriptive title of the document.
        content (str): The actual text content to be embedded.
        doc_type (str): Classification of the document (e.g., FAQ, Policy).
        created_at (datetime): Timestamp of creation.
        updated_at (datetime): Timestamp of the last update.
    """
    __tablename__ = 'knowledge_documents'
    
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[str] = mapped_column(String(50), default="General")
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc), 
        onupdate=lambda: datetime.now(timezone.utc)
    )