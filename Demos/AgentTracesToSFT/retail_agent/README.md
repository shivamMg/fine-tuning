# LangGraph Multi-turn Chat Agent (Responses Protocol)

A multi-turn conversational agent built with [LangGraph](https://langchain-ai.github.io/langgraph/)
and Azure OpenAI, hosted via the **responses** protocol.

## What it demonstrates

- **LangGraph agent graph** with conditional tool-calling routing
- **Two built-in tools**: `get_current_time` and `calculator`
- **Server-side conversation state** via `previous_response_id` — no application-side session storage
- **Streaming** output over the responses protocol
- **Azure OpenAI** with `DefaultAzureCredential` authentication

## Architecture

```
┌───────┐    ┌─────────┐    ┌───────┐
│ START │───▶│ chatbot  │───▶│  END  │
└───────┘    └────┬─────┘    └───────┘
                  │ tool_calls?
                  ▼
             ┌─────────┐
             │  tools   │
             └────┬─────┘
                  │
                  └──▶ chatbot (loop)
```

## Key difference from invocations protocol

This sample uses the **responses** protocol where conversation history is
managed server-side. The platform stores conversation state and resolves it
via `previous_response_id` — no need for an in-memory session store.

## Prerequisites

- Python 3.12+
- Azure OpenAI resource with a deployed model (e.g., `gpt-4o-mini`)
- Azure CLI login (`az login`) or other `DefaultAzureCredential` source

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | Yes | — | Azure OpenAI endpoint URL |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | No | `gpt-4o-mini` | Model deployment name |

## Running locally

```bash
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
pip install -r requirements.txt
python main.py
```

## Testing with curl

```bash
# Turn 1 — ask for the time (triggers tool call)
curl -N -X POST http://localhost:8088/responses \
    -H "Content-Type: application/json" \
    -d '{"model": "chat", "input": "What time is it right now?", "stream": true}'

# Turn 2 — chain via previous_response_id
curl -N -X POST http://localhost:8088/responses \
    -H "Content-Type: application/json" \
    -d '{"model": "chat", "input": "What is 42 * 17?", "previous_response_id": "<ID>", "stream": true}'

# Turn 3 — context recall
curl -N -X POST http://localhost:8088/responses \
    -H "Content-Type: application/json" \
    -d '{"model": "chat", "input": "Add 100 to that result", "previous_response_id": "<ID>", "stream": true}'
```

## Deploying to Azure AI Agent Hosting

```bash
azd ai agent init -m agent.manifest.yaml
azd up
```
