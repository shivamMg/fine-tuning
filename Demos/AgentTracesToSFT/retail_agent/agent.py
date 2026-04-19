import asyncio
import os

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import ToolNode
from langchain_core.messages import HumanMessage, AIMessage

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    TextResponse,
)
from azure.ai.agentserver.responses.models import (
    MessageContentInputTextContent,
    MessageContentOutputTextContent,
)
from agent_tools import AgentTools


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

    def chatbot(state: MessagesState):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + state["messages"]
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
    for item in history:
        if hasattr(item, "content") and item.content:
            for content in item.content:
                if isinstance(content, MessageContentOutputTextContent) and content.text:
                    messages.append(AIMessage(content=content.text))
                elif isinstance(content, MessageContentInputTextContent) and content.text:
                    messages.append(HumanMessage(content=content.text))
    return messages


credential = DefaultAzureCredential()
token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
graph = build_graph(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    azure_deployment=AZURE_AI_MODEL_DEPLOYMENT_NAME,
    token_provider=token_provider,
    tools=AgentTools.all_tools(),
)
app = ResponsesAgentServerHost(options=ResponsesServerOptions(default_fetch_history_count=20))


@app.create_handler
async def handle_create(
    request: CreateResponse,
    context: ResponseContext,
    cancellation_signal: asyncio.Event,
):
    history = await context.get_history()
    current_input = await context.get_input_text()

    lc_messages = history_to_langchain_messages(history)
    lc_messages.append(HumanMessage(content=current_input))

    result = await asyncio.get_event_loop().run_in_executor(
        None, lambda: graph.invoke({"messages": lc_messages})
    )
    final_text = result["messages"][-1].content

    return TextResponse(context, request, text=final_text)


if __name__ == "__main__":
    app.run()
