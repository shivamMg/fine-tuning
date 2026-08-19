import asyncio
import json
import logging
import os

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponseEventStream,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
)
from azure.ai.agentserver.responses.models import (
    FunctionCallOutputItemParam,
    FunctionToolCallOutputResource,
    ItemFunctionToolCall,
    MessageContentInputTextContent,
    MessageContentOutputTextContent,
    OutputItemFunctionToolCall,
)
from azure.ai.agentserver.responses.store._foundry_errors import FoundryResourceNotFoundError
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import AzureChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from microsoft.opentelemetry import use_microsoft_opentelemetry

from tools import AgentTools


logger = logging.getLogger(__name__)

# Microsoft OpenTelemetry's LangChain instrumentation reads these during initialization.
# SPAN_ONLY records full GenAI messages on the chat spans but does not additionally duplicate them as log events.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "span_only")

AZURE_OPENAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
AZURE_AI_MODEL_DEPLOYMENT_NAME = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]

SYSTEM_PROMPT = (
    "You are a retail customer service assistant. "
    "Help customers with orders, returns, shipping, and product inquiries. "
    "Use the available tools to look up and manage order information."
)


def build_graph(azure_endpoint, azure_deployment, token_provider, tools) -> StateGraph:
    llm = AzureChatOpenAI(
        azure_endpoint=azure_endpoint,
        azure_deployment=azure_deployment,
        api_version="2025-03-01-preview",
        azure_ad_token_provider=token_provider,
    )
    llm_with_tools = llm.bind_tools(tools)

    with open("agent_policy.md") as f:
        agent_policy = f.read()
    system_message = SYSTEM_PROMPT + "\n\n" + agent_policy

    def chatbot(state: MessagesState):
        messages = [{"role": "system", "content": system_message}] + state["messages"]
        return {"messages": [llm_with_tools.invoke(messages)]}

    def route_tools(state: MessagesState):
        last = state["messages"][-1]
        if hasattr(last, "tool_calls") and last.tool_calls:
            return "tools"
        return END

    graph_ = StateGraph(MessagesState)
    graph_.add_node("chatbot", chatbot)
    graph_.add_node("tools", ToolNode(tools=tools))
    graph_.add_edge(START, "chatbot")
    graph_.add_conditional_edges("chatbot", route_tools, {
                                "tools": "tools", END: END})
    graph_.add_edge("tools", "chatbot")
    return graph_.compile()


def history_to_langchain_messages(history: list) -> list:
    """Convert responses-protocol history items to LangChain messages."""
    messages = []
    pending_tool_calls = []

    def flush_tool_calls():
        if pending_tool_calls:
            messages.append(AIMessage(content="", tool_calls=pending_tool_calls.copy()))
            pending_tool_calls.clear()

    for item in history:
        if isinstance(item, (ItemFunctionToolCall, OutputItemFunctionToolCall)):
            try:
                arguments = json.loads(item.arguments)
            except (TypeError, json.JSONDecodeError):
                logger.warning("Ignoring malformed JSON arguments for tool call %s", item.call_id)
                arguments = {"raw_arguments": item.arguments}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            pending_tool_calls.append({
                "id": item.call_id,
                "name": item.name,
                "args": arguments,
                "type": "tool_call",
            })
            continue

        flush_tool_calls()

        if isinstance(item, (FunctionCallOutputItemParam, FunctionToolCallOutputResource)):
            output = item.output if isinstance(item.output, str) else json.dumps(item.output, ensure_ascii=False)
            messages.append(ToolMessage(content=output, tool_call_id=item.call_id))
            continue

        if hasattr(item, "content") and item.content:
            for content in item.content:
                if isinstance(content, MessageContentOutputTextContent) and content.text:
                    messages.append(AIMessage(content=content.text))
                elif isinstance(content, MessageContentInputTextContent) and content.text:
                    messages.append(HumanMessage(content=content.text))
    flush_tool_calls()
    return messages


def content_to_text(content) -> str:
    """Convert LangChain string or structured message content to protocol text."""
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


use_microsoft_opentelemetry(
    # Foundry injects APPLICATIONINSIGHTS_CONNECTION_STRING for hosted agents.
    # Explicitly enabling the exporter prevents the SDK from falling back to the
    # console exporter when the agent starts.
    enable_azure_monitor=True,
    sampling_ratio=1.0,
    # Message bodies, tool arguments/results, and bound tool schemas are needed
    # for the training-trace corpus. Treat this setting as sensitive telemetry.
    enable_sensitive_data=True,
    # Keep the LangChain spans that aggregate every model turn into the agent
    # trace, including the full input/output message history and tool schemas.
    instrumentation_options={
        "langchain": {
            "enabled": True,
            "agent_name": os.getenv("FOUNDRY_AGENT_NAME", "retail-agent-langgraph"),
            "agent_version": os.getenv("FOUNDRY_AGENT_VERSION"),
            "agent_id": os.getenv("FOUNDRY_AGENT_ID"),
        }
    },
)
credential = DefaultAzureCredential()
token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
graph = build_graph(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    azure_deployment=AZURE_AI_MODEL_DEPLOYMENT_NAME,
    token_provider=token_provider,
    tools=AgentTools.all_tools(),
)
app = ResponsesAgentServerHost(options=ResponsesServerOptions(default_fetch_history_count=100))


@app.response_handler
async def handle_create(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    current_input = await context.get_input_text()
    try:
        history = await context.get_history()
    except FoundryResourceNotFoundError:
        history = []

    lc_messages = history_to_langchain_messages(history)
    lc_messages.append(HumanMessage(content=current_input))

    stream = ResponseEventStream(response_id=context.response_id, request=request)
    yield stream.emit_created()
    yield stream.emit_in_progress()

    result = await graph.ainvoke({"messages": lc_messages})
    if cancellation_signal.is_set():
        raise asyncio.CancelledError("Request was cancelled")

    # MessagesState appends every assistant tool call, tool result, and final
    # assistant message to the input history. Emit only those new messages.
    new_messages = result["messages"][len(lc_messages):]
    for message in new_messages:
        if isinstance(message, AIMessage):
            for tool_call in message.tool_calls:
                arguments = json.dumps(
                    tool_call.get("args", {}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for response_event in stream.output_item_function_call(
                    name=tool_call["name"],
                    call_id=tool_call["id"],
                    arguments=arguments,
                ):
                    yield response_event

            if message.content:
                for response_event in stream.output_item_message(
                    content_to_text(message.content)
                ):
                    yield response_event
        elif isinstance(message, ToolMessage):
            for response_event in stream.output_item_function_call_output(
                call_id=message.tool_call_id,
                output=content_to_text(message.content),
            ):
                yield response_event

    yield stream.emit_completed()


if __name__ == "__main__":
    app.run()
