import enum
from typing import List, Optional
from datetime import datetime

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import (
    String, Integer, ForeignKey, Text, DateTime, Enum, 
    Boolean, Numeric, UniqueConstraint, Table, Column, func
)


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy 2.0 declarative models.
    """
    pass

class UserRole(str, enum.Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


product_tags = Table(
    "product_tags",
    Base.metadata,
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)
)



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
        created_at (datetime): Creation date of the account.
        updated_at (datetime): Update date of the account.
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
        server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        onupdate=func.now()
    )

    cart_items: Mapped[List["CartItem"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    
    orders: Mapped[List["Order"]] = relationship(
        back_populates="customer", 
        cascade="all, delete-orphan"
    )


class Tag(Base):
    """
    Represents a searchable keyword or attribute (e.g., "Sale", "Electronics", "Wireless").
    
    Attributes:
        id (int): Primary key.
        name (str): The unique name of the tag.
        products (List[Product]): The products associated with this tag.
    """
    __tablename__ = 'tags'
    
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    
    products: Mapped[List["Product"]] = relationship(
        secondary=product_tags, 
        back_populates="tags"
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
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
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
    __table_args__ = (UniqueConstraint('user_id', 'product_id', name='uq_user_product_cart'),)
    
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
        status (OrderStatus): Current state of the order can be one of the following: 'pending', 'processing', 'shipped', 'delivered', 'canceled'.
        total_amount (float): Total cost of the order.
        created_at (datetime): Timestamp of when the order was placed.
        customer (Customer): The customer who placed the order.
        items (List[OrderItem]): The specific products included in this order.
    """
    __tablename__ = 'orders'
    
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)
    total_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now()
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
    unit_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    
    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()


class Conversation(Base):
    """
    Tracks LangGraph execution threads and ties AI conversation memory to specific users.
    
    Attributes:
        id (int): Primary key.
        user_id (int): Optional foreign key to the User (nullable to allow anonymous/guest chats).
        thread_id (str): The unique string ID utilized by the LangGraph checkpointer.
        created_at (datetime): Timestamp when the conversation started.
        updated_at (datetime): Timestamp of the last interaction.
        user (User): The user engaging in this conversation.
    """
    __tablename__ = 'conversations'
    
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    thread_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        onupdate=func.now()
    )
    
    user: Mapped[Optional["User"]] = relationship(back_populates="conversations")


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
        server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        onupdate=func.now()
    )