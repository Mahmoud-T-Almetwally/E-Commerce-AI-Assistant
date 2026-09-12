import asyncio
from typing import List, Optional
import logging

from pydantic import Field
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from sqlalchemy import or_, func, update
from sqlalchemy.orm import joinedload

from database.db_setup import SessionLocal
from database.models import Product, CartItem, Order, OrderItem, Tag, OrderStatus
from database.rag_manager import get_rag_manager
from utils.sanitizers import escape_like


logger = logging.getLogger(__name__)


@tool
async def display_recommendations(
    config: RunnableConfig,
    query: Optional[str] = Field(default=None, max_length=300,
                                 description="Search term for product name or visual description."),
    category: Optional[str] = Field(default=None, max_length=100, description="Category of the product."),
    min_price: Optional[float] = Field(default=None, ge=0, description="Minimum price."),
    max_price: Optional[float] = Field(default=None, ge=0, description="Maximum price."),
    tags: Optional[List[str]] = Field(default=None, max_length=10, description="List of tags to filter by."),
) -> str:
    """Searches the database for products matching the criteria, displays them to the user via UI."""
    def _db_op():
        with SessionLocal() as db:
            q = db.query(Product)
            if query:
                escaped = escape_like(query)
                q = q.filter(or_(
                    Product.name.ilike(f"%{escaped}%", escape="\\"),
                    Product.description.ilike(f"%{escaped}%", escape="\\"),
                ))
            if category:
                q = q.filter(Product.category.ilike(f"%{escape_like(category)}%", escape="\\"))
            if min_price is not None:
                q = q.filter(Product.price >= min_price)
            if max_price is not None:
                q = q.filter(Product.price <= max_price)
            if tags:
                tag_lower = [t.lower() for t in tags]
                q = q.filter(Product.tags.any(func.lower(Tag.name).in_(tag_lower)))

            return q.limit(10).all()

    products = await asyncio.to_thread(_db_op)

    if not products:
        return "No products found matching the criteria."

    product_data = [{
        "id": p.id,
        "name": p.name,
        "price": float(p.price),
        "description": p.description,
        "image_url": p.image_url,
    } for p in products]

    room = config.get("configurable", {}).get("thread_id")
    if room:
        try:
            from utils.extensions import socketio
            # server.emit works outside a request context (tools run on worker threads).
            socketio.server.emit("product_carousel", {"products": product_data},
                                 room=room, namespace="/chat")
        except Exception:
            logger.warning("Failed to push product carousel to room %s", room, exc_info=True)

    return f"Successfully displayed {len(products)} recommended products to the user."


@tool
async def add_to_cart(config: RunnableConfig,
                      product_id: int = Field(description="ID of the product to add.", ge=1),
                      quantity: int = Field(default=1, ge=1, le=99,
                                            description="How many units to add.")) -> str:
    """Adds a specified quantity of a product to the user's cart. The user must confirm this action before it executes."""
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return "Error: User is not logged in."

    def _db_op():
        with SessionLocal() as db:
            product = db.query(Product).filter(Product.id == product_id).with_for_update().first()
            if not product:
                return "Error: Product not found."

            if product.stock_quantity < quantity:
                return f"Error: Only {product.stock_quantity} units available."

            cart_item = db.query(CartItem).filter(
                CartItem.user_id == user_id,
                CartItem.product_id == product_id,
            ).first()

            if cart_item:
                if product.stock_quantity < (cart_item.quantity + quantity):
                    return "Error: Cannot add more. Stock limit reached."
                cart_item.quantity += quantity
            else:
                db.add(CartItem(user_id=user_id, product_id=product_id, quantity=quantity))

            db.commit()
            return f"Successfully added {quantity} of '{product.name}' to the cart."

    return await asyncio.to_thread(_db_op)


@tool
async def view_cart(config: RunnableConfig) -> str:
    """Returns the current contents of the user's shopping cart."""
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return "Error: User is not logged in."

    def _db_op():
        with SessionLocal() as db:
            cart_items = db.query(CartItem).options(joinedload(CartItem.product)) \
                .filter(CartItem.user_id == user_id).all()
            if not cart_items:
                return "The cart is empty."

            res = ["Cart Contents:"]
            total = 0
            for item in cart_items:
                cost = item.product.price * item.quantity
                total += cost
                res.append(f"- {item.quantity}x {item.product.name} (ID: {item.product.id}) "
                           f"@ ${item.product.price:.2f} each")
            res.append(f"Total: ${total:.2f}")
            return "\n".join(res)

    return await asyncio.to_thread(_db_op)


@tool
async def checkout(config: RunnableConfig) -> str:
    """Processes the user's cart and creates an order. The user must strictly confirm this action before it executes."""
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return "Error: User is not logged in."

    def _db_op():
        with SessionLocal() as db:
            cart_items = db.query(CartItem).options(joinedload(CartItem.product)) \
                .filter(CartItem.user_id == user_id).all()
            if not cart_items:
                return "Error: Cart is empty."

            total_amount = sum(item.product.price * item.quantity for item in cart_items)
            new_order = Order(customer_id=user_id, status=OrderStatus.PENDING,
                              total_amount=total_amount)
            db.add(new_order)
            db.flush()

            for item in cart_items:
                updated = db.execute(
                    update(Product)
                    .where(Product.id == item.product_id,
                           Product.stock_quantity >= item.quantity)
                    .values(stock_quantity=Product.stock_quantity - item.quantity)
                ).rowcount

                if updated != 1:
                    db.rollback()
                    return (f"Error: '{item.product.name}' is out of stock or has "
                            f"insufficient quantity.")

                db.add(OrderItem(order_id=new_order.id, product_id=item.product_id,
                                 quantity=item.quantity,
                                 unit_price=item.product.price))
                db.delete(item)

            db.commit()
            return f"Successfully placed order #{new_order.id} for a total of ${total_amount:.2f}."

    return await asyncio.to_thread(_db_op)


@tool
async def check_order_status(config: RunnableConfig,
                             order_id: int = Field(description="ID of the order to check.", ge=1)) -> str:
    """Checks the status of an order given its ID."""
    user_id = config.get("configurable", {}).get("user_id")
    if not user_id:
        return "Error: User is not logged in."

    def _db_op():
        with SessionLocal() as db:
            order = db.query(Order).filter(Order.id == order_id).first()
            if not order:
                return "Error: Order not found."
            if order.customer_id != user_id:
                return "Error: You do not have permission to view this order."

            return (f"Order #{order.id} is currently '{order.status.value}'. "
                    f"Total amount: ${order.total_amount:.2f}.")

    return await asyncio.to_thread(_db_op)


@tool
async def search_knowledge_base(query: str = Field(max_length=500,
                                                   description="The question to search the knowledge base for.")) -> str:
    """Searches the knowledge base for company policies, shipping info, FAQs, and general information."""
    def _db_op():
        rag = get_rag_manager()
        results = rag.search(query, k=3)
        if not results:
            return "No relevant information found in the knowledge base."
        return "\n\n".join([
            f"Source: {doc.metadata.get('title', 'Unknown')}\n{doc.page_content}"
            for doc in results
        ])

    return await asyncio.to_thread(_db_op)
