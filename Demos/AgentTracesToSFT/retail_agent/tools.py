import ast
import json
import operator
import os
from datetime import datetime

from langchain_core.tools import tool


class AgentTools:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

    def load_db(base_dir: str = BASE_DIR) -> dict:
        with open(os.path.join(base_dir, "db.json"), encoding="utf-8") as f:
            loaded_db = json.load(f)

        max_delivered_at = "2025-11-06T23:42:41.835913"
        offset = datetime.now() - datetime.fromisoformat(max_delivered_at)
        for order in loaded_db["orders"].values():
            if "delivered_at" in order:
                order["delivered_at"] = (
                    datetime.fromisoformat(order["delivered_at"]) + offset
                ).isoformat()
        return loaded_db

    db: dict = load_db()
    load_db = staticmethod(load_db)

    @staticmethod
    @tool
    def calculate(expression: str) -> str:
        """Calculate the result of a mathematical expression."""
        safe_ops = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.USub: operator.neg,
            ast.UAdd: operator.pos,
        }

        def safe_eval(node: ast.AST) -> float:
            if isinstance(node, ast.Expression):
                return safe_eval(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return float(node.value)
            if isinstance(node, ast.BinOp) and type(node.op) in safe_ops:
                return safe_ops[type(node.op)](safe_eval(node.left), safe_eval(node.right))
            if isinstance(node, ast.UnaryOp) and type(node.op) in safe_ops:
                return safe_ops[type(node.op)](safe_eval(node.operand))
            raise ValueError("Unsupported expression")

        try:
            result = safe_eval(ast.parse(expression, mode="eval"))
            if result.is_integer():
                return json.dumps({"result": int(result)})
            return json.dumps({"result": result})
        except Exception as e:
            return json.dumps({"error": str(e)})

    @staticmethod
    @tool
    def find_user_id_by_email(email: str) -> str:
        """Find user id by email."""
        for uid, user in AgentTools.db["users"].items():
            if user["email"] == email:
                return json.dumps({"user_id": uid})
        return json.dumps({"error": "User not found"})

    @staticmethod
    @tool
    def find_user_id_by_name_zip(first_name: str, last_name: str, zip: str) -> str:
        """Find user id by first name, last name, and zip code."""
        for uid, user in AgentTools.db["users"].items():
            if (
                user["name"]["first_name"].lower() == first_name.lower()
                and user["name"]["last_name"].lower() == last_name.lower()
                and user["address"]["zip"] == zip
            ):
                return json.dumps({"user_id": uid})
        return json.dumps({"error": "User not found"})

    @staticmethod
    @tool
    def list_all_product_types() -> str:
        """List the name and product id of all product types."""
        return json.dumps(
            [
                {"name": product["name"], "product_id": product_id}
                for product_id, product in AgentTools.db["products"].items()
            ]
        )

    @staticmethod
    @tool
    def get_product_details(product_id: str) -> str:
        """Get the inventory details of a product."""
        if product_id in AgentTools.db["products"]:
            return json.dumps(AgentTools.db["products"][product_id])
        return json.dumps({"error": "Product not found"})

    @staticmethod
    @tool
    def get_user_details(user_id: str) -> str:
        """Get the details of a user."""
        if user_id in AgentTools.db["users"]:
            return json.dumps(AgentTools.db["users"][user_id])
        return json.dumps({"error": "User not found"})

    @staticmethod
    @tool
    def get_order_details(order_id: str) -> str:
        """Get the status and details of an order."""
        if order_id in AgentTools.db["orders"]:
            return json.dumps(AgentTools.db["orders"][order_id])
        return json.dumps({"error": "Order not found"})

    @staticmethod
    @tool
    def cancel_pending_order(order_id: str, reason: str) -> str:
        """Cancel a pending order. Reason must be 'no longer needed' or 'ordered by mistake'."""
        if order_id not in AgentTools.db["orders"]:
            return json.dumps({"error": "Order not found"})
        order = AgentTools.db["orders"][order_id]
        if order.get("status") != "pending":
            return json.dumps(
                {"error": f"Order is '{order.get('status')}', only pending orders can be cancelled"}
            )
        order["status"] = "cancelled"
        total = sum(item["price"] for item in order["items"])
        payment_method = order["payment_history"][0]["payment_method_id"]
        order["payment_history"].append(
            {
                "transaction_type": "refund",
                "amount": total,
                "payment_method_id": payment_method,
                "reason": reason,
            }
        )
        return json.dumps(order)

    @staticmethod
    @tool
    def modify_pending_order_items(
        order_id: str, item_ids: list[str], new_item_ids: list[str], payment_method_id: str
    ) -> str:
        """Modify items in a pending order to new items of the same product type."""
        if order_id not in AgentTools.db["orders"]:
            return json.dumps({"error": "Order not found"})
        order = AgentTools.db["orders"][order_id]
        if order.get("status") != "pending":
            return json.dumps(
                {"error": f"Order is '{order.get('status')}', only pending orders can be modified"}
            )
        if len(item_ids) != len(new_item_ids):
            return json.dumps({"error": "item_ids and new_item_ids must have the same length"})

        old_total = sum(item["price"] for item in order["items"])
        for old_id, new_id in zip(item_ids, new_item_ids):
            item = next((i for i in order["items"] if i["item_id"] == old_id), None)
            if not item:
                return json.dumps({"error": f"Item {old_id} not found in order"})
            product = AgentTools.db["products"].get(item["product_id"])
            if not product or new_id not in product["variants"]:
                return json.dumps({"error": f"Item {new_id} is not a valid variant of {item['name']}"})
            variant = product["variants"][new_id]
            item["item_id"] = new_id
            item["price"] = variant["price"]
            item["options"] = variant["options"]

        new_total = sum(item["price"] for item in order["items"])
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

    @staticmethod
    @tool
    def modify_pending_order_payment(order_id: str, payment_method_id: str) -> str:
        """Modify the payment method of a pending order."""
        if order_id not in AgentTools.db["orders"]:
            return json.dumps({"error": "Order not found"})
        order = AgentTools.db["orders"][order_id]
        if order.get("status") != "pending":
            return json.dumps(
                {"error": f"Order is '{order.get('status')}', only pending orders can be modified"}
            )
        order["payment_history"][0]["payment_method_id"] = payment_method_id
        return json.dumps(order)

    @staticmethod
    @tool
    def modify_pending_order_address(
        order_id: str,
        address1: str,
        address2: str,
        city: str,
        state: str,
        country: str,
        zip: str,
    ) -> str:
        """Modify the shipping address of a pending order."""
        if order_id not in AgentTools.db["orders"]:
            return json.dumps({"error": "Order not found"})
        order = AgentTools.db["orders"][order_id]
        if order.get("status") != "pending":
            return json.dumps(
                {"error": f"Order is '{order.get('status')}', only pending orders can be modified"}
            )
        order["address"] = {
            "address1": address1,
            "address2": address2,
            "city": city,
            "state": state,
            "country": country,
            "zip": zip,
        }
        return json.dumps(order)

    @staticmethod
    @tool
    def modify_user_address(
        user_id: str,
        address1: str,
        address2: str,
        city: str,
        state: str,
        country: str,
        zip: str,
    ) -> str:
        """Modify the default address of a user."""
        if user_id not in AgentTools.db["users"]:
            return json.dumps({"error": "User not found"})
        AgentTools.db["users"][user_id]["address"] = {
            "address1": address1,
            "address2": address2,
            "city": city,
            "state": state,
            "country": country,
            "zip": zip,
        }
        return json.dumps(AgentTools.db["users"][user_id])

    @staticmethod
    @tool
    def exchange_delivered_order_items(
        order_id: str, item_ids: list[str], new_item_ids: list[str], payment_method_id: str
    ) -> str:
        """Exchange items in a delivered order to new items of the same product type."""
        if order_id not in AgentTools.db["orders"]:
            return json.dumps({"error": "Order not found"})
        order = AgentTools.db["orders"][order_id]
        if order.get("status") != "delivered":
            return json.dumps(
                {"error": f"Order is '{order.get('status')}', only delivered orders can be exchanged"}
            )
        if len(item_ids) != len(new_item_ids):
            return json.dumps({"error": "item_ids and new_item_ids must have the same length"})

        for old_id, new_id in zip(item_ids, new_item_ids):
            item = next((i for i in order["items"] if i["item_id"] == old_id), None)
            if not item:
                return json.dumps({"error": f"Item {old_id} not found in order"})
            product = AgentTools.db["products"].get(item["product_id"])
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

    @staticmethod
    @tool
    def return_delivered_order_items(order_id: str, item_ids: list[str], payment_method_id: str) -> str:
        """Return some items of a delivered order."""
        if order_id not in AgentTools.db["orders"]:
            return json.dumps({"error": "Order not found"})
        order = AgentTools.db["orders"][order_id]
        if order.get("status") != "delivered":
            return json.dumps(
                {"error": f"Order is '{order.get('status')}', only delivered orders can be returned"}
            )

        refund = 0.0
        for item_id in item_ids:
            item = next((i for i in order["items"] if i["item_id"] == item_id), None)
            if not item:
                return json.dumps({"error": f"Item {item_id} not found in order"})
            refund += item["price"]

        order["status"] = "return requested"
        order["payment_history"].append(
            {
                "transaction_type": "refund",
                "amount": round(refund, 2),
                "payment_method_id": payment_method_id,
            }
        )
        return json.dumps(order)

    @staticmethod
    @tool
    def list_user_orders(
        user_id: str, from_date: str = "", to_date: str = "", limit: int = 10
    ) -> str:
        """List recent orders for a user, optionally filtered by date range."""
        if user_id not in AgentTools.db["users"]:
            return json.dumps({"error": "User not found"})

        user = AgentTools.db["users"][user_id]
        order_ids = user.get("orders", [])
        orders = []

        for order_id in order_ids:
            order = AgentTools.db["orders"].get(order_id)
            if not order:
                continue

            if from_date or to_date:
                order_date_str = order.get("delivered_at")
                if not order_date_str:
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

    @staticmethod
    @tool
    def policy_verify_return(order_id: str, item_ids: list[str], reason: str) -> str:
        """Verify return eligibility and calculate fees based on store policy."""
        valid_reasons = ("unwanted", "wrong_item", "size_issue", "defective", "other")
        if reason not in valid_reasons:
            return json.dumps(
                {"error": f"Invalid reason. Must be one of: {', '.join(valid_reasons)}"}
            )
        if order_id not in AgentTools.db["orders"]:
            return json.dumps({"error": "Order not found"})

        order = AgentTools.db["orders"][order_id]
        if order.get("status") != "delivered":
            return json.dumps(
                {
                    "error": (
                        f"Order is '{order.get('status')}', only delivered orders are eligible for return"
                    )
                }
            )

        user = AgentTools.db["users"].get(order.get("user_id", ""), {})
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
                    return json.dumps(
                        {
                            "eligible": False,
                            "reason": (
                                "Return window expired. "
                                f"{days_since} days since delivery exceeds "
                                f"{return_window_days}-day limit for {tier} tier."
                            ),
                        }
                    )
            except ValueError:
                pass

        refund_total = 0.0
        items_detail = []
        for item_id in item_ids:
            item = next((i for i in order["items"] if i["item_id"] == item_id), None)
            if not item:
                return json.dumps({"error": f"Item {item_id} not found in order"})
            items_detail.append(item)
            refund_total += item["price"]

        if reason in {"defective", "wrong_item"} or tier in {"vip", "gold"}:
            restocking_fee_pct = 0.0
        else:
            restocking_fee_pct = 0.15

        restocking_fee = round(refund_total * restocking_fee_pct, 2)
        net_refund = round(refund_total - restocking_fee, 2)
        return json.dumps(
            {
                "eligible": True,
                "order_id": order_id,
                "items": [
                    {"item_id": item["item_id"], "name": item["name"], "price": item["price"]}
                    for item in items_detail
                ],
                "reason": reason,
                "user_tier": tier,
                "return_window_days": return_window_days,
                "restocking_fee_pct": restocking_fee_pct,
                "restocking_fee": restocking_fee,
                "refund_subtotal": round(refund_total, 2),
                "net_refund": net_refund,
            }
        )

    @staticmethod
    @tool
    def transfer_to_human_agents(summary: str) -> str:
        """Transfer the user to a human agent, with a summary of the user's issue."""
        return json.dumps(
            {
                "message": "Transfer initiated. A human agent will follow up shortly.",
                "summary": summary,
            }
        )

    @classmethod
    def all_tools(cls) -> list:
        return [
            cls.calculate,
            cls.find_user_id_by_email,
            cls.find_user_id_by_name_zip,
            cls.list_all_product_types,
            cls.get_product_details,
            cls.get_user_details,
            cls.get_order_details,
            cls.cancel_pending_order,
            cls.modify_pending_order_items,
            cls.modify_pending_order_payment,
            cls.modify_pending_order_address,
            cls.modify_user_address,
            cls.exchange_delivered_order_items,
            cls.return_delivered_order_items,
            cls.list_user_orders,
            cls.policy_verify_return,
            cls.transfer_to_human_agents,
        ]
