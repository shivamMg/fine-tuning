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


### Initialize Agent in Foundry project

To use your existing Foundry project, you'll need the following resources:
1. Model deployment with name `gpt-4.1` in the Foundry project.
2. Azure Container Registry (ACR) resource. Keep its resource ID and login server handy:
   - `/subscriptions/<YOUR_SUBSCRIPTION_ID>/resourceGroups/<YOUR_RESOURCE_GROUP>/providers/Microsoft.ContainerRegistry/registries/<YOUR_ACR_NAME>`
   - `<YOUR_REGISTRY>.azurecr.io`.
3. Application Insights resource. Keep its resource ID and connection string handy:
   - `/subscriptions/<YOUR_SUBSCRIPTION_ID>/resourceGroups/<YOUR_RESOURCE_GROUP>/providers/Microsoft.Insights/components/<YOUR_APP_INSIGHTS_NAME>`
   - `InstrumentationKey=...`.

`azd ai agent init` command below will prompt you to share these values.

```shell
# Replace placeholders with your Foundry project details
$env:FOUNDRY_PROJECT_ID="/subscriptions/<YOUR_SUBSCRIPTION_ID>/resourceGroups/<YOUR_RESOURCE_GROUP>/providers/Microsoft.CognitiveServices/accounts/<YOUR_FOUNDRY_ACCOUNT_NAME>/projects/<YOUR_PROJECT_NAME>"

# Replace placeholders with your ACR details
$env:ACR_ID="/subscriptions/<YOUR_SUBSCRIPTION_ID>/resourceGroups/<YOUR_RESOURCE_GROUP>/providers/Microsoft.ContainerRegistry/registries/<YOUR_ACR_NAME>"

# Grant "AcrPull" role to Foundry project's identity so it can pull container images
$ProjectIdentityClientId = $(az resource show --ids $env:FOUNDRY_PROJECT_ID --query identity.principalId -o tsv)
az role assignment create --assignee $ProjectIdentityClientId --role "AcrPull" --scope $env:ACR_ID

# Run from parent directory of  retail-agent-langgraph/
azd auth login
azd ai agent init -m retail-agent-langgraph/agent.manifest.yaml --project-id $env:FOUNDRY_PROJECT_ID --model gpt-4.1
azd env set enableHostedAgentVNext true
```

#### (Optional) Create a new Foundry project

If you don't have an existing Foundry project, you can create a new one using `azd` which will set up the necessary resources including Application Insights and ACR. Skip this step if you already have a Foundry project.

```shell
azd auth login
azd ai agent init -m retail-agent-langgraph/agent.manifest.yaml
azd env set enableHostedAgentVNext=true
pip install keyring artifacts-keyring
azd provision
```


### Deploy Agent

```shell
azd deploy

# Extract Foundry ID from FOUNDRY_PROJECT_ID in powershell
$env:FOUNDRY_ID = ($env:FOUNDRY_PROJECT_ID -replace '/projects/.*$', '')

# Grant "Azure AI/Foundry User" role to Agent's identity so it can use Foundry resources
$AgentIdentityClientId = (azd ai agent show -o table | Select-String "Instance Identity Client ID").Line -replace ".*Client ID\s+",""
az role assignment create --assignee $AgentIdentityClientId --role "53ca6127-db72-4b80-b1b0-d745d6d5456d" --scope $env:FOUNDRY_ID
```


### Invoke Agent

```shell
azd ai agent invoke "Please share my orders. My email is ava.moore2222@example.com"
```

Use [Copilot CLI](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-in-the-cli) to generate synthetic conversations with the agent. Run this from the parent directory of `retail-agent-langgraph/` so Copilot can read the agent's source files for context:

```powershell
copilot --allow-all-tools -p 'Read the files under ./retail-agent-langgraph to understand what this agent does and what tools it exposes. Then generate 10 different realistic end-user conversations with it. For each conversation: (1) start a new conversation with `azd ai agent invoke "<first user message>" --new-conversation` and capture the conversation_id from the response, (2) continue with up to 2 more follow-up turns using `azd ai agent invoke "<next user message>" --conversation-id <conversation_id>`. Keep each conversation to a maximum of 3 turns. Vary the user personas, intents, and tools exercised across the 10 conversations.'
```


## Troubleshooting

### Agent init fails with: "create_agent: HTTP request failed: AzureDeveloperCLICredential"

If an `azd` command fails with the following error, then run `azd auth logout` and `azd auth login` to refresh your credentials and try again.

```
create_agent: HTTP request failed: AzureDeveloperCLICredential: please run "azd auth login" from a command prompt to authenticate before using this credential
```
