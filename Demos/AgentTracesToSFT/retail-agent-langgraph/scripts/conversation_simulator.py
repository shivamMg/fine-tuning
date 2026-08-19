"""Simulate diverse multi-turn conversations against the deployed retail hosted agent.

This script talks to the hosted agent through the public Foundry OpenAI-compatible
Conversations + Responses HTTP API (the same API used by ``azd ai agent invoke``),
NOT through the azd CLI. For each simulated conversation it:

  1. Creates a durable Foundry ``conversation_id`` via
     ``POST {project}/agents/{name}/endpoint/protocols/openai/conversations``
     (an optional ``--agent-version`` is passed as an ``agent-version`` query param).
    2. Drives 2-4 user turns, sending each turn to the sibling ``/responses`` endpoint
     with ``{"input": ..., "conversation": {"id": <conversation_id>}}`` so the agent
     keeps server-side memory across the turns.

A separate Foundry model deployment (the "simulator") role-plays the customer and
produces varied, realistic phrasing for each turn. Fixture scenarios are grounded in
the real users/orders/products in ``db.json`` so the retail agent's tools can succeed.

WARNING: scenarios may exercise state-changing agent tools (cancel order, modify
address/payment/items, return, exchange). This mutates the deployed agent's local
database state.

Authentication uses ``DefaultAzureCredential`` (run ``azd auth login`` / ``az login``).
Required RBAC: ``Foundry User`` on the Foundry account for both the agent and the
simulator deployment.

--------------------------------------------------------------------------------
Setup (reuse the existing virtual environment in Demos/AgentTracesToSFT/.venv):

    # from Demos/AgentTracesToSFT
    .venv\\Scripts\\Activate.ps1
    pip install azure-identity azure-ai-projects requests

Run (from retail-agent-langgraph):

    ..\\.venv\\Scripts\\python.exe scripts/conversation_simulator.py \\
        --num-conversations 10 \\
        --project-endpoint "https://<acct>.services.ai.azure.com/api/projects/<project>" \\
        --agent-name "retail-agent-langgraph" \\
        --simulator-project-endpoint "https://<acct>.services.ai.azure.com/api/projects/<project>" \\
        --simulator-deployment "gpt-4.1"

The emitted ``conversation_id`` values (printed in each conversation header and in the
final summary) can be passed to ``verify_app_insights_conversations.py`` to confirm the
traces landed in Application Insights.

Each conversation (scenario, conversation_id, response-id chain, and the full
user/agent transcript) is also written as one JSON object per line to a JSONL file
(``--output``; defaults to ``scripts/conversations_<timestamp>.jsonl``). An explicitly
provided ``--output`` path is appended to, allowing multiple simulation batches in the
same training corpus.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests
from azure.core.credentials import AccessToken
from azure.identity import DefaultAzureCredential

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

AGENT_TOKEN_SCOPE = "https://ai.azure.com/.default"
API_VERSION = "v1"
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db.json")

# Reasons accepted by the agent's return-policy tool.
RETURN_REASONS = ("unwanted", "wrong_item", "size_issue", "defective", "other")
# Human-friendly phrasing for those reasons (for the simulated customer).
RETURN_REASON_PHRASES = {
    "unwanted": "I just don't want it anymore",
    "wrong_item": "it's the wrong item",
    "size_issue": "the size doesn't fit",
    "defective": "it arrived defective",
    "other": "personal reasons",
}
TRANSFER_INITIATED_RESPONSE = "transfer initiated. a human agent will follow up shortly."


# --------------------------------------------------------------------------- #
# Agent HTTP client (public Foundry Responses/Conversations API)
# --------------------------------------------------------------------------- #


class AgentClient:
    """Minimal client for the hosted agent's OpenAI-compatible HTTP API."""

    def __init__(
        self,
        project_endpoint: str,
        agent_name: str,
        agent_version: Optional[str],
        credential: DefaultAzureCredential,
        timeout: float,
    ) -> None:
        base = project_endpoint.rstrip("/")
        self._openai_base = f"{base}/agents/{agent_name}/endpoint/protocols/openai"
        # The agent version is selected via an `agent-version` query parameter on the
        # protocol endpoints (NOT a `/versions/{v}` path segment, which 404s).
        self._version_query = f"&agent-version={agent_version}" if agent_version else ""
        self._credential = credential
        self._timeout = timeout
        self._token: Optional[AccessToken] = None
        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        now = time.time()
        if self._token is None or self._token.expires_on - now < 120:
            self._token = self._credential.get_token(AGENT_TOKEN_SCOPE)
        return {
            "Authorization": f"Bearer {self._token.token}",
            "Content-Type": "application/json",
        }

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(4):
            try:
                resp = self._session.post(
                    url, headers=self._headers(), json=body, timeout=self._timeout
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"{resp.status_code}: {resp.text[:300]}")
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:  # noqa: BLE001 - retry transient failures
                last_error = exc
                if attempt < 3:
                    time.sleep(2 ** attempt)
        assert last_error is not None
        raise last_error

    def create_conversation(self) -> str:
        url = f"{self._openai_base}/conversations?api-version={API_VERSION}{self._version_query}"
        data = self._post(url, {})
        conversation_id = data.get("id")
        if not conversation_id:
            raise RuntimeError(f"Conversation create returned no id: {data}")
        return conversation_id

    def send_turn(self, conversation_id: str, user_text: str) -> dict[str, Any]:
        url = f"{self._openai_base}/responses?api-version={API_VERSION}{self._version_query}"
        body = {"input": user_text, "conversation": {"id": conversation_id}}
        data = self._post(url, body)
        return {
            "response_id": data.get("id"),
            "text": _extract_output_text(data),
            "status": data.get("status"),
        }


def _extract_output_text(data: dict[str, Any]) -> str:
    """Pull assistant text out of a Responses API payload defensively."""
    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    parts: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") in (
                "output_text",
                "text",
            ):
                value = content.get("text")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
    return "\n".join(parts).strip()


def is_terminal_agent_response(agent_text: str) -> bool:
    """Return whether the agent has handed the conversation to a human."""
    return TRANSFER_INITIATED_RESPONSE in " ".join(agent_text.lower().split())


# --------------------------------------------------------------------------- #
# Simulator model client (role-plays the customer)
# --------------------------------------------------------------------------- #


class SimulatorModel:
    """Wraps a Foundry model deployment used to generate customer turns."""

    def __init__(
        self,
        project_endpoint: str,
        deployment: str,
        credential: DefaultAzureCredential,
    ) -> None:
        from azure.ai.projects import AIProjectClient

        self._deployment = deployment
        project = AIProjectClient(endpoint=project_endpoint, credential=credential)
        self._client = project.get_openai_client()

    def next_message(
        self,
        scenario_brief: str,
        transcript: list[dict[str, str]],
        turn_index: int,
        total_turns: int,
    ) -> Optional[str]:
        """Return the customer's next message, or None on failure."""
        system = (
            "You are role-playing a RETAIL CUSTOMER talking to a customer-service "
            "agent. Stay fully in character as the customer. Output ONLY the "
            "customer's next chat message as plain text - no quotes, no narration, "
            "no role labels. Keep it natural and concise (1-2 sentences). Use the "
            "exact identifiers given in the brief when relevant. Follow the scenario "
            "goal; do not invent order details, phone numbers, dates, payment methods, "
            "or requests such as emailing, calling, monitoring, carrier investigations, "
            "or supervisor escalation unless the scenario explicitly requires them. "
            "Keep every turn useful: do not send repeated thanks, acknowledgements, "
            "goodbyes, or paraphrases of an earlier message. Before the final turn, "
            "continue with a natural follow-up about the same order, product, return, "
            "or account, grounded only in the scenario brief and facts the agent has "
            "already stated. You may ask for clarification, status, timing, totals, "
            "available options, or confirmation of an action, but do not invent facts. "
            "Do not thank the agent or end the conversation before the final turn.\n\n"
            f"This is customer turn {turn_index} of {total_turns}. "
            + (
                "This is the FINAL turn, so wrap up politely (thank the agent, "
                "confirm, or say goodbye)."
                if turn_index >= total_turns
                else "There are more turns to come. Ask one useful, non-repetitive "
                "follow-up and keep the conversation moving toward the scenario goal."
            )
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"YOUR SCENARIO BRIEF:\n{scenario_brief}"},
        ]
        # Give the model the conversation so far. From the customer's viewpoint the
        # agent's messages are the "other party", so map agent->user, customer->assistant.
        for turn in transcript:
            if turn["role"] == "agent":
                messages.append({"role": "user", "content": f"[AGENT]: {turn['text']}"})
            else:
                messages.append({"role": "assistant", "content": turn["text"]})

        # Newer models (e.g. gpt-5*) require `max_completion_tokens` and reject
        # `max_tokens`; older ones only accept `max_tokens`. Try the modern
        # parameter first and fall back on a parameter error.
        for token_param in ("max_completion_tokens", "max_tokens"):
            try:
                completion = self._client.chat.completions.create(
                    model=self._deployment,
                    messages=messages,
                    **{token_param: 2000},
                )
                content = completion.choices[0].message.content
                if content and content.strip():
                    return content.strip()
                print(
                    "    [warn] simulator model returned empty content "
                    f"(finish={completion.choices[0].finish_reason}); using fallback.",
                    file=sys.stderr,
                )
                return None
            except Exception as exc:  # noqa: BLE001 - retry with alt param, else give up
                message = str(exc)
                if token_param == "max_completion_tokens" and "max_tokens" in message:
                    continue
                print(f"    [warn] simulator model error: {exc}", file=sys.stderr)
                return None
        return None


# --------------------------------------------------------------------------- #
# Fixture-backed scenarios
# --------------------------------------------------------------------------- #


@dataclass
class Scenario:
    name: str
    brief: str
    opening: str
    # Authored fallback follow-ups used when the simulator model is unavailable.
    fallbacks: list[str] = field(default_factory=list)


class ScenarioFactory:
    """Builds grounded scenarios from db.json fixtures."""

    def __init__(self, db: dict[str, Any], rng: random.Random) -> None:
        self.db = db
        self.rng = rng
        users = db["users"]
        orders = db["orders"]

        self._users_with_email = [u for u in users.values() if u.get("email")]
        self._pending_orders = [o for o in orders.values() if o.get("status") == "pending"]
        self._delivered_orders = [o for o in orders.values() if o.get("status") == "delivered"]
        self._return_eligible_orders = self._find_return_eligible_orders()
        self._products = list(db["products"].values())

        self.builders: list[Callable[[], Optional[Scenario]]] = [
            self._list_orders_by_email,
            self._identify_by_name_zip,
            self._order_status_lookup,
            self._product_inquiry,
            self._cancel_pending_order,
            self._modify_pending_address,
            self._modify_pending_payment,
            self._modify_pending_items,
            self._return_delivered_items,
            self._exchange_delivered_items,
            self._refund_math,
            self._escalate_to_human,
        ]

    # -- helpers -------------------------------------------------------------- #

    def _user_of(self, order: dict[str, Any]) -> Optional[dict[str, Any]]:
        return self.db["users"].get(order.get("user_id", ""))

    def _payment_id(self, user: dict[str, Any]) -> Optional[str]:
        methods = user.get("payment_methods") or {}
        keys = list(methods.keys())
        return self.rng.choice(keys) if keys else None

    def _find_return_eligible_orders(self) -> list[dict[str, Any]]:
        """Mirror the agent's shifted delivery-date and tier return-window policy."""
        delivered = [o for o in self._delivered_orders if o.get("delivered_at") and o.get("items")]
        if not delivered:
            return []
        newest_delivery = max(datetime.fromisoformat(o["delivered_at"]) for o in delivered)
        eligible: list[dict[str, Any]] = []
        for order in delivered:
            user = self._user_of(order)
            if not user:
                continue
            window = {"standard": 7, "gold": 30, "vip": 60}.get(user.get("tier"), 7)
            days_since = (newest_delivery - datetime.fromisoformat(order["delivered_at"])).days
            if days_since <= window:
                eligible.append(order)
        return eligible

    # -- scenario builders ---------------------------------------------------- #

    def _list_orders_by_email(self) -> Optional[Scenario]:
        user = self.rng.choice(self._users_with_email)
        email = user["email"]
        return Scenario(
            name="list_orders_by_email",
            brief=(
                f"You are {user['name']['first_name']} {user['name']['last_name']}. "
                f"Your account email is {email}. You want to see a list of your recent "
                "orders, then ask one quick follow-up about the most recent order's "
                "status or items. Do not start a return, exchange, or ask for notifications."
            ),
            opening=f"Hi, can you pull up my recent orders? My email is {email}.",
            fallbacks=[
                "Thanks! Can you tell me more about the most recent one?",
                "Got it, that's all I needed. Thank you!",
            ],
        )

    def _identify_by_name_zip(self) -> Optional[Scenario]:
        user = self.rng.choice(self._users_with_email)
        first = user["name"]["first_name"]
        last = user["name"]["last_name"]
        zip_code = user["address"]["zip"]
        return Scenario(
            name="identify_by_name_zip",
            brief=(
                f"You are {first} {last}, zip code {zip_code}. You do not want to give "
                "your email. You want to verify your identity by name and zip, then ask "
                "to review your orders."
            ),
            opening=f"Hello, I'd like to check my orders. My name is {first} {last} and my zip is {zip_code}.",
            fallbacks=[
                "Great, can you show me what I've ordered?",
                "Perfect, thanks so much for the help!",
            ],
        )

    def _order_status_lookup(self) -> Optional[Scenario]:
        pool = self._pending_orders + self._delivered_orders
        order = self.rng.choice(pool)
        user = self._user_of(order)
        if not user:
            return None
        return Scenario(
            name="order_status_lookup",
            brief=(
                f"You are {user['name']['first_name']}, email {user['email']}. You want "
                f"the status and tracking of order {order['order_id']}. Ask only for the "
                "details the record can contain, then thank the agent and end."
            ),
            opening=(
                f"Hi, what's the status of my order {order['order_id']}? "
                f"My email is {user['email']}."
            ),
            fallbacks=[
                "Do you have a tracking number for it?",
                "Okay, thank you for checking!",
            ],
        )

    def _product_inquiry(self) -> Optional[Scenario]:
        product = self.rng.choice(self._products)
        user = self.rng.choice(self._users_with_email)
        return Scenario(
            name="product_inquiry",
            brief=(
                f"You are {user['name']['first_name']}, email {user['email']}. You are "
                f"interested in the '{product['name']}'. First identify yourself, then "
                "ask for the product variants and availability. Do not ask for store "
                "inventory, email alerts, or anything outside product details."
            ),
            opening=(
                f"Hi, I'm interested in the {product['name']}. My email is {user['email']}. "
                "What variants do you have available?"
            ),
            fallbacks=[
                "Which of those variants are currently in stock?",
                "Thanks, that answers my question!",
            ],
        )

    def _cancel_pending_order(self) -> Optional[Scenario]:
        if not self._pending_orders:
            return None
        order = self.rng.choice(self._pending_orders)
        user = self._user_of(order)
        if not user:
            return None
        reason = self.rng.choice(["no longer needed", "ordered by mistake"])
        return Scenario(
            name="cancel_pending_order",
            brief=(
                f"You are {user['name']['first_name']}, email {user['email']}. You want "
                f"to CANCEL your pending order {order['order_id']} because it was "
                f"'{reason}'. State the required reason in the first message and confirm "
                "the cancellation only if the agent asks. End after confirmation."
            ),
            opening=(
                f"Hi, I need to cancel my order {order['order_id']}. "
                f"My email is {user['email']}; it was {reason}."
            ),
            fallbacks=[
                f"It was {reason}.",
                "Yes, please go ahead and cancel it.",
            ],
        )

    def _modify_pending_address(self) -> Optional[Scenario]:
        if not self._pending_orders:
            return None
        order = self.rng.choice(self._pending_orders)
        user = self._user_of(order)
        if not user:
            return None
        return Scenario(
            name="modify_pending_address",
            brief=(
                f"You are {user['name']['first_name']}, email {user['email']}. You moved "
                f"and want to change the shipping address on pending order {order['order_id']} "
                "to: 742 Evergreen Terrace, Apt 5, Portland, OR, USA, 97205. Change only "
                "this order, provide the exact address if requested, then confirm and end."
            ),
            opening=(
                f"Hi, I need to update the shipping address on order {order['order_id']}. "
                f"My email is {user['email']}."
            ),
            fallbacks=[
                "New address: 742 Evergreen Terrace, Apt 5, Portland, OR, USA, 97205.",
                "Yes, that's correct, please update it.",
            ],
        )

    def _modify_pending_payment(self) -> Optional[Scenario]:
        candidates = [
            o
            for o in self._pending_orders
            if self._user_of(o) and len(self._user_of(o).get("payment_methods", {})) >= 2
        ]
        if not candidates:
            return None
        order = self.rng.choice(candidates)
        user = self._user_of(order)
        payment_id = self._payment_id(user)
        return Scenario(
            name="modify_pending_payment",
            brief=(
                f"You are {user['name']['first_name']}, email {user['email']}. You want to "
                f"switch the payment method on pending order {order['order_id']} to your "
                f"other method on file ({payment_id}). Provide the exact payment ID, confirm "
                "the completed change, and end without asking for email confirmation."
            ),
            opening=(
                f"Hi, can I change the payment method on order {order['order_id']}? "
                f"My email is {user['email']}."
            ),
            fallbacks=[
                f"Please use {payment_id}.",
                "Yes, go ahead, thank you!",
            ],
        )

    def _modify_pending_items(self) -> Optional[Scenario]:
        for order in self.rng.sample(self._pending_orders, min(len(self._pending_orders), 20)):
            user = self._user_of(order)
            if not user:
                continue
            for item in order.get("items", []):
                product = self.db["products"].get(item.get("product_id"))
                if not product:
                    continue
                alt = [
                    v
                    for iid, v in product["variants"].items()
                    if iid != item["item_id"] and v.get("available")
                ]
                if alt:
                    new_variant = self.rng.choice(alt)
                    opts = ", ".join(f"{k}: {v}" for k, v in new_variant["options"].items())
                    return Scenario(
                        name="modify_pending_items",
                        brief=(
                            f"You are {user['name']['first_name']}, email {user['email']}. "
                            f"On pending order {order['order_id']} you want to swap your "
                            f"'{item['name']}' (item {item['item_id']}) for the variant "
                            f"{new_variant['item_id']} ({opts}). Confirm when asked, accept "
                            "any shown price difference, then end."
                        ),
                        opening=(
                            f"Hi, I'd like to change an item on order {order['order_id']}. "
                            f"My email is {user['email']}."
                        ),
                        fallbacks=[
                            f"I want to swap the {item['name']} for variant {new_variant['item_id']} ({opts}).",
                            "Yes please, go ahead with the change.",
                        ],
                    )
        return None

    def _return_delivered_items(self) -> Optional[Scenario]:
        if not self._return_eligible_orders:
            return None
        order = self.rng.choice(self._return_eligible_orders)
        user = self._user_of(order)
        if not user or not order.get("items"):
            return None
        item = self.rng.choice(order["items"])
        reason = self.rng.choice(RETURN_REASONS)
        phrase = RETURN_REASON_PHRASES[reason]
        return Scenario(
            name="return_delivered_items",
            brief=(
                f"You are {user['name']['first_name']}, email {user['email']}. You want to "
                f"RETURN the '{item['name']}' (item {item['item_id']}) from delivered order "
                f"{order['order_id']} because {phrase}. This fixture was selected within the "
                "customer's return window. Ask for eligibility/refund details, then confirm "
                "the return once. Do not request exceptions or unrelated follow-up work."
            ),
            opening=(
                f"Hi, I'd like to return an item from order {order['order_id']}. "
                f"My email is {user['email']}."
            ),
            fallbacks=[
                f"It's the {item['name']}, and {phrase}.",
                "Okay, that refund works for me. Please process the return.",
            ],
        )

    def _exchange_delivered_items(self) -> Optional[Scenario]:
        for order in self.rng.sample(
            self._delivered_orders, min(len(self._delivered_orders), 20)
        ):
            user = self._user_of(order)
            if not user:
                continue
            for item in order.get("items", []):
                product = self.db["products"].get(item.get("product_id"))
                if not product:
                    continue
                alt = [
                    v
                    for iid, v in product["variants"].items()
                    if iid != item["item_id"] and v.get("available")
                ]
                if alt:
                    new_variant = self.rng.choice(alt)
                    opts = ", ".join(f"{k}: {v}" for k, v in new_variant["options"].items())
                    return Scenario(
                        name="exchange_delivered_items",
                        brief=(
                            f"You are {user['name']['first_name']}, email {user['email']}. "
                            f"You received order {order['order_id']} and want to EXCHANGE the "
                            f"'{item['name']}' (item {item['item_id']}) for variant "
                            f"{new_variant['item_id']} ({opts}). Use the original payment "
                            "method for any difference, confirm the exchange once, then end "
                            "without requesting shipment notifications."
                        ),
                        opening=(
                            f"Hi, I'd like to exchange an item from order {order['order_id']}. "
                            f"My email is {user['email']}."
                        ),
                        fallbacks=[
                            f"I want to exchange the {item['name']} for variant {new_variant['item_id']} ({opts}).",
                            "Yes, please process the exchange.",
                        ],
                    )
        return None

    def _refund_math(self) -> Optional[Scenario]:
        if not self._delivered_orders:
            return None
        order = self.rng.choice([o for o in self._delivered_orders if o.get("items")])
        user = self._user_of(order)
        if not user:
            return None
        return Scenario(
            name="refund_math",
            brief=(
                f"You are {user['name']['first_name']}, email {user['email']}. You are "
                f"curious about order {order['order_id']}: ask the agent for the total of "
                "all the items on it, ask it to double-check the math, then thank it."
            ),
            opening=(
                f"Hi, can you tell me the total cost of everything in order {order['order_id']}? "
                f"My email is {user['email']}."
            ),
            fallbacks=[
                "Can you add those up and confirm the exact total?",
                "Thanks for confirming!",
            ],
        )

    def _escalate_to_human(self) -> Optional[Scenario]:
        user = self.rng.choice(self._users_with_email)
        return Scenario(
            name="escalate_to_human",
            brief=(
                f"You are {user['name']['first_name']}, email {user['email']}. You are "
                "frustrated about a billing discrepancy that the agent can't resolve, and "
                "you need a human-agent transfer. Identify yourself, request the transfer, "
                "acknowledge it once the transfer is initiated, then end. Do not invent a "
                "phone number, case ID, callback promise, or a demand for staff details."
            ),
            opening="I've been overcharged and I'm really frustrated. I want to talk to a human.",
            fallbacks=[
                f"My email is {user['email']}. This still isn't resolved - please escalate.",
                "Yes, please transfer me to a human agent.",
            ],
        )

    def build_batch(self, count: int) -> list[Scenario]:
        """Return `count` scenarios, cycling builders for diversity."""
        scenarios: list[Scenario] = []
        builders = list(self.builders)
        self.rng.shuffle(builders)
        idx = 0
        attempts = 0
        while len(scenarios) < count and attempts < count * 5:
            builder = builders[idx % len(builders)]
            idx += 1
            attempts += 1
            try:
                scenario = builder()
            except Exception:  # noqa: BLE001 - skip a scenario that can't be built
                scenario = None
            if scenario:
                scenarios.append(scenario)
        return scenarios


# --------------------------------------------------------------------------- #
# Conversation runner
# --------------------------------------------------------------------------- #


def run_conversation(
    agent: AgentClient,
    simulator: SimulatorModel,
    scenario: Scenario,
    num_turns: int,
    index: int,
    total: int,
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()
    print(f"\n{'=' * 78}")
    print(f"CONVERSATION {index}/{total}  |  scenario={scenario.name}  |  turns={num_turns}")

    conversation_id = agent.create_conversation()
    print(f"conversation_id: {conversation_id}")
    print(f"run_id: {run_id}")
    print("-" * 78)

    transcript: list[dict[str, str]] = []
    response_ids: list[str] = []
    error: Optional[str] = None

    for turn in range(1, num_turns + 1):
        if turn == 1:
            user_text = scenario.opening
        else:
            generated = simulator.next_message(scenario.brief, transcript, turn, num_turns)
            if generated:
                user_text = generated
            else:
                fb_index = turn - 2
                user_text = (
                    scenario.fallbacks[fb_index]
                    if fb_index < len(scenario.fallbacks)
                    else "Okay, thank you for your help!"
                )

        print(f"[turn {turn}] USER:  {user_text}")
        transcript.append({"role": "user", "text": user_text})

        try:
            result = agent.send_turn(conversation_id, user_text)
        except Exception as exc:  # noqa: BLE001 - record and stop this conversation
            error = f"turn {turn} failed: {exc}"
            print(f"    [error] {error}", file=sys.stderr)
            break

        agent_text = result["text"] or "(no text output)"
        if result["response_id"]:
            response_ids.append(result["response_id"])
        print(f"[turn {turn}] AGENT: {agent_text}")
        transcript.append({"role": "agent", "text": agent_text})
        if is_terminal_agent_response(agent_text):
            print("    [terminal] human transfer initiated; ending conversation.")
            break

    ended = datetime.now(timezone.utc).isoformat()
    record = {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "scenario": scenario.name,
        "num_turns_planned": num_turns,
        "num_user_turns": sum(1 for t in transcript if t["role"] == "user"),
        "response_ids": response_ids,
        "transcript": transcript,
        "started_at": started,
        "ended_at": ended,
        "error": error,
    }
    print("-" * 78)
    print("RESULT: " + json.dumps({k: record[k] for k in (
        "conversation_id", "scenario", "num_user_turns", "error")}))
    return record


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_turn_plan(
    count: int, min_turns: int, max_turns: int, rng: random.Random
) -> list[int]:
    """Build a shuffled, balanced turn plan spanning the configured range.

    Values are sampled without replacement within each cycle, so batches do not
    collapse to one fixed conversation length. If the batch is at least as large
    as the range, every configured turn count appears before any count repeats.
    """
    choices = list(range(min_turns, max_turns + 1))
    plan: list[int] = []
    while len(plan) < count:
        cycle = choices.copy()
        rng.shuffle(cycle)
        plan.extend(cycle)
    return plan[:count]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate multi-turn conversations against the deployed retail hosted agent.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--num-conversations",
        type=int,
        required=True,
        help="Number of independent new conversations to create.",
    )
    parser.add_argument(
        "--project-endpoint",
        required=True,
        help="Foundry project endpoint hosting the agent, e.g. "
        "https://<acct>.services.ai.azure.com/api/projects/<project>.",
    )
    parser.add_argument("--agent-name", required=True, help="Deployed hosted agent name.")
    parser.add_argument(
        "--agent-version",
        default=None,
        help="Optional agent version. If omitted, the default (latest) endpoint is used.",
    )
    parser.add_argument(
        "--simulator-project-endpoint",
        required=True,
        help="Foundry project endpoint for the simulator model deployment.",
    )
    parser.add_argument(
        "--simulator-deployment",
        default="gpt-5.6-luna",
        help="Model deployment name used to generate customer turns.",
    )
    parser.add_argument("--min-turns", type=int, default=2, help="Minimum user turns per conversation.")
    parser.add_argument("--max-turns", type=int, default=4, help="Maximum user turns per conversation.")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-request HTTP timeout (seconds).")
    parser.add_argument(
        "--output",
        default=None,
        help="Path to write conversations as JSONL (one conversation per line). "
        "Defaults to scripts/conversations_<timestamp>.jsonl next to this script.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    # Agent/simulator replies may contain Unicode (arrows, non-breaking hyphens);
    # force UTF-8 so printing them does not crash on Windows consoles (cp1252).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best effort
            pass

    args = parse_args(argv)

    if args.num_conversations < 1:
        print("--num-conversations must be >= 1", file=sys.stderr)
        return 2
    if not (1 <= args.min_turns <= args.max_turns <= 4):
        print("Require 1 <= --min-turns <= --max-turns <= 4", file=sys.stderr)
        return 2
    if args.min_turns < 2:
        print("--min-turns must be >= 2 (minimum conversation length).", file=sys.stderr)
        return 2

    with open(DB_PATH, encoding="utf-8") as fh:
        db = json.load(fh)

    rng = random.Random(args.seed)
    credential = DefaultAzureCredential()

    agent = AgentClient(
        project_endpoint=args.project_endpoint,
        agent_name=args.agent_name,
        agent_version=args.agent_version,
        credential=credential,
        timeout=args.timeout,
    )
    simulator = SimulatorModel(
        project_endpoint=args.simulator_project_endpoint,
        deployment=args.simulator_deployment,
        credential=credential,
    )
    factory = ScenarioFactory(db, rng)
    scenarios = factory.build_batch(args.num_conversations)
    turn_plan = build_turn_plan(len(scenarios), args.min_turns, args.max_turns, rng)

    output_path = args.output or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"conversations_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
    )
    output_mode = "a" if args.output else "w"

    records: list[dict[str, Any]] = []
    with open(output_path, output_mode, encoding="utf-8") as out:
        for i, (scenario, num_turns) in enumerate(zip(scenarios, turn_plan), start=1):
            try:
                record = run_conversation(agent, simulator, scenario, num_turns, i, len(scenarios))
            except Exception as exc:  # noqa: BLE001 - keep other conversations going
                print(f"[error] conversation {i} failed to start: {exc}", file=sys.stderr)
                record = {"conversation_id": None, "scenario": scenario.name, "error": str(exc)}
            records.append(record)
            # Persist incrementally so partial runs are still captured.
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()

    succeeded = [r for r in records if r.get("conversation_id") and not r.get("error")]
    print(f"\n{'#' * 78}")
    print(f"SUMMARY: {len(succeeded)}/{len(records)} conversations completed without error.")
    print(f"Saved {len(records)} conversation(s) to: {output_path}")
    print("Conversation IDs:")
    for r in records:
        print(f"  - {r.get('conversation_id')}  [{r.get('scenario')}]"
              + (f"  ERROR: {r['error']}" if r.get("error") else ""))
    print("\nAll conversation IDs (space-separated, for the verifier):")
    print(" ".join(r["conversation_id"] for r in succeeded))

    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
