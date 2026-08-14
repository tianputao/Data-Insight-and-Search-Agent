# Enterprise Agentic RAG Chatbot — Getting Started

## ✨ Project Overview

**Project Name**: Enterprise Agentic RAG Chatbot  
**Location**: repository root
**Status**: active application; validate your target environment before production deployment

---

## 🎯 What You Have

### 🤖 Multi-Agent System (5 Agents)

| Agent | Purpose | Key Tools |
|-------|---------|-----------|
| **MasterAgent** | Orchestration and routing | `decompose_query`, `search_multiple_queries`, `search_knowledge`, `delegate_metadata`, `delegate_data_analysis` |
| **SearchAgent** | Azure AI Search hybrid retrieval | `search_knowledge_base`, `parallel_search` |
| **OntologyAgent** | Governed Skill routing, deterministic OWL business context, and model-driven weak-result recovery | `get_business_context`, `list_defined_classes`, and related Owlready2 recovery tools |
| **DataInsightAgent** | Databricks Unity Catalog SQL analytics | `execute_sql`; bounded context recovery; governed template Skills plus native `sql-planning` |
| **MetadataAgent** | Unity Catalog candidate recall and physical verification | deterministic table-summary recall and batch detail fetch; UC tools for gap recovery; native `metadata-mapping` Skill in discovery mode |

Agents use Azure OpenAI through Microsoft Agent Framework and `OpenAIChatCompletionClient`: Master, Search, Ontology routing/recovery, and DataInsight use the primary deployment; Metadata discovery/verification uses the configured small deployment.

### 🔌 Skill System

Plugin-based skills in `skills/`:
- **`analytics-spec`** — governed highest-spending-customer matching contract and SQL resource
- **`sql-planning`** — dynamic OWL + verified UC query-planning method
- **`metadata-mapping`** — Unity Catalog metadata field mapping

MAF `SkillsProvider` discovers `SKILL.md` files, advertises only the Skills assigned to each agent, and registers native load/resource/script tools. Read-only loading is trusted; script execution remains approval-gated.

### 🖥️ Run Modes

| Mode | Command | Port | Notes |
|------|---------|------|-------|
| Full Stack (React) | `./run.sh` | 3000 (UI) + 8000 (API) | Streaming SSE, all 5 agents |
| Backend only | `./run.sh backend` | 8000 | FastAPI |
| Frontend only | `./run.sh frontend` | 3000 | React dev server |

### 🗂️ Key Files

```
src/
├── agents/
│   ├── master_agent.py       # Orchestration (5 tools)
│   ├── search_agent.py       # Azure AI Search (2 tools)
│   ├── data_insight_agent.py # Databricks SQL + governed/dynamic Skill provider
│   ├── metadata_agent.py     # Unity Catalog + metadata-mapping provider
│   ├── ontology_agent.py     # Owlready2 business context tools
│   └── maf_runtime.py        # MAF client/session/stream adapter
├── ontology/service.py       # Recursive read-only OWL loading and graph queries
├── api/main.py               # FastAPI backend + SSE streaming + citation pipeline
├── config/settings.py        # All config classes (OpenAI, Search, Databricks, App)
├── skills_provider.py        # Native MAF SkillsProvider factory/API adapter
├── business_layer.py         # Workspace business semantic document store (data/business_layer.md)
├── tools/ai_search_tool.py   # Azure AI Search: hybrid, semantic, agentic modes
└── prompts/                  # Per-agent system prompts (master, search, ontology, data_insight, metadata)
skills/
├── analytics-spec/
│   ├── SKILL.md
│   └── references/highest-spending-customer.sql
├── sql-planning/SKILL.md
└── metadata-mapping/SKILL.md
frontend/src/
├── App.tsx                   # Chat UI + citation normalization
├── services/api.ts           # SSE client
├── types.ts
└── types/activity.ts
Ontology/*.owl                # Read-only business ontologies
run.sh                        # Launcher script
```

### 🏗️ Enterprise Features

- ✅ **Streaming responses** — SSE with activity, text, reset, completion, stop, and error events; final citations arrive in `done.content`
- ✅ **Citation pipeline** — references collected during tool calls, rendered as `[1]`, `[2]` footnotes; documents without a public URL shown as plain-text citations
- ✅ **AUTH_MODE** — `auto | key | aad` controls API key vs. AAD/`DefaultAzureCredential` auth
- ✅ **Rich index schema** — 30+ field mappings covering document metadata, language, header hierarchy, image mapping
- ✅ **Blob Storage SAS** — document and image URLs auto-resolved with SAS tokens
- ✅ **Optional Databricks capability** — the application starts without Databricks, while UC/SQL tools report configuration errors if invoked
- ✅ **Ontology-guided SQL** — session-selectable role-neutral OWL properties, restrictions, lineage, and semantic paths before UC verification and model-driven planning
- ✅ **Visible fallback** — Ontology failures are shown in the thinking panel before standard metadata-driven analysis continues
- ✅ **User-authored business layer** — business users edit a workspace semantic document in the UI; it is read per request, so edits apply without restarting the agent
- ✅ **Comprehensive logging** — `logs/application_YYYYMMDD.log`

---

## 🚀 Quick Start (3 Steps)

### Step 1: Install Dependencies
```bash
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
AZURE_OPENAI_GPT_DEPLOYMENT=<primary-deployment>
AZURE_OPENAI_GPT_SMALL_DEPLOYMENT=<small-tool-capable-deployment>
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large
AZURE_SEARCH_ENDPOINT=https://your-search.search.windows.net
AZURE_SEARCH_API_KEY=your-search-key
AZURE_SEARCH_INDEX_NAME=your-index-name
```

### Step 3: Run
```bash
./run.sh            # React UI at http://localhost:3000
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
| `DEFAULT_ENABLE_ONTOLOGY` | `true` | Initial Ontology switch value for each new session |

The React Ontology switch belongs to the active session. Switching it does not affect other sessions or already-running requests.

### Databricks (Optional)

When these three are set, `DatabricksConfig.is_configured()` returns `True` and UC/SQL tools can execute:
```
DATABRICKS_HOST=https://adb-XXXX.XX.azuredatabricks.net/
DATABRICKS_TOKEN=dapiXXXXXXXXXXXXXXXX
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/<warehouse-id>
DATABRICKS_CATALOG=<catalog-name>
DATABRICKS_SCHEMAS=silver  # Add comma-separated schemas only when they actually exist
```

---

## 🎓 Understanding the System

### Agent Workflow (Knowledge Q&A)

```
User Question
       ↓
MasterAgent (primary GPT deployment, main AgentSession + bounded agentic loop)
       ↓
  Correct and enrich search terminology
         ↓
  Simple?  → search_knowledge        → SearchAgent → Azure AI Search
  Complex? → decompose_query
              → search_multiple_queries (parallel) → SearchAgent
         Data?    → delegate_data_analysis
                            Ontology on? → OntologyRouter progressively matches governed Skills
                                   match → DataInsightAgent loads Skill + governed SQL resource
                                   no match → deterministic question-driven OWL lookup
                                            → weak-result Ontology recovery when needed
                                            → deterministic UC candidate recall → Metadata verifier
                                            → DataInsightAgent loads sql-planning
                            Ontology off/fallback? → UC candidate recall → MetadataAgent loads metadata-mapping → DataInsightAgent
                    → Databricks SQL
  Schema?  → delegate_metadata       → MetadataAgent   → Unity Catalog
       ↓
Collect search refs in the request-local queue
       ↓
Synthesize answer with context
       ↓
Stream answer tokens immediately; MAF exits when no further Agent/tool call is requested
       ↓
Stream: thinking → text chunks → thinking_done → done
       ↓
Frontend: render answer + footnote citations
```

### Citation Pipeline

1. During `search_knowledge` / `search_multiple_queries`, document titles and URLs are collected into a `refs_map`
2. The backend citation pipeline (`_normalize_citations_and_references`) merges inline `[[n]](url)` marks with the collected map
3. Documents with no public URL are rendered as `[n] title` (plain text); linked documents as `[n] [title](url)`
4. The normalized answer is returned in final `done.content`
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
venv/bin/python -c "from src.config import validate_config; validate_config(); print('OK')"

# 2. Backend health
curl http://localhost:8000/health

# 3. Skills loaded
curl http://localhost:8000/skills

# 4. Search tool
venv/bin/python -c "
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
- Export logs through a deployment-specific pipeline for quality metrics; the repository does not currently register a Foundry exporter

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
| DataInsight/Metadata tool configuration error | Databricks not configured | Set required `DATABRICKS_*` variables |
| Frontend can't reach backend | CORS or wrong URL | Check `src/api/main.py` CORS origins; frontend uses port 8000 |

---

## 📚 Documentation Index

| File | Content |
|------|---------|
| `README.md` | Feature overview, project structure, quick start |
| `docs/SETUP.md` | Detailed setup with all config options |
| `ARCHITECTURE.md` | Component deep dive, data flows, extension points |
| `docs/DEPLOYMENT.md` | Production deployment to App Service / Container Apps |
| `docs/GET_STARTED.md` | This file — project snapshot and quick reference |
| `docs/AZURE_SEARCH_CONFIG.md` | Azure AI Search index configuration details |

---

**🎊 Your Enterprise Agentic RAG Chatbot is ready!**

```bash
./run.sh   # Start the full stack
```

React chat UI → `http://localhost:3000`  
