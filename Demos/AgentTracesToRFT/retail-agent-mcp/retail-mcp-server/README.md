# Retail MCP Server

MCP server implementing customer service tools (order management, user lookup, product catalog) over Streamable HTTP transport.

| File | Description |
|------|-------------|
| `mcp_server.py` | Server to expose MCP `/mcp` and OpenAI `/tools` endpoints |
| `db.json` | Mock database (users, orders, products) |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container image definition |

## Run locally

```bash
pip install -r requirements.txt
$env:MCP_API_KEY="your-secret-key"  # powershell
python mcp_server.py
```

Server starts at `http://localhost:8000`. Get tools: `GET /tools`.

### Run locally with Docker

```bash
docker build -t retail-mcp-server .
docker run -p 8000:8000 -e MCP_API_KEY=your-secret-key retail-mcp-server
```

## Deploy to Azure Container Apps

Prerequisites: [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) with the `containerapp` extension, an Azure subscription.

### Deploy

Build and deploy with the following command. Make sure to replace placeholders with your values.

`--registry-server` is optional if you want to push the image to your ACR instead of letting Container Apps to create and use a new ACR.

```bash
az containerapp up \
  --name retail-mcp-server \
  --subscription <YOUR_SUBSCRIPTION> \
  --resource-group <YOUR_RG> \
  --source . \
  --ingress external \
  --target-port 8000 \
  --env-vars PORT=8000 \
  --registry-server <YOUR_ACR>.azurecr.io
```

Then set the `MCP_API_KEY` secret (only needed once — persists across redeployments):

```bash
az containerapp secret set \
  --name retail-mcp-server \
  --subscription <YOUR_SUBSCRIPTION> \
  -g <YOUR_RG> \
  --secrets mcp-api-key=<YOUR_SECRET_KEY>

az containerapp update \
  --name retail-mcp-server \
  --subscription <YOUR_SUBSCRIPTION> \
  -g <YOUR_RG> \
  --set-env-vars MCP_API_KEY=secretref:mcp-api-key
```


## Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/mcp/` | POST | Yes | MCP Streamable HTTP transport |
| `/tools` | POST | Yes | OpenAI function-call compatible endpoint |
| `/tools` | GET | No | Returns list of tool names. Can be used for server health check. |

For authentication, set `X-MCP-API-Key` header to the value of `MCP_API_KEY` env var.

### POST `/tools` endpoint

Request:
```json
{
  "type": "function_call",
  "id": "fc_123",
  "call_id": "call_123",
  "name": "calculate",
  "arguments": "{\"expression\": \"2 + 3\"}"
}
```

Response:
```json
{
  "type": "function_call_output",
  "id": "fc_123",
  "call_id": "call_123",
  "output": "{\"result\": 5}"
}
```

### Available tools

17 tools available: `calculate`, `find_user_id_by_email`, `find_user_id_by_name_zip`, `list_all_product_types`, `get_product_details`, `get_user_details`, `get_order_details`, `cancel_pending_order`, `modify_pending_order_items`, `modify_pending_order_payment`, `modify_pending_order_address`, `modify_user_address`, `exchange_delivered_order_items`, `return_delivered_order_items`, `list_user_orders`, `policy_verify_return`, `transfer_to_human_agents`.

### Print Tool endpoints for RFT job

```bash
python -c "from mcp_server import tool_endpoints; print(tool_endpoints())"
```

### MCP Client Configuration

```json
{
  "mcpServers": {
    "retail-mcp": {
      "url": "https://<your-app>.azurecontainerapps.io/mcp/",
      "headers": {
        "X-MCP-API-Key": "your-secret-key"
      }
    }
  }
}
```
