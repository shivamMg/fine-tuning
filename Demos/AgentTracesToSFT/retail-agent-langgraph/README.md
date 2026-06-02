# Retail Agent in LangGraph

A multi-turn conversational tool-calling agent built with [LangGraph](https://langchain-ai.github.io/langgraph/) and deployable as a Hosted Agent on [Foundry](https://learn.microsoft.com/en-us/azure/foundry/what-is-foundry?tabs=python).

## Agent details

- Retail agent that assists customers with orders, returns, shipping, and product inquiries. It has access to tools that read and write order data to a local database.
- Built with LangGraph with an agentic loop to perform tool calls and manage conversation flow.
- Uses AgentServer to expose a responses-style API for multi-turn conversations with server-side state management.
- Calls Foundry Model Deployment backed by Azure OpenAI to generate chat completions and agent actions.
- Saves traces to Application Insights connected to Foundry for observability.

## Architecture

```
+--------------------+                   +----------------------------------+
|                    |                   | Retail Agent                     |
| User or client app |   --- POST -->    | LangGraph loop                   |                             +----------------------+
|                    |    /responses     | chatbot -> tools -> chatbot      |  -- /chat/completions -->   | Azure OpenAI LLM     |
| JSON body:         |                   |                                  |                             +----------------------+
| - input            |                   | tools:                           |
| - previous_        |                   | - find_user_id_by_email()        |                             +----------------------+
|   response_id?     |                   | - list_user_orders()             |  ----- Agent Traces  --->   | Application Insights |
|                    |                   | - ...                            |                             +----------------------+
+--------------------+                   +----------------------------------+
                                                       |
                                                       | Db calls from tools
                                                       v
                                              +----------------------+
                                              | Local Database       |
                                              +----------------------+
```


## Deploy to Foundry

### Prerequisites

- [Install Azure Developer CLI (azd)](https://learn.microsoft.com/en-us/azure/developer/azure-developer-cli/install-azd?tabs=winget-windows%2Cbrew-mac%2Cscript-linux&pivots=os-windows)
- Install Azure AI Agents extension using: `azd ext install azure.ai.agents`
- `Owner` or `RBAC Administrator` role on your Azure subscription to assign roles.
- Note: Below commands have been tested on PowerShell. Adjust syntax as needed for other shells.


### Deploy Agent to Foundry project

```shell
az account set --subscription "<YOUR_SUBSCRIPTION_NAME_OR_ID>"
azd auth login

mkdir hosted-agent && cd hosted-agent
azd ai agent init -m ../retail-agent-langgraph/agent.manifest.yaml

cd retail-agent-langgraph
azd up

# Grant "Foundry User" role to Agent's identity so it can use Foundry resources
$env:AZURE_AI_ACCOUNT_ID = ((cat .azure/retail-agent-langgraph-dev/.env | Select-String -Pattern '^AZURE_AI_ACCOUNT_ID').Line -split '=', 2)[1].Trim('"')
$env:AGENT_IDENTITY_CLIENT_ID = (azd ai agent show -o table | Select-String "Instance Identity Client ID").Line -replace ".*Client ID\s+",""
az role assignment create --assignee $env:AGENT_IDENTITY_CLIENT_ID --role "Foundry User" --scope $env:AZURE_AI_ACCOUNT_ID
```

### Invoke Agent

```shell
azd ai agent invoke "Please share my orders. My email is ava.moore2222@example.com"
```

Use [Copilot CLI](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-in-the-cli) to generate synthetic conversations with the agent:

```powershell
copilot --allow-all-tools -p 'Read the files under ./retail-agent-langgraph to understand what this agent does and what tools it exposes. Then generate 10 different realistic end-user conversations with it. For each conversation: (1) start a new conversation with `azd ai agent invoke "<first user message>" --new-conversation` and capture the conversation_id from the response, (2) continue with up to 2 more follow-up turns using `azd ai agent invoke "<next user message>" --conversation-id <conversation_id>`. Keep each conversation to a maximum of 3 turns. Vary the user personas, intents, and tools exercised across the 10 conversations.'
```


## Troubleshooting

### azd ai agent init fails with: 'create_agent: HTTP request failed: AzureDeveloperCLICredential'

If an `azd` command fails with the following error:

> create_agent: HTTP request failed: AzureDeveloperCLICredential: please run "azd auth login" from a command prompt to authenticate before using this credential

then run `azd auth logout` and `azd auth login` to refresh your credentials and try again.

### azd provision fails with: 'ERROR: The resource group location conflicts with the deployment.'

If `azd provision` fails with the following error:

> ERROR: The resource group location conflicts with the deployment.
> Suggestion: This usually means the resource group already exists in a different region than the one you specified. Either use the existing resource group's region with 'azd env set AZURE_LOCATION <existing-region>', create a new environment with 'azd env new', or delete the existing resource group and retry
> InvalidResourceGroupLocation: Invalid resource group location '<X_REGION>'. The Resource group already exists in location '<Y_REGION>'.

Then run `azd env set AZURE_LOCATION <Y_REGION>` with the existing resource group's region and try again.
