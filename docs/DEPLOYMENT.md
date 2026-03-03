# Deployment Guide

## 🚀 Production Deployment Guide

This guide covers deploying the Enterprise Agentic RAG Chatbot to production. The system consists of two deployable components:

- **FastAPI backend** (`src/api/main.py`) — Python, serves SSE streaming and REST endpoints
- **React frontend** (`frontend/`) — TypeScript/Vite, communicates with the backend over HTTP

Additionally, the standalone **Streamlit UI** (`app.py`) can be deployed independently for simple RAG-only access (no Databricks agents).

## 📋 Pre-Deployment Checklist

### Azure Resources
- [ ] Azure OpenAI resource with GPT-5.1 deployment
- [ ] text-embedding-3-large deployment (3072 dimensions)
- [ ] Azure AI Search service with semantic search + vector search configured
- [ ] Search index (`index-dev-figure-01-chunk` schema or equivalent) loaded with data
- [ ] Azure Blob Storage container (for document URL resolution + SAS token)
- [ ] Azure Blob Storage container for images (optional)
- [ ] Azure AI Foundry project (optional, for monitoring)
- [ ] Azure Databricks workspace with Unity Catalog SQL Warehouse (optional, for DataInsight)
- [ ] Auth decision: API key (`AZURE_OPENAI_AUTH_MODE=key`) or Managed Identity (`aad`)

### Application
- [ ] `.env` configured with all required production values
- [ ] Python 3.10+ and Node.js 18+ available on deployment target
- [ ] All Python and Node.js dependencies installable
- [ ] `logs/`, `tmp/`, `data/` directories writable
- [ ] Network connectivity to all Azure services verified
- [ ] Security review of SAS token expiry dates

## 🌐 Deployment Options

### Option 1: Azure App Service (Recommended for backend)

Deploy the FastAPI backend to Azure App Service and serve the React build as static files or via a CDN.

#### 1a. Build the React frontend

```bash
cd frontend
npm install
npm run build
# Build output: frontend/dist/
```

#### 1b. Create and configure App Service

```bash
az webapp create \
  --resource-group <your-rg> \
  --plan <your-plan> \
  --name <your-app-name> \
  --runtime "PYTHON:3.10"

az webapp config set \
  --resource-group <your-rg> \
  --name <your-app-name> \
  --startup-file "uvicorn src.api.main:app --host 0.0.0.0 --port 8000"
```

#### 1c. Set environment variables

```bash
az webapp config appsettings set \
  --resource-group <your-rg> \
  --name <your-app-name> \
  --settings \
    AZURE_OPENAI_ENDPOINT="<value>" \
    AZURE_OPENAI_AUTH_MODE="aad" \
    AZURE_OPENAI_GPT_DEPLOYMENT="gpt-5.1" \
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT="text-embedding-3-large" \
    AZURE_OPENAI_EMBEDDING_DIMENSIONS="3072" \
    AZURE_SEARCH_ENDPOINT="<value>" \
    AZURE_SEARCH_API_KEY="<value>" \
    AZURE_SEARCH_INDEX_NAME="<value>" \
    BASE_URL="<blob-base-url>" \
    SAS_TOKEN="<sas-token>" \
    DATABRICKS_HOST="<value>" \
    DATABRICKS_TOKEN="<value>" \
    DATABRICKS_HTTP_PATH="<value>" \
    DATABRICKS_CATALOG="<value>" \
    DATABRICKS_SCHEMAS="<comma-separated-schemas>"
```

#### 1d. Deploy code

```bash
az webapp up \
  --resource-group <your-rg> \
  --name <your-app-name> \
  --runtime "PYTHON:3.10"
```

#### 1e. Frontend static hosting

Serve `frontend/dist/` from Azure Static Web Apps or a CDN. Set the backend URL in the frontend environment:

```bash
# frontend/.env.production
VITE_API_URL=https://<your-app-name>.azurewebsites.net
```

Rebuild: `npm run build`

### Option 2: Azure Container Apps

#### 2a. Create a Dockerfile

```dockerfile
FROM python:3.10-slim

WORKDIR /app

# Install build tools for any native packages
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source (exclude node_modules, venv, __pycache__)
COPY src/ ./src/
COPY skills/ ./skills/
COPY .env .env

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

#### 2b. Build and push the backend image

```bash
az acr build \
  --registry <your-acr> \
  --image agentic-rag-backend:latest \
  --file Dockerfile .
```

#### 2c. Create a Dockerfile for the React frontend

```dockerfile
FROM node:18-alpine AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
ARG VITE_API_URL
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
```

```bash
az acr build \
  --registry <your-acr> \
  --image agentic-rag-frontend:latest \
  --build-arg VITE_API_URL=https://<backend-fqdn> \
  --file Dockerfile.frontend .
```

#### 2d. Deploy to Container Apps

```bash
# Backend
az containerapp create \
  --resource-group <your-rg> \
  --name agentic-rag-backend \
  --image <your-acr>.azurecr.io/agentic-rag-backend:latest \
  --target-port 8000 \
  --ingress internal \
  --env-vars \
    AZURE_OPENAI_ENDPOINT="<value>" \
    AZURE_OPENAI_AUTH_MODE="aad"
    # ... other env vars

# Frontend
az containerapp create \
  --resource-group <your-rg> \
  --name agentic-rag-frontend \
  --image <your-acr>.azurecr.io/agentic-rag-frontend:latest \
  --target-port 80 \
  --ingress external
```

### Option 3: Standalone Streamlit (RAG only)

For minimal deployments that do not require Databricks agents:

```bash
# Dockerfile.streamlit
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

## 🔐 Security Hardening

### Use Managed Identity (Recommended for Production)

Set `AZURE_OPENAI_AUTH_MODE=aad` and assign the *Cognitive Services OpenAI User* role to the App Service / Container App managed identity:

```bash
# Get the app's principal ID
PRINCIPAL=$(az webapp identity assign \
  --resource-group <your-rg> \
  --name <your-app-name> \
  --query principalId -o tsv)

# Get the AOAI resource ID
AOAI_ID=$(az cognitiveservices account show \
  --resource-group <aoai-rg> \
  --name <aoai-name> \
  --query id -o tsv)

# Assign role
az role assignment create \
  --assignee "$PRINCIPAL" \
  --role "Cognitive Services OpenAI User" \
  --scope "$AOAI_ID"
```

No API key is needed in `.env` when using this approach.

### Databricks Token Security

Use short-lived tokens or OAuth M2M credentials instead of long-lived PATs:
```bash
# Rotate PAT before expiry; update the DATABRICKS_TOKEN app setting
az webapp config appsettings set \
  --resource-group <your-rg> \
  --name <your-app-name> \
  --settings DATABRICKS_TOKEN="<new-token>"
```

### SAS Token Rotation

`BASE_URL` and `SAS_TOKEN` (Blob Storage) must be rotated before expiry. Check current expiry:
```bash
echo "$SAS_TOKEN" | grep "se="
```

Update in App Service:
```bash
az webapp config appsettings set ... --settings SAS_TOKEN="<new-sas>"
```

### Enable Azure Private Link

- Configure Private Endpoints for Azure OpenAI, Azure AI Search, and Azure Blob Storage
- Deploy the App Service / Container App inside a VNet with service endpoints

## 📊 Monitoring Setup

### Application Insights Integration

```bash
pip install opencensus-ext-azure
```

Add to `src/utils/logger.py`:
```python
from opencensus.ext.azure.log_exporter import AzureLogHandler

logger.addHandler(AzureLogHandler(
    connection_string='InstrumentationKey=<your-instrumentation-key>'
))
```

### Azure Monitor Alerts

Configure alerts for:
- Backend HTTP error rate > 5%
- Response latency P95 > 10s
- Azure OpenAI throttling (429 responses)
- Databricks query timeout rate

## 🔄 CI/CD Pipeline (GitHub Actions)

```yaml
name: Deploy to Azure

on:
  push:
    branches: [ main ]

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v3

    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.10'

    - name: Install Python dependencies
      run: pip install -r requirements.txt

    - name: Run Python compile check
      run: python -m py_compile src/api/main.py src/agents/master_agent.py

    - name: Set up Node.js
      uses: actions/setup-node@v3
      with:
        node-version: '18'

    - name: Build React frontend
      run: |
        cd frontend
        npm ci
        VITE_API_URL=${{ secrets.BACKEND_URL }} npm run build

    - name: Deploy backend to Azure Web App
      uses: azure/webapps-deploy@v2
      with:
        app-name: '<your-app-name>'
        publish-profile: ${{ secrets.AZURE_WEBAPP_PUBLISH_PROFILE }}
```

## 🧪 Testing in Production

### Backend health check

```bash
curl https://<your-backend>/health
# Expected: {"status": "ok", ...}

curl https://<your-backend>/skills
# Expected: list of registered skills
```

### Smoke test via SSE

```bash
curl -N -X POST https://<your-backend>/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "hello", "thread_id": "test-1", "enable_semantic_reranker": true}'
```

## 📈 Scaling Considerations

### Backend (FastAPI + Uvicorn)
- Scale out App Service plan horizontally (min 2 instances for HA)
- SSE connections are long-lived; tune App Service timeout settings
- Consider Azure Container Apps with auto-scaling on HTTP request length

### Conversation Thread Storage
- Current: in-memory dict (lost on restart/scale-out)
- For production with multiple replicas: migrate to Azure Cosmos DB or Azure Cache for Redis

### Databricks SQL Warehouse
- Use auto-stop enabled warehouse to avoid idle costs
- Scale warehouse up if SQL query latency is high

## 🔧 Configuration Management

### Environment-Specific Configs

Use App Service deployment slots or separate resources per environment:
- `dev` — development (key-based auth OK)
- `staging` — pre-production (AAD auth, representative data)
- `prod` — production (AAD auth, Managed Identity, Private Link)

## 📝 Post-Deployment Tasks

1. **Verify functionality**: Test each question type (RAG, data insight, metadata)
2. **Check skill loading**: `GET /skills` returns both `analytics-spec` and `metadata-mapping`
3. **Monitor logs**: `az webapp log tail --resource-group <your-rg> --name <your-app-name>`
4. **Set up Azure Monitor alerts**
5. **Document internal service endpoints** for the team

## 🆘 Troubleshooting Production Issues

**Backend not starting** — Check startup command, verify all required env vars are set, review App Service logs.

**403 AuthenticationTypeDisabled** — Switch to `AZURE_OPENAI_AUTH_MODE=aad` and assign the correct role to the Managed Identity.

**DataInsight agent unavailable** — Verify `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_HTTP_PATH` are set. Check SQL warehouse is running.

**Citations have no links** — Blob Storage SAS token may be expired or `BASE_URL` is missing. Update `SAS_TOKEN` in app settings.

**Slow cold start** — Pre-warm the app using App Service "Always On" setting or health-check pings.

## 📞 Support

For production issues:
1. Check application logs: `logs/application_YYYYMMDD.log`
2. Review Azure Monitor metrics and Application Insights
3. Contact Azure Support for service-level issues

---

**Always test in a staging environment before deploying to production!**
