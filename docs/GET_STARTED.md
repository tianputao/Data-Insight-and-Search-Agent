# Enterprise Agentic RAG Chatbot — Getting Started

## ✨ Project Overview

**Project Name**: Enterprise Agentic RAG Chatbot  
**Location**: `/home/tarhone/Comprehensive_AI_Agent`  
**Status**: Production-ready

---

## 🎯 What You Have

### 🤖 Multi-Agent System (4 Agents)

| Agent | Purpose | Key Tools |
|-------|---------|-----------|
| **MasterAgent** | Orchestration, query routing, answer synthesis | `decompose_query`, `search_multiple_queries`, `search_knowledge`, `delegate_metadata`, `delegate_data_insight` |
| **SearchAgent** | Azure AI Search hybrid retrieval | `search_knowledge_base`, `parallel_search` |
| **DataInsightAgent** | Databricks Unity Catalog SQL analytics | `get_relevant_tables`, `execute_sql`, `load_skill` |
| **MetadataAgent** | Unity Catalog schema browsing | `list_schemas`, `list_tables`, `get_table_details`, `search_tables`, `load_skill` |

All agents are powered by Azure OpenAI GPT-5.1 via the Microsoft Agent Framework (MAF) `AzureOpenAIChatClient`.

### 🔌 Skill System

Plugin-based skills in `skills/`:
- **`analytics-spec`** — data analytics query conventions
- **`metadata-mapping`** — Unity Catalog metadata field mapping

The `SkillRegistry` scans skills at startup; agents call `load_skill` to get full skill content at runtime. The `SkillInjector` embeds skill metadata XML into agent prompts automatically.

### 🖥️ Dual Frontend

| Mode | Command | Port | Notes |
|------|---------|------|-------|
| Full Stack (React) | `./run.sh` | 3000 (UI) + 8000 (API) | Streaming SSE, all 4 agents |
| Backend only | `./run.sh backend` | 8000 | FastAPI |
| Frontend only | `./run.sh frontend` | 3000 | React dev server |
| Streamlit | `./run.sh streamlit` | 8501 | RAG only, no Databricks agents |

### 🗂️ Key Files

```
src/
├── agents/
│   ├── master_agent.py       # Orchestration (5 tools)
│   ├── search_agent.py       # Azure AI Search (2 tools)
│   ├── data_insight_agent.py # Databricks SQL (3 tools)
│   └── metadata_agent.py     # Unity Catalog schema (5 tools)
├── api/main.py               # FastAPI backend + SSE streaming + citation pipeline
├── config/settings.py        # All config classes (OpenAI, Search, Databricks, App)
├── injector.py               # SkillInjector (XML prompt injection)
├── registry.py               # SkillRegistry (scans skills/ dir)
├── tools/ai_search_tool.py   # Azure AI Search: hybrid, semantic, agentic modes
└── prompts/system_prompts.py # Agent system prompts
skills/
├── analytics-spec/SKILL.md
└── metadata-mapping/SKILL.md
frontend/src/
├── App.tsx                   # Chat UI + citation normalization
├── services/api.ts           # SSE client
└── types/index.ts
app.py                        # Standalone Streamlit UI (RAG only)
run.sh                        # Launcher script
```

### 🏗️ Enterprise Features

- ✅ **Streaming responses** — SSE with `thinking`, `text`, `refs`, `done`, `error` events
- ✅ **Citation pipeline** — references collected during tool calls, rendered as `[1]`, `[2]` footnotes; documents without a public URL shown as plain-text citations
- ✅ **AUTH_MODE** — `auto | key | aad` controls API key vs. AAD/`DefaultAzureCredential` auth
- ✅ **Rich index schema** — 30+ field mappings covering document metadata, language, header hierarchy, image mapping
- ✅ **Blob Storage SAS** — document and image URLs auto-resolved with SAS tokens
- ✅ **Optional Databricks** — DataInsight and Metadata agents gracefully absent when not configured
- ✅ **Comprehensive logging** — `logs/application_YYYYMMDD.log`

---

## 🚀 Quick Start (3 Steps)

### Step 1: Install Dependencies
```bash
cd /home/tarhone/Comprehensive_AI_Agent
./run.sh install
```

### Step 2: Configure Environment
```bash
cp .env.example .env
nano .env   # Fill in your Azure credentials
```

Minimum required:
```
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_AUTH_MODE=aad          # or: key (if key-based auth is enabled)
AZURE_OPENAI_API_KEY=               # required only when AUTH_MODE=key
AZURE_OPENAI_GPT_DEPLOYMENT=gpt-5.1
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large
AZURE_SEARCH_ENDPOINT=https://your-search.search.windows.net
AZURE_SEARCH_API_KEY=your-search-key
AZURE_SEARCH_INDEX_NAME=your-index-name
```

### Step 3: Run
```bash
./run.sh            # React UI at http://localhost:3000
# or
./run.sh streamlit  # Streamlit UI at http://localhost:8501
```

---

## 🔑 Key Configuration Details

### Authentication (`AZURE_OPENAI_AUTH_MODE`)

| Value | Behaviour |
|-------|-----------|
| `key` | Uses `AZURE_OPENAI_API_KEY` (fails if key-based auth disabled on resource) |
| `aad` | Uses `DefaultAzureCredential` (requires `az login` + *Cognitive Services OpenAI User* role) |
| `auto` | Uses key if `AZURE_OPENAI_API_KEY` is set, otherwise AAD |

### Feature Flags

| Variable | Default | Effect |
|----------|---------|--------|
| `DEFAULT_ENABLE_SEMANTIC_RERANKER` | `true` | Azure AI Search semantic reranking |
| `DEFAULT_ENABLE_AGENTIC_RETRIEVAL` | `true` | Azure-managed agentic retrieval |

### Databricks (Optional)

When these three are set, `DatabricksConfig.is_configured()` returns `True` and both Databricks agents are activated:
```
DATABRICKS_HOST=https://adb-XXXX.XX.azuredatabricks.net/
DATABRICKS_TOKEN=dapiXXXXXXXXXXXXXXXX
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<warehouse-id>
DATABRICKS_CATALOG=<catalog-name>
DATABRICKS_SCHEMAS=silver,gold
```

---

## 🎓 Understanding the System

### Agent Workflow (Knowledge Q&A)

```
User Question
       ↓
MasterAgent (GPT-5.1)
       ↓
  Simple?  → search_knowledge        → SearchAgent → Azure AI Search
  Complex? → decompose_query
              → search_multiple_queries (parallel) → SearchAgent
  Data?    → delegate_data_insight   → DataInsightAgent → Databricks SQL
  Schema?  → delegate_metadata       → MetadataAgent   → Unity Catalog
       ↓
Collect search refs  →  push "refs" SSE event
       ↓
Synthesize answer with context
       ↓
Stream: thinking → text chunks → refs → done
       ↓
Frontend: render answer + footnote citations
```

### Citation Pipeline

1. During `search_knowledge` / `search_multiple_queries`, document titles and URLs are collected into a `refs_map`
2. `refs_map` is pushed as a `refs` SSE event to the frontend queue
3. The backend citation pipeline (`_normalize_citations_and_references`) merges inline `[[n]](url)` marks with the map
4. Documents with no public URL are rendered as `[n] title` (plain text); linked documents as `[n] [title](url)`
5. Frontend `normalizeCitationsForDisplay()` handles both formats

### Search Index Schema

The default index (`index-dev-figure-01-chunk`) has:
- **Content fields**: `content`, `title`, `main_title`, `sub_title`, `description`
- **Vector fields**: `contentVector` (3072d), `full_metadata_vector` (3072d)
- **Document metadata**: `publisher`, `document_code`, `document_category`, `document_schema`
- **Language**: `primary_language`, `secondary_language`, localized title alternatives
- **Headers**: `full_headers`, `h1`–`h6`
- **Storage**: `url`, `filepath` (resolved to Blob SAS URLs), `image_mapping`

All field names are configurable via `AZURE_SEARCH_*_FIELD` env vars.

---

## ✅ Test Checklist

```bash
# 1. Config validation
python -c "from src.config import validate_config; validate_config(); print('OK')"

# 2. Backend health
curl http://localhost:8000/health

# 3. Skills loaded
curl http://localhost:8000/skills

# 4. Search tool
python -c "
import asyncio
from src.tools import create_search_tool
t = create_search_tool()
r = asyncio.run(t.search('test'))
print(f'Search: {len(r)} results')
"
```

---

## 📊 Evaluation & Monitoring

- Logs: `logs/application_YYYYMMDD.log`
- A/B test via `.env` feature flags: toggle `DEFAULT_ENABLE_SEMANTIC_RERANKER` / `DEFAULT_ENABLE_AGENTIC_RETRIEVAL`
- Export logs to Azure AI Foundry (`AZURE_AI_PROJECT_CONNECTION_STRING`) for quality metrics

---

## 🔐 Security Checklist

- ✅ API credentials stored in `.env` only (git-ignored)
- ✅ `AZURE_OPENAI_AUTH_MODE=aad` supported for keyless auth
- ✅ Blob Storage secured with time-limited SAS tokens
- ✅ No hardcoded secrets in source code
- 🔲 **Production**: Use Managed Identity (`aad` mode) + Private Link
- 🔲 **Production**: Add user authentication layer in front of the React app

---

## 🆘 Common Issues & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `403 AuthenticationTypeDisabled` | Key auth disabled on AOAI resource | Set `AZURE_OPENAI_AUTH_MODE=aad`, run `az login` |
| Generic `Reference N` in citations | Index returned documents with empty titles | Titles now preserved with fallback to doc code or path |
| No citations on English queries | `Internal Document` refs (no URL) were discarded | Now stored as `(title, "")` and rendered as plain text |
| DataInsight agent missing | Databricks not configured | Set required `DATABRICKS_*` variables |
| Frontend can't reach backend | CORS or wrong URL | Check `src/api/main.py` CORS origins; frontend uses port 8000 |

---

## 📚 Documentation Index

| File | Content |
|------|---------|
| `README.md` | Feature overview, project structure, quick start |
| `SETUP.md` | Detailed setup with all config options |
| `ARCHITECTURE.md` | Component deep dive, data flows, extension points |
| `DEPLOYMENT.md` | Production deployment to App Service / Container Apps |
| `GET_STARTED.md` | This file — project snapshot and quick reference |
| `docs/AZURE_SEARCH_CONFIG.md` | Azure AI Search index configuration details |

---

**🎊 Your Enterprise Agentic RAG Chatbot is ready!**

```bash
./run.sh   # Start the full stack
```

React chat UI → `http://localhost:3000`  
