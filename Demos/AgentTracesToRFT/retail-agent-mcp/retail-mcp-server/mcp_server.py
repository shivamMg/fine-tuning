import os
import json
import ast
import logging
import operator
import uuid
import contextvars
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)

MCP_API_KEY_HEADER = "X-MCP-API-Key"
MCP_READ_ONLY_HEADER = "X-MCP-Read-Only"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


mcp = FastMCP(
    "Retail MCP Server",
    # Disable DNS rebinding protection to avoid "Invalid Host header" error in Azure Container Apps.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# Context variable to indicate if the current request is read-only, set by auth_middleware based on MCP_READ_ONLY_HEADER.
# Tools check this variable to determine if they should actually modify data or just return what would have been modified.
read_only_var: contextvars.ContextVar[bool] = contextvars.ContextVar("read_only", default=False)


def load_db():
    with open(os.path.join(BASE_DIR, "db.json")) as f:
        _db = json.load(f)

    # Shift all delivered_at dates to keep data relevant for policy_verify_return and other tools.
    max_delivered_at = "2025-11-06T23:42:41.835913"  # max delivered_at in db.json
    offset = datetime.now() - datetime.fromisoformat(max_delivered_at)
    for order in _db["orders"].values():
        if "delivered_at" in order:
            order["delivered_at"] = (datetime.fromisoformat(order["delivered_at"]) + offset).isoformat()
    return _db


db = load_db()


@mcp.tool(description="Calculate the result of a mathematical expression.")
def calculate(expression: str) -> str:
    # Safe math evaluator
    SAFE_OPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def _safe_eval(node):
        if isinstance(node, ast.Expression):
            return _safe_eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in SAFE_OPS:
            return SAFE_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in SAFE_OPS:
            return SAFE_OPS[type(node.op)](_safe_eval(node.operand))
        raise ValueError("Unsupported expression")
    
    try:
        result = _safe_eval(ast.parse(expression, mode="eval"))
        return json.dumps({"result": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool(description="Find user id by email.")
def find_user_id_by_email(email: str) -> str:
    for uid, user in db["users"].items():
        if user["email"] == email:
            return json.dumps({"user_id": uid})
    return json.dumps({"error": "User not found"})


@mcp.tool(description="Find user id by first name, last name, and zip code.")
def find_user_id_by_name_zip(first_name: str, last_name: str, zip: str) -> str:
    for uid, user in db["users"].items():
        if (
            user["name"]["first_name"].lower() == first_name.lower()
            and user["name"]["last_name"].lower() == last_name.lower()
            and user["address"]["zip"] == zip
        ):
            return json.dumps({"user_id": uid})
    return json.dumps({"error": "User not found"})


@mcp.tool(description="List the name and product id of all product types.")
def list_all_product_types() -> str:
    return json.dumps(
        [{"name": p["name"], "product_id": pid} for pid, p in db["products"].items()]
    )


@mcp.tool(description="Get the inventory details of a product.")
def get_product_details(product_id: str) -> str:
    if product_id in db["products"]:
        return json.dumps(db["products"][product_id])
    return json.dumps({"error": "Product not found"})


@mcp.tool(description="Get the details of a user.")
def get_user_details(user_id: str) -> str:
    if user_id in db["users"]:
        return json.dumps(db["users"][user_id])
    return json.dumps({"error": "User not found"})


@mcp.tool(description="Get the status and details of an order.")
def get_order_details(order_id: str) -> str:
    if order_id in db["orders"]:
        return json.dumps(db["orders"][order_id])
    return json.dumps({"error": "Order not found"})


@mcp.tool(
    description="Cancel a pending order. Reason must be 'no longer needed' or 'ordered by mistake'."
)
def cancel_pending_order(order_id: str, reason: str) -> str:
    if order_id not in db["orders"]:
        return json.dumps({"error": "Order not found"})
    order = db["orders"][order_id]
    if order.get("status") != "pending":
        return json.dumps(
            {"error": f"Order is '{order.get('status')}', only pending orders can be cancelled"}
        )
    if read_only_var.get():
        return json.dumps(order)
    order["status"] = "cancelled"
    total = sum(item["price"] for item in order["items"])
    pm = order["payment_history"][0]["payment_method_id"]
    order["payment_history"].append(
        {"transaction_type": "refund", "amount": total, "payment_method_id": pm, "reason": reason}
    )
    return json.dumps(order)


@mcp.tool(description="Modify items in a pending order to new items of the same product type.")
def modify_pending_order_items(
    order_id: str, item_ids: list[str], new_item_ids: list[str], payment_method_id: str
) -> str:
    if order_id not in db["orders"]:
        return json.dumps({"error": "Order not found"})
    order = db["orders"][order_id]
    if order.get("status") != "pending":
        return json.dumps(
            {"error": f"Order is '{order.get('status')}', only pending orders can be modified"}
        )
    if len(item_ids) != len(new_item_ids):
        return json.dumps({"error": "item_ids and new_item_ids must have the same length"})
    if read_only_var.get():
        return json.dumps(order)
    old_total = sum(i["price"] for i in order["items"])
    for old_id, new_id in zip(item_ids, new_item_ids):
        item = next((i for i in order["items"] if i["item_id"] == old_id), None)
        if not item:
            return json.dumps({"error": f"Item {old_id} not found in order"})
        product = db["products"].get(item["product_id"])
        if not product or new_id not in product["variants"]:
            return json.dumps({"error": f"Item {new_id} is not a valid variant of {item['name']}"})
        variant = product["variants"][new_id]
        item["item_id"] = new_id
        item["price"] = variant["price"]
        item["options"] = variant["options"]
    new_total = sum(i["price"] for i in order["items"])
    diff = round(new_total - old_total, 2)
    if diff != 0:
        order["payment_history"].append(
            {
                "transaction_type": "payment" if diff > 0 else "refund",
                "amount": abs(diff),
                "payment_method_id": payment_method_id,
            }
        )
    return json.dumps(order)


@mcp.tool(description="Modify the payment method of a pending order.")
def modify_pending_order_payment(order_id: str, payment_method_id: str) -> str:
    if order_id not in db["orders"]:
        return json.dumps({"error": "Order not found"})
    order = db["orders"][order_id]
    if order.get("status") != "pending":
        return json.dumps(
            {"error": f"Order is '{order.get('status')}', only pending orders can be modified"}
        )
    if read_only_var.get():
        return json.dumps(order)
    order["payment_history"][0]["payment_method_id"] = payment_method_id
    return json.dumps(order)


@mcp.tool(description="Modify the shipping address of a pending order.")
def modify_pending_order_address(
    order_id: str, address1: str, address2: str, city: str, state: str, country: str, zip: str
) -> str:
    if order_id not in db["orders"]:
        return json.dumps({"error": "Order not found"})
    order = db["orders"][order_id]
    if order.get("status") != "pending":
        return json.dumps(
            {"error": f"Order is '{order.get('status')}', only pending orders can be modified"}
        )
    if read_only_var.get():
        return json.dumps(order)
    order["address"] = {
        "address1": address1,
        "address2": address2,
        "city": city,
        "state": state,
        "country": country,
        "zip": zip,
    }
    return json.dumps(order)


@mcp.tool(description="Modify the default address of a user.")
def modify_user_address(
    user_id: str, address1: str, address2: str, city: str, state: str, country: str, zip: str
) -> str:
    if user_id not in db["users"]:
        return json.dumps({"error": "User not found"})
    if read_only_var.get():
        return json.dumps(db["users"][user_id])
    db["users"][user_id]["address"] = {
        "address1": address1,
        "address2": address2,
        "city": city,
        "state": state,
        "country": country,
        "zip": zip,
    }
    return json.dumps(db["users"][user_id])


@mcp.tool(description="Exchange items in a delivered order to new items of the same product type.")
def exchange_delivered_order_items(
    order_id: str, item_ids: list[str], new_item_ids: list[str], payment_method_id: str
) -> str:
    if order_id not in db["orders"]:
        return json.dumps({"error": "Order not found"})
    order = db["orders"][order_id]
    if order.get("status") != "delivered":
        return json.dumps(
            {"error": f"Order is '{order.get('status')}', only delivered orders can be exchanged"}
        )
    if len(item_ids) != len(new_item_ids):
        return json.dumps({"error": "item_ids and new_item_ids must have the same length"})
    if read_only_var.get():
        return json.dumps(order)
    for old_id, new_id in zip(item_ids, new_item_ids):
        item = next((i for i in order["items"] if i["item_id"] == old_id), None)
        if not item:
            return json.dumps({"error": f"Item {old_id} not found in order"})
        product = db["products"].get(item["product_id"])
        if not product or new_id not in product["variants"]:
            return json.dumps({"error": f"Item {new_id} is not a valid variant of {item['name']}"})
        variant = product["variants"][new_id]
        diff = round(variant["price"] - item["price"], 2)
        item["item_id"] = new_id
        item["price"] = variant["price"]
        item["options"] = variant["options"]
        if diff != 0:
            order["payment_history"].append(
                {
                    "transaction_type": "payment" if diff > 0 else "refund",
                    "amount": abs(diff),
                    "payment_method_id": payment_method_id,
                }
            )
    order["status"] = "exchange requested"
    return json.dumps(order)


@mcp.tool(description="Return some items of a delivered order.")
def return_delivered_order_items(order_id: str, item_ids: list[str], payment_method_id: str) -> str:
    if order_id not in db["orders"]:
        return json.dumps({"error": "Order not found"})
    order = db["orders"][order_id]
    if order.get("status") != "delivered":
        return json.dumps(
            {"error": f"Order is '{order.get('status')}', only delivered orders can be returned"}
        )
    if read_only_var.get():
        return json.dumps(order)
    refund = 0.0
    for item_id in item_ids:
        item = next((i for i in order["items"] if i["item_id"] == item_id), None)
        if not item:
            return json.dumps({"error": f"Item {item_id} not found in order"})
        refund += item["price"]
    order["status"] = "return requested"
    order["payment_history"].append(
        {"transaction_type": "refund", "amount": round(refund, 2), "payment_method_id": payment_method_id}
    )
    return json.dumps(order)


@mcp.tool(
    description="List recent orders for a user, optionally filtered by date range. "
    "This allows the agent to identify the correct order when the user only mentions product details or approximate timing."
)
def list_user_orders(
    user_id: str, from_date: str = "", to_date: str = "", limit: int = 10
) -> str:
    if user_id not in db["users"]:
        return json.dumps({"error": "User not found"})
    user = db["users"][user_id]
    order_ids = user.get("orders", [])
    orders = []
    for oid in order_ids:
        order = db["orders"].get(oid)
        if not order:
            continue
        # Date filtering using delivered_at when available
        if from_date or to_date:
            order_date_str = order.get("delivered_at")
            if not order_date_str:
                # Include orders with no date (e.g. pending) only if no date filter is set
                continue
            try:
                order_date = datetime.fromisoformat(order_date_str)
                if from_date and order_date < datetime.fromisoformat(from_date):
                    continue
                if to_date and order_date > datetime.fromisoformat(to_date):
                    continue
            except ValueError:
                continue
        orders.append(order)
        if len(orders) >= limit:
            break
    return json.dumps(orders)


@mcp.tool(
    description="Verify return eligibility and calculate fees based on store policy. "
    "Call this after you know order_id, item_ids, and reason to check if a return is allowed "
    "and what options are available. This tool evaluates return windows, category-specific rules, "
    "user tier benefits, and restocking fees."
)
def policy_verify_return(order_id: str, item_ids: list[str], reason: str) -> str:
    VALID_REASONS = ("unwanted", "wrong_item", "size_issue", "defective", "other")
    if reason not in VALID_REASONS:
        return json.dumps({"error": f"Invalid reason. Must be one of: {', '.join(VALID_REASONS)}"})
    if order_id not in db["orders"]:
        return json.dumps({"error": "Order not found"})
    order = db["orders"][order_id]
    if order.get("status") != "delivered":
        return json.dumps(
            {"error": f"Order is '{order.get('status')}', only delivered orders are eligible for return"}
        )
    # Determine user tier
    user = db["users"].get(order.get("user_id", ""), {})
    tier = user.get("tier", "standard")
    if tier == "vip":
        return_window_days = 60
    elif tier == "gold":
        return_window_days = 30
    else:
        return_window_days = 7
    delivered_at_str = order.get("delivered_at")
    if delivered_at_str:
        try:
            delivered_at = datetime.fromisoformat(delivered_at_str)
            days_since = (datetime.now() - delivered_at).days
            if days_since > return_window_days:
                return json.dumps({
                    "eligible": False,
                    "reason": f"Return window expired. {days_since} days since delivery exceeds {return_window_days}-day limit for {tier} tier.",
                })
        except ValueError:
            pass
    # Validate items exist in the order
    refund_total = 0.0
    items_detail = []
    for item_id in item_ids:
        item = next((i for i in order["items"] if i["item_id"] == item_id), None)
        if not item:
            return json.dumps({"error": f"Item {item_id} not found in order"})
        items_detail.append(item)
        refund_total += item["price"]
    # Restocking fee: 0% for defective/wrong_item, 15% for unwanted/other/size_issue; gold and vip tiers always 0%
    if reason in {"defective", "wrong_item"} or tier in {"vip", "gold"}:
        restocking_fee_pct = 0.0
    else:
        restocking_fee_pct = 0.15
    restocking_fee = round(refund_total * restocking_fee_pct, 2)
    net_refund = round(refund_total - restocking_fee, 2)
    return json.dumps({
        "eligible": True,
        "order_id": order_id,
        "items": [{"item_id": i["item_id"], "name": i["name"], "price": i["price"]} for i in items_detail],
        "reason": reason,
        "user_tier": tier,
        "return_window_days": return_window_days,
        "restocking_fee_pct": restocking_fee_pct,
        "restocking_fee": restocking_fee,
        "refund_subtotal": round(refund_total, 2),
        "net_refund": net_refund,
    })


@mcp.tool(description="Transfer the user to a human agent, with a summary of the user's issue.")
def transfer_to_human_agents(summary: str) -> str:
    return json.dumps(
        {"message": "Transfer initiated. A human agent will follow up shortly.", "summary": summary}
    )


mcp_http_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    async with mcp_http_app.router.lifespan_context(mcp_http_app):
        yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/tools" and request.method == "GET":
        return await call_next(request)

    MCP_API_KEY = os.environ["MCP_API_KEY"]
    if MCP_API_KEY and request.headers.get(MCP_API_KEY_HEADER) != MCP_API_KEY:
        return Response("Unauthorized", status_code=401)

    # The read-only header is optional; default to writable mode when omitted.
    read_only_header = request.headers.get(MCP_READ_ONLY_HEADER, "false")
    read_only_var.set(read_only_header.lower() == "true")
    return await call_next(request)


@app.post("/tools")
async def invoke_tool(request: Request):
    """OpenAI compatible endpoint for tool invocation."""
    body = await request.json()
    name = body.get("name")
    call_id = body.get("call_id") or f"call_{uuid.uuid4().hex[:24]}"
    request_id = body.get("id")
    arguments_raw = body.get("arguments", "{}")

    logger.info("[call_id:%s] /tools Request: id=%s name=%s arguments=%s", call_id, request_id, name, arguments_raw)

    try:
        kwargs = json.loads(arguments_raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("[call_id:%s] Invalid arguments for tool %s: %s", call_id, name, e)
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid arguments: {e}"},
        )

    try:
        result = await mcp.call_tool(name, kwargs)
        # call_tool returns (content_blocks, structured); extract text from first block
        content_blocks = result[0] if isinstance(result, tuple) else result
        output = content_blocks[0].text if content_blocks else ""
    except Exception as e:
        logger.warning("[call_id:%s] Tool call failed: name=%s error=%s", call_id, name, e)
        return JSONResponse(
            status_code=400,
            content={"error": str(e)},
        )

    response = {
        "type": "function_call_output",
        "id": request_id,
        "call_id": call_id,
        "output": output,
    }
    logger.info("[call_id:%s] /tools Response: %s", call_id, response)
    return response


@app.get("/tools")
async def list_tools(request: Request):
    tools = sorted(mcp._tool_manager.list_tools(), key=lambda t: t.name)
    return [t.name for t in tools]


def tool_endpoints():
    """Print JSON tool endpoints for use in RFT job."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, "../.env"))

    server_url = os.environ["MCP_ENDPOINT"].rstrip("/") + "/tools"
    headers = {
        MCP_API_KEY_HEADER: os.environ["MCP_API_KEY"],
        MCP_READ_ONLY_HEADER: "true"
    }
    tools = sorted(mcp._tool_manager.list_tools(), key=lambda t: t.name)
    return [
        {"name": t.name, "server_url": server_url, "headers": headers}
        for t in tools
    ]


app.mount("/", mcp_http_app)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
