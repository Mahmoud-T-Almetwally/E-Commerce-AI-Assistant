import enum
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

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
        role (UserRole): Role of the User, can be either 'customer' or 'admin'.
        is_active (bool): Whether or not the User's account is active.
        phone (Optional[str]): The phone number of the User, can be 9 or 11 digits.
        created_at (datetime): Creation date of the account.
        updated_at (datetime): Update date of the account.
        cart_items (List[CartItem]): The list of Products in the User's Cart.
        orders (List[Order]): List of orders the User has made.
        conversations (List[Conversation]): AI chat threads belonging to this User.
    """
    __tablename__ = 'users'

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))  # For Flask Admin login

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

    cart_items: Mapped[List["CartItem"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan"
    )

    orders: Mapped[List["Order"]] = relationship(
        back_populates="customer",
        cascade="all, delete-orphan"
    )

    conversations: Mapped[List["Conversation"]] = relationship(back_populates="user")


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
        price (Decimal): Current price of the product.
        stock_quantity (int): Number of units currently available.
        category (str): Categorization for organizational purposes.
        tags (List[Tag]): Tags associated with the product.
    """
    __tablename__ = 'products'

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    image_url: Mapped[str] = mapped_column(
        String(255), 
        default="/static/images/default-product.png",
        server_default="/static/images/default-product.png"
    )
    tags: Mapped[List["Tag"]] = relationship(secondary=product_tags, back_populates="products")


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
        customer_id (int): Foreign key referencing the User who placed the order.
        status (OrderStatus): Current state of the order: 'pending', 'processing',
            'shipped', 'delivered', 'cancelled'.
        total_amount (Decimal): Total cost of the order.
        created_at (datetime): Timestamp of when the order was placed.
        customer (User): The customer who placed the order.
        items (List[OrderItem]): The specific products included in this order.
    """
    __tablename__ = 'orders'

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    customer: Mapped["User"] = relationship(back_populates="orders")
    items: Mapped[List["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan"
    )


class OrderItem(Base):
    """
    Represents a specific product and quantity within an Order.

    Attributes:
        id (int): Primary key.
        order_id (int): Foreign key referencing the parent Order.
        product_id (int): Foreign key referencing the purchased Product.
        quantity (int): Number of units purchased.
        unit_price (Decimal): Price per unit at the time of purchase.
        order (Order): The parent order.
        product (Product): The associated product.
    """
    __tablename__ = 'order_items'

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()


class Conversation(Base):
    """
    Tracks LangGraph execution threads and ties AI conversation memory to specific users.

    Attributes:
        id (int): Primary key.
        user_id (Optional[int]): Foreign key to the User (nullable to allow anonymous/guest chats).
        thread_id (str): The unique string ID utilized by the LangGraph checkpointer.
        created_at (datetime): Timestamp when the conversation started.
        updated_at (datetime): Timestamp of the last interaction.
        user (Optional[User]): The user engaging in this conversation.
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


class AgentTurnStats(Base):
    """
    Persisted record of one agent graph execution ("turn") — powers the
    admin dashboard's full-length and aggregated statistics.

    A turn that pauses for user confirmation writes TWO rows: an
    'interrupted' row at the pause (tokens/tool calls up to that point) and a
    terminal row ('completed'/'error') when the resumed run finishes. Token
    aggregates therefore sum over ALL rows; turn counts must exclude
    status='interrupted'.

    Deleting the parent Conversation removes its stat rows (explicit cascade
    in the admin delete endpoint; ondelete CASCADE covers enforced-FK
    deployments). Store orders are never touched.

    Attributes:
        id (int): Primary Key.
        conversation_id (Optional[int]): FK to the Conversation this turn ran in.
        user_id (Optional[int]): FK to the User who chatted.
        thread_id (str): LangGraph thread id (matches Conversation.thread_id).
        started_at / finished_at (datetime): Turn boundaries (UTC).
        status (str): 'completed' | 'interrupted' | 'error'.
        intent (Optional[str]): Classified intent for the turn.
        guard_blocked (bool): Whether the guard node refused the message.
        tokens_in / tokens_out (int): Token usage across the turn's rows.
        llm_calls (int): Assistant messages produced during the turn.
        error (Optional[str]): Short error summary for failed turns.
        tool_calls (List[AgentToolCall]): Tool executions within this turn.
    """
    __tablename__ = 'agent_turn_stats'

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    thread_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    intent: Mapped[Optional[str]] = mapped_column(String(50))
    guard_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(String(500))

    tool_calls: Mapped[List["AgentToolCall"]] = relationship(
        back_populates="turn", cascade="all, delete-orphan")


class AgentToolCall(Base):
    """
    One tool execution outcome within a turn (see AgentTurnStats).

    Attributes:
        id (int): Primary Key.
        turn_id (int): FK to the parent AgentTurnStats row.
        conversation_id (Optional[int]): FK to the Conversation (denormalized
            for direct dashboard queries).
        user_id (Optional[int]): Denormalized user id (plain integer by
            design — no FK, so user lifecycle never rewrites stats).
        tool_name (str): Registered tool name.
        state (str): 'done' | 'error' | 'declined' | 'blocked'.
        duration_ms (Optional[int]): Wall time of the execution loop.
        attempts (int): Total attempts made (retries + 1).
        order_id (Optional[int]): Order created by a successful chat checkout.
        created_at (datetime): When the call finished (UTC).
        turn (AgentTurnStats): The parent turn.
    """
    __tablename__ = 'agent_tool_calls'

    id: Mapped[int] = mapped_column(primary_key=True)
    turn_id: Mapped[int] = mapped_column(
        ForeignKey("agent_turn_stats.id", ondelete="CASCADE"), index=True, nullable=False)
    conversation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)

    tool_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)

    turn: Mapped["AgentTurnStats"] = relationship(back_populates="tool_calls")


class MessengerIdentity(Base):
    """
    Links a Facebook Messenger Page-Scoped ID (PSID) to a store User.

    v1 (auto-provisioned): the User is created on first contact with a
    synthetic, non-loginable email (no password hash). A future account
    linking flow can re-point `user_id` at a real customer account with zero
    migration — conversations (thread ids) are unaffected, only this row
    changes; `linked_at` is reserved for that.

    Attributes:
        id (int): Primary Key.
        psid (str): Page-scoped id — unique per Page↔person pair.
        user_id (int): FK to the mapped User (CASCADE with it).
        created_at (datetime): First contact.
        linked_at (Optional[datetime]): Reserved for account linking.
        user (User): The mapped user.
    """
    __tablename__ = 'messenger_identities'

    id: Mapped[int] = mapped_column(primary_key=True)
    psid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    linked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship()