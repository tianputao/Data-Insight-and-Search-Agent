# Quick Start Guide

## 📋 Prerequisites Checklist

Before starting, ensure you have:

- ✅ Python 3.10 or higher installed
- ✅ Node.js 18+ installed (for the React frontend)
- ✅ Java 11+ installed (for optional HermiT startup reasoning)
- ✅ Azure subscription with active resources
- ✅ Azure OpenAI service with primary and small tool-capable GPT deployments
- ✅ Azure AI Search service with semantic search + vector search configured
- ✅ text-embedding-3-large model deployed (3072 dimensions)
- ✅ Azure Blob Storage container (for document/image URL resolution)
- ✅ (Optional) Azure Databricks workspace with Unity Catalog SQL Warehouse
- ✅ Network access to all Azure services

## 🎯 5-Minute Setup

### Step 1: Install Dependencies

```bash
# Installs Python venv + pip packages AND Node.js packages for the React frontend
./run.sh install
```

Or manually:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && cd ..
```

### Step 2: Configure Azure Services

1. **Copy environment template**:
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env` with your credentials**:
   ```bash
   nano .env
   ```

3. **Required variables** (minimum set):

   | Variable | Description |
   |----------|-------------|
   | `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint URL |
   | `AZURE_OPENAI_AUTH_MODE` | `auto` \| `key` \| `aad` (default: `auto`) |
   | `AZURE_OPENAI_API_KEY` | API key — required when `AUTH_MODE=key` or `auto` |
   | `AZURE_OPENAI_GPT_DEPLOYMENT` | Primary deployment for MasterAgent and DataInsightAgent |
   | `AZURE_OPENAI_GPT_SMALL_DEPLOYMENT` | Smaller tool-capable deployment for OntologyAgent and MetadataAgent |
   | `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Name of your text-embedding-3-large deployment |
   | `AZURE_SEARCH_ENDPOINT` | Azure AI Search endpoint URL |
   | `AZURE_SEARCH_API_KEY` | Azure AI Search admin/query key |
   | `AZURE_SEARCH_INDEX_NAME` | Name of your search index |

4. **Auth mode selection**:

   - **API Key** (default if key is set):
     ```
     AZURE_OPENAI_AUTH_MODE=key
     AZURE_OPENAI_API_KEY=<your-key>
     ```
   - **AAD / Entra ID** (when key-based auth is disabled on the resource):
     ```
     AZURE_OPENAI_AUTH_MODE=aad
     # Leave AZURE_OPENAI_API_KEY blank or remove it
     # Run 'az login' with an identity that has the
     # "Cognitive Services OpenAI User" role on the resource
     ```

### Step 3: Verify Azure AI Search Index Schema

Your index must match the schema expected by `AzureSearchConfig`. Default field names:

| Field | Default name | Notes |
|-------|-------------|-------|
| Document ID | `id` | Key field (Edm.String) |
| Content | `content` | Searchable (Edm.String) |
| Title | `title` | Searchable (Edm.String) |
| File path | `filepath` | Filterable |
| Public URL | `url` | Used for citations |
| Content vector | `contentVector` | Collection(Edm.Single), 3072d |
| Metadata vector | `full_metadata_vector` | Collection(Edm.Single), 3072d |
| Main title | `main_title` | Top-level document title |
| Sub title | `sub_title` | Section title |
| Publisher | `publisher` | e.g. standard body name |
| Document code | `document_code` | Standard/regulation number |

All field names are overridable via `AZURE_SEARCH_*_FIELD` environment variables in `.env`.

**Semantic search**: Create a semantic configuration named `default` (or set `AZURE_SEARCH_SEMANTIC_CONFIG`).  
**Vector search**: Use profile name `vectorSearchProfile` (or set `AZURE_SEARCH_VECTOR_PROFILE`).

### Step 4: Configure Blob Storage (for Citations)

To show clickable citation links, set your Blob Storage details:
```
BASE_URL=https://<account>.blob.core.windows.net/<container>
SAS_TOKEN=sp=rl&st=...&se=...&sv=...&sr=c&sig=...

AZURE_IMAGE_BASE_URL=https://<account>.blob.core.windows.net/<image-container>
AZURE_IMAGE_SAS_TOKEN=sp=r&st=...&se=...&spr=https&sv=...&sr=c&sig=...
```

Documents with no public URL are still cited as plain-text footnotes.

### Step 5: Configure Ontology Runtime

Owlready2 recursively loads repository OWL files without modifying them. Defaults:

```env
DEFAULT_ENABLE_ONTOLOGY=true
ONTOLOGY_DIR=Ontology
ONTOLOGY_FILE_GLOB=**/*.owl
ONTOLOGY_ONLY_LOCAL=true
ONTOLOGY_ENABLE_REASONER=true
ONTOLOGY_REASONER=hermit
ONTOLOGY_MAX_RESULTS=25
ONTOLOGY_MAX_DEPTH=5
ONTOLOGY_MAX_PATHS=10
ONTOLOGY_MAX_NODES=250
ONTOLOGY_FUZZY_THRESHOLD=0.62
ONTOLOGY_AGENT_TIMEOUT_SECONDS=90
```

`DEFAULT_ENABLE_ONTOLOGY` initializes each new React session independently. The active session's switch is sent with each request and does not affect other sessions.

HermiT startup reasoning is enabled by default. Explicit classes, properties, restrictions, inverse relations, and multi-hop graph queries remain available. Set `ONTOLOGY_ENABLE_REASONER=false` to disable it. When Ontology is enabled, OntologyAgent first progressively matches governed Skills. A confirmed governed match skips OWL and Metadata; otherwise role-neutral semantic discovery is followed by a skill-free Metadata verifier. DataInsightAgent then loads `sql-planning`, and the primary model selects analytical roles, grain, comparisons, and SQL from the question, OWL evidence, and verified UC metadata. When Ontology is disabled or fails, Metadata discovery progressively loads `metadata-mapping`.

The normal analytics handoff is linear and does not return to the Master LLM between sub-agents. If DataInsightAgent detects an unexpectedly missing or incomplete handoff, its own MAF loop may recover Metadata or enabled Ontology context once before continuing. Disabled Ontology and known upstream Ontology failures are never retried.

### Step 6: (Optional) Configure Databricks

For the DataInsightAgent and MetadataAgent:
```
DATABRICKS_HOST=https://adb-XXXX.XX.azuredatabricks.net/
DATABRICKS_TOKEN=dapiXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<warehouse-id>
DATABRICKS_CATALOG=<your-unity-catalog-name>
DATABRICKS_SCHEMAS=silver  # Add comma-separated schemas only when they actually exist
DATABRICKS_METADATA_CACHE_TTL_SECONDS=900
```

When these are not set, `DatabricksConfig.is_configured()` returns `False` and both agents are skipped.
Metadata tools cache only objects they actually request: catalog schemas, table summaries for a specific schema, and details for a selected fully-qualified table. The cache is not question-scoped and never preloads every table's columns. Set the TTL to `0` for a process-lifetime object cache.

### Step 7: (Optional) Author the Business Layer Document

Business users can describe semantics the OWL ontology does not define — terminology, metric definitions, and reporting conventions. Open the React UI and click **Business Layer Doc** in the chat header, or call the API directly:

```bash
curl http://localhost:8000/business-layer
curl -X PUT http://localhost:8000/business-layer \
  -H 'Content-Type: application/json' \
  -d '{"content": "# Metric definitions\n- Revenue uses the order total, not the subtotal\n"}'
```

The text is stored at `data/business_layer.md` (git-ignored), shared by every session, and read on each request, so edits apply to the next question without restarting. It is advisory: verified Unity Catalog schema always wins, and the document is never treated as instructions. No configuration is required — when the file is absent the feature stays inert.

### Step 8: Run the Application

```bash
./run.sh             # Full stack: FastAPI (port 8000) + React (port 3000)
./run.sh backend     # FastAPI only
./run.sh frontend    # React dev server only
```

Open your browser to `http://localhost:3000`.

## 🔍 Verify Installation

### Test configuration

```bash
source venv/bin/activate
python -c "from src.config import validate_config; validate_config(); print('Config OK')"
```

### Test ontology loading

```bash
source venv/bin/activate
python -c "from src.ontology import OntologyService; s=OntologyService(enable_reasoner=False).load(); print(s.health())"
curl http://localhost:8000/config
```

### Test search tool

```python
from src.tools import create_search_tool
import asyncio

tool = create_search_tool()
results = asyncio.run(tool.search("test query", top_k=3))
print(f"Retrieved {len(results)} results")
```

### Test agent initialization

```python
from src.tools import create_search_tool
from src.agents import SearchAgent, MasterAgent

search_tool = create_search_tool()
search_agent = SearchAgent(tools=[search_tool])
master = MasterAgent(search_agent=search_agent)
print("All agents initialized")
```

### Test backend health endpoint

```bash
# With FastAPI running:
curl http://localhost:8000/health
curl http://localhost:8000/skills
```

## 🐛 Troubleshooting

### "Missing required configuration values"

Check `.env` has all required variables:
```bash
grep -v '^#' .env | grep '=' | head -20
```

### "Error code: 403 - AuthenticationTypeDisabled"

Key-based auth is disabled on your Azure OpenAI resource. Set:
```
AZURE_OPENAI_AUTH_MODE=aad
```
Then run `az login` and ensure your identity has the *Cognitive Services OpenAI User* role.

### "Search returns no results"

1. Verify index name in `.env` matches Azure portal
2. Check `AZURE_SEARCH_VECTOR_FIELD=contentVector` matches your index schema
3. Confirm embedding dimensions = 3072 (`AZURE_SEARCH_VECTOR_DIMENSIONS=3072`)
4. Test search directly:
   ```bash
   curl -H "api-key: $AZURE_SEARCH_API_KEY" \
     "$AZURE_SEARCH_ENDPOINT/indexes/$AZURE_SEARCH_INDEX_NAME?api-version=2023-11-01"
   ```

### "DataInsight/Metadata agent not available"

Set the three required Databricks variables:
```
DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_HTTP_PATH
```

### React frontend cannot connect to backend

1. Confirm FastAPI is running: `curl http://localhost:8000/health`
2. Check CORS origins in `src/api/main.py` include `http://localhost:3000`
3. Confirm `VITE_API_URL` in `frontend/.env` (if set) points to `http://localhost:8000`

## 📚 Next Steps

1. Load your data into the Azure AI Search index
2. Enable semantic search in Azure Portal and set `AZURE_SEARCH_SEMANTIC_CONFIG`
3. Test example questions via the React UI
4. Review logs in `logs/` for debugging
5. Customize agent prompts in the per-agent modules under `src/prompts/` (`master.py`, `search.py`, `ontology.py`, `data_insight.py`, `metadata.py`)
6. Add domain skills to `skills/` directory

## 🔐 Security Notes

- Never commit `.env` to version control (it is in `.gitignore`)
- Use `AZURE_OPENAI_AUTH_MODE=aad` with Managed Identity for production
- Rotate SAS tokens before expiry
- Scope Databricks PAT tokens to minimum required permissions

## 📊 Monitoring

```bash
tail -f logs/application_$(date +%Y%m%d).log
```

## 🎓 Learning Resources

- [Microsoft Agent Framework Documentation](https://learn.microsoft.com/en-us/agent-framework/)
- [Azure AI Search Documentation](https://learn.microsoft.com/en-us/azure/search/)
- [Azure OpenAI Service Documentation](https://learn.microsoft.com/en-us/azure/ai-services/openai/)
- [Azure Databricks Unity Catalog](https://learn.microsoft.com/en-us/azure/databricks/data-governance/unity-catalog/)

---

**Need Help?** Check the main `README.md`, `ARCHITECTURE.md`, or the application logs for detailed error messages.
