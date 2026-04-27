# LangGraph Multi-turn Chat Agent (Responses Protocol)

A multi-turn conversational agent built with [LangGraph](https://langchain-ai.github.io/langgraph/)
and Azure OpenAI, hosted via the **responses** protocol.

## What it demonstrates

- **LangGraph agent** with agentic loop for tool-calling
- **Retail tools** for order management that read and write to Db
- **Server-side conversation state** via `previous_response_id` — no application-side session storage
- **Azure OpenAI** with `DefaultAzureCredential` authentication
- **Traces** saved to App Insights connected to Foundry 

## Architecture

```
+--------------------+                   +----------------------------------+                          +----------------------+
| User or client app |                   | Retail Agent                     | -- /chat/completions --> | Azure OpenAI LLM     |
|                    |   --- POST -->    | LangGraph loop                   |                          +----------------------+
| JSON body:         |    /responses     | chatbot -> tools -> chatbot      |
| - model            |                   |                                  |
| - input            |                   | functions:                       |
| - previous_        |                   | - find_user_id_by_email()        |
|   response_id?     |                   | - list_user_orders()             |
|                    |                   | - ...                            |
+--------------------+                   +----------------------------------+
                                                  |
                                                  | traces
                                                  v
                                        +-------------------------+
                                        | Application Insights    |
                                        |                         |
                                        +-------------------------+

```

The agent host resolves conversation state from previous_response_id and returns
the final answer, or a streamed response, back to the caller.

The user calls the agent's **responses-style** endpoint, not the model endpoint directly.
The host resolves prior turns with `previous_response_id`, the LangGraph agent decides
whether to call tools, and the agent then calls Azure OpenAI on the user's behalf.

## Responses endpoint

The client sends each turn to the agent with an HTTP `POST` to:

- Local URL: `http://localhost:8088/responses`
- Hosted URL: `<your-agent-base-url>/responses`

Minimal request body:

```json
{
    "model": "chat",
    "input": "What time is it right now?",
    "stream": true
}
```

Continue an existing conversation by passing the previous response ID:

```json
{
    "model": "chat",
    "input": "Add 100 to that result",
    "previous_response_id": "resp_123",
    "stream": true
}
```

Key fields:

- `model`: logical model name exposed by the agent host. In this sample, the curl examples use `chat`.
- `input`: the current user turn.
- `previous_response_id`: optional conversation handle for server-side history lookup.
- `stream`: when `true`, the agent streams the response back over the responses protocol.

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

If you change `agent.manifest.yaml` later, rerun `azd ai agent init -m agent.manifest.yaml` before `azd deploy` so the generated `agent.yaml` stays in sync. `azd deploy` uses the generated agent spec, not the manifest directly.

Do not declare `APPLICATIONINSIGHTS_CONNECTION_STRING` in `agent.manifest.yaml` or `agent.yaml`. Hosted Agents reserve that variable for platform use and inject it automatically when monitoring is enabled for the project. The agent runtime reads it automatically, and this sample only adds LangChain instrumentation on top of the host-managed tracer provider.
