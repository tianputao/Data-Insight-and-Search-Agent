# Architecture Documentation

## 🏛️ System Architecture

### Overview

The Enterprise Agentic RAG Chatbot uses a **multi-agent orchestration pattern** built on the Microsoft Agent Framework (MAF). The system supports both knowledge retrieval (Azure AI Search) and structured data analytics (Azure Databricks Unity Catalog) through four specialized agents, a plugin-based skill system, and a streaming FastAPI backend.

## 📊 Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                React + TypeScript Frontend (Vite, port 3000)     │
│              OR  Streamlit App (app.py, port 8501 — RAG only)    │
└──────────────────────────────┬───────────────────────────────────┘
                               │  SSE / REST  (FastAPI, port 8000)
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                FastAPI Backend  (src/api/main.py)                 │
│   POST /chat/stream   POST /threads/new   GET /skills  …         │
│   SkillRegistry.scan() at startup; per-request SSE stream        │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                       MasterAgent  (GPT-5.1)                     │
│  ┌──────────────────┐  ┌────────────────┐  ┌─────────────────┐  │
│  │ decompose_query  │  │ search_multiple│  │ search_knowledge│  │
│  │                  │  │ _queries       │  │                 │  │
│  └──────────────────┘  └────────────────┘  └─────────────────┘  │
│  ┌──────────────────┐  ┌────────────────┐                        │
│  │delegate_metadata │  │delegate_data   │  SkillInjector injects  │
│  │                  │  │_insight        │  skills XML into prompt │
│  └──────────────────┘  └────────────────┘                        │
└────┬─────────────────┬────────────────────┬───────────────────────┘
     │                 │                    │
     ▼                 ▼                    ▼
┌──────────┐  ┌────────────────┐  ┌────────────────────────┐
│ Search   │  │ MetadataAgent  │  │  DataInsightAgent       │
│ Agent    │  │                │  │                         │
│          │  │ list_schemas   │  │ get_relevant_tables     │
│ search_  │  │ list_tables    │  │ execute_sql             │
│ knowledge│  │ get_table_     │  │ load_skill              │
│ _base    │  │ _details       │  │                         │
│ parallel │  │ search_tables  │  └──────────┬──────────────┘
│ _search  │  │ load_skill     │             │
└────┬─────┘  └───────┬────────┘             │
     │                │                      │
     ▼                ▼                      ▼
┌────────────────┐  ┌──────────────────────────────────────────┐
│ Azure AI Search│  │  Azure Databricks Unity Catalog           │
│ index-dev-     │  │  (SQL Warehouse via JDBC)                │
│ figure-01-chunk│  │  Catalog: configurable                   │
│ Hybrid + Semantic│  │  Schemas: configurable (e.g. silver,gold)│
│ + Agentic mode │  └──────────────────────────────────────────┘
└────────┬───────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────┐
│                      Azure Services                              │
│  ┌───────────────────┐  ┌───────────────────────────────────┐   │
│  │ Azure OpenAI      │  │  Azure Blob Storage               │   │
│  │ GPT-5.1 (LLM)    │  │  Document URL resolution + SAS    │   │
│  │ text-embedding-   │  │  Image URL resolution + SAS       │   │
│  │   3-large (3072d) │  └───────────────────────────────────┘   │
│  └───────────────────┘  ┌───────────────────────────────────┐   │
│                          │  Azure AI Foundry                 │   │
│                          │  Monitoring & Evaluation          │   │
│                          └───────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

## 🧩 Component Details

### 1. Frontend Layer

**Primary UI** — React + TypeScript (Vite, `frontend/`)
- Chat interface with token-by-token streaming
- "Thinking" step panel showing live agent reasoning
- Inline citation footnotes `[1]`, `[2]` with optional hyperlinks
- Per-session conversation threads via `/threads/new` REST call

**Secondary UI** — Streamlit (`app.py`)
- Standalone single-page app, requires no Node.js
- Supports RAG (SearchAgent) only; no Databricks agents
- Useful for quick local testing

### 2. FastAPI Backend (`src/api/main.py`)

**Endpoints**:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat/stream` | SSE streaming chat (main endpoint) |
| `POST` | `/threads/new` | Create a new MAF conversation thread |
| `GET` | `/threads` | List all active threads |
| `GET` | `/threads/{id}/history` | Message history for a thread |
| `DELETE` | `/threads/{id}` | Delete a thread |
| `GET` | `/skills` | List registered skills |
| `GET` | `/health` | Health check |

**SSE event types** streamed to frontend:

| Type | Payload | Meaning |
|------|---------|---------|
| `thinking` | `{"message": "..."}` | Agent reasoning step |
| `text` | `{"content": "..."}` | Response token chunk |
| `refs` | `{num: [title, url]}` | Citation map from tool calls |
| `done` | — | Stream complete |
| `error` | `{"message": "..."}` | Error description |

**Citation pipeline**:
1. `search_knowledge` tool pushes a `refs` dict to the SSE queue on every search call
2. Backend `_extract_search_references` and `_normalize_citations_and_references` merge and format citations (preserving documents without a URL as plain-text footnotes)
3. Frontend `normalizeCitationsForDisplay()` renders `[n] [title](url)` (linked) or `[n] title` (plain)

**Lifecycle**: A single `MasterAgent` is created at startup. The `SkillRegistry` scans `skills/` once on startup and injects skill metadata XML into agent prompts.

### 3. MasterAgent (`src/agents/master_agent.py`)

**Framework**: MAF `AzureOpenAIChatClient.as_agent()` with in-memory thread store

**Auth**: Controlled by `AzureOpenAIConfig.use_api_key()`:
- `AZURE_OPENAI_AUTH_MODE=key` → API key
- `AZURE_OPENAI_AUTH_MODE=aad` → `DefaultAzureCredential`
- `AZURE_OPENAI_AUTH_MODE=auto` (default) → key if `AZURE_OPENAI_API_KEY` is set, else AAD

**Tools registered**:

| Tool | Description |
|------|-------------|
| `decompose_query` | Breaks complex questions into N focused sub-queries using LLM |
| `search_multiple_queries` | Executes a list of sub-queries via SearchAgent in parallel |
| `search_knowledge` | Executes a single search and pushes citation refs to the SSE stream |
| `delegate_metadata` | Streams a Unity Catalog schema question to MetadataAgent |
| `delegate_data_insight` | Streams a data analytics question to DataInsightAgent |

**Input routing logic**: MasterAgent uses `MASTER_AGENT_PROMPT` (with injected skill XML) to decide which tool to call. Questions requiring data analytics or schema discovery are delegated; knowledge questions flow through `search_knowledge` / `search_multiple_queries`.

### 4. SearchAgent (`src/agents/search_agent.py`)

**Tools**: `search_knowledge_base` (single query), `parallel_search` (multi-query concurrent)

**Search modes** (toggled per request):
- **Hybrid** (always on): vector similarity (text-embedding-3-large) + BM25 keyword
- **Semantic reranking** (`DEFAULT_ENABLE_SEMANTIC_RERANKER`): Azure semantic reranker
- **Agentic retrieval** (`DEFAULT_ENABLE_AGENTIC_RETRIEVAL`): Azure-managed query planning

**Index schema** (`index-dev-figure-01-chunk`):
- Core fields: `id`, `session_id`, `content`, `title`, `filepath`, `url`, `metadata`, `doc_metadata`, `description`
- Vector fields: `contentVector` (3072d), `full_metadata_vector`
- Document metadata: `main_title`, `sub_title`, `publisher`, `document_code`, `document_category`, `document_schema`
- Language fields: `primary_language`, `secondary_language`, localized title fields
- Header hierarchy: `full_headers`, `h1`–`h6`
- Timestamps: `timestamp`, `publish_date`
- Image mapping: `image_mapping` (resolved to Azure Blob URLs with SAS token)

### 5. DataInsightAgent (`src/agents/data_insight_agent.py`)

**Backend**: Azure Databricks Unity Catalog via JDBC (databricks-sql-connector)

**Tools**:

| Tool | Description |
|------|-------------|
| `get_relevant_tables` | Lists tables in the Unity Catalog schema matching a topic |
| `execute_sql` | Runs a SQL query against the Databricks SQL Warehouse; returns rows as JSON |
| `load_skill` | Loads full body of a named skill from the SkillRegistry |

**Configuration**: `DatabricksConfig` — `HOST`, `TOKEN`, `HTTP_PATH`, `CATALOG`, `SCHEMAS` (comma-separated list), `MAX_ROWS`, `QUERY_TIMEOUT`. The agent is only instantiated when `DatabricksConfig.is_configured()` returns `True`.

### 6. MetadataAgent (`src/agents/metadata_agent.py`)

**Backend**: Azure Databricks Unity Catalog via JDBC

**Tools**:

| Tool | Description |
|------|-------------|
| `list_schemas` | Lists all schemas in the configured Unity Catalog |
| `list_tables` | Lists tables within a schema |
| `get_table_details` | Returns column names, types, and comments for a table |
| `search_tables` | Fuzzy-matches table names by keyword |
| `load_skill` | Loads a skill body from the SkillRegistry |

### 7. Skill System

**SkillRegistry** (`src/registry.py`): Scans all `skills/*/SKILL.md` files at startup; registers `SkillMeta` objects (name, description, tags, body).

**SkillInjector** (`src/injector.py`): Builds an XML block listing available skills and injects it into agent system prompts. On demand, `load_skill_full_body(name)` returns the complete SKILL.md content (logged with char count and SHA256 for verification).

**Current skills**:

| Skill | Purpose |
|-------|---------|
| `analytics-spec` | Data analytics query patterns and conventions |
| `metadata-mapping` | Unity Catalog metadata field mapping rules |

### 8. AzureAISearchTool (`src/tools/ai_search_tool.py`)

- `search()`: Main async search entrypoint — dispatches to `_search_standard` or `_search_with_agentic_mode`
- `_search_standard`: Builds `VectorizedQuery` + `SearchOptions`; supports hybrid + semantic reranking
- `_search_with_agentic_mode`: Uses Azure AI Search agentic retrieval API
- `parallel_search()` / `parallel_search_sync()`: Async and sync parallel multi-query execution
- `_ensure_blob_sas_url()`: Appends SAS token to Blob Storage document/image URLs
- `_process_image_mapping()`: Expands `image_mapping` field to full image URLs with SAS

## 🔄 Data Flow

### Knowledge Q&A Flow

```
User Question
      ↓
[FastAPI POST /chat/stream]
      ↓
MasterAgent.chat_stream()  (MAF thread)
      ↓
  ┌──────────────────────────────┐
  │ Simple question?             │  → search_knowledge (single SearchAgent call)
  │ Complex question?            │  → decompose_query → search_multiple_queries
  │ Agentic retrieval ON?        │  → search_knowledge (agentic mode in search tool)
  └──────────────────────────────┘
      ↓
SearchAgent.search_knowledge_base()
      ↓
AzureAISearchTool.search()
  ├── Vectorize query (text-embedding-3-large)
  ├── Hybrid search (vector + BM25)
  ├── Semantic reranking (optional)
  └── Return top-K results with titles, URLs, content
      ↓
MasterAgent: push "refs" event to SSE queue
      ↓
MasterAgent: synthesize answer with retrieved context
      ↓
SSE stream: thinking → text chunks → refs → done
      ↓
Frontend: render answer + citations
```

### Data Analytics Flow

```
User Question (analytics intent detected)
      ↓
MasterAgent → delegate_data_insight()
      ↓
DataInsightAgent.query_stream()
  ├── load_skill("analytics-spec")  → injects query conventions
  ├── get_relevant_tables(topic)    → Unity Catalog table list
  ├── execute_sql(sql)              → Databricks SQL Warehouse
  └── Stream results back
      ↓
MasterAgent: relay thinking + text events to SSE queue
      ↓
Frontend: render tabular / prose summary
```

## 🎛️ Configuration Architecture

All configuration is centralized in `src/config/settings.py` and loaded from `.env`:

```
AzureOpenAIConfig
  ├── ENDPOINT, API_KEY, AUTH_MODE (auto|key|aad)
  ├── API_VERSION, GPT_DEPLOYMENT
  └── EMBEDDING_DEPLOYMENT, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS

AzureSearchConfig
  ├── ENDPOINT, API_KEY, INDEX_NAME
  ├── 30+ field name mappings (ID_FIELD, CONTENT_FIELD, VECTOR_FIELD, …)
  ├── BASE_URL + SAS_TOKEN (document Blob Storage)
  ├── IMAGE_BASE_URL + IMAGE_SAS_TOKEN (image Blob Storage)
  └── SEMANTIC_CONFIG_NAME, VECTOR_SEARCH_PROFILE

AzureAIFoundryConfig
  └── CONNECTION_STRING

DatabricksConfig
  ├── HOST, TOKEN, HTTP_PATH
  ├── CATALOG, SCHEMAS (comma-separated list)
  ├── MAX_ROWS, QUERY_TIMEOUT
  └── is_configured() → bool

AppConfig
  ├── LOG_LEVEL, MAX_SEARCH_RESULTS, DEFAULT_TOP_K
  ├── DEFAULT_ENABLE_SEMANTIC_RERANKER
  ├── DEFAULT_ENABLE_AGENTIC_RETRIEVAL
  └── LOG_DIR, TMP_DIR, DATA_DIR
```

`validate_config()` raises `ValueError` for missing required variables; `AZURE_OPENAI_API_KEY` is only required when `use_api_key()` returns `True`.

## 🔒 Security Architecture

### Authentication

```
AZURE_OPENAI_AUTH_MODE = key   →  API Key (from .env)
AZURE_OPENAI_AUTH_MODE = aad   →  DefaultAzureCredential (Entra ID)
AZURE_OPENAI_AUTH_MODE = auto  →  key if API_KEY set, else AAD
```

For AAD mode, the running identity needs the *Cognitive Services OpenAI User* role on the Azure OpenAI resource.

### Best Practices
- All secrets in `.env` only (`.gitignore`-d)
- No credentials in source code or logs
- Databricks PAT scoped by Unity Catalog RBAC
- Blob Storage access via time-limited SAS tokens

## 📊 Monitoring & Observability

### Logging
- File: `logs/application_YYYYMMDD.log`
- Content: agent decisions, tool calls, SQL queries, search queries, citation collection, errors, startup events
- Level controlled by `LOG_LEVEL` env var

### Azure AI Foundry
- Connects via `AZURE_AI_PROJECT_CONNECTION_STRING`
- Use exported logs for groundedness, relevance, coherence evaluation
- A/B test semantic reranker and agentic retrieval configurations

## 🔄 Extension Points

### Adding a New Agent

1. Create `src/agents/my_agent.py` implementing `_create_tools()` and `_create_agent()`
2. Add to `src/agents/__init__.py`
3. Create a corresponding `delegate_my_agent` tool in `MasterAgent._create_tools()`
4. Instantiate and pass into `MasterAgent.__init__()` in `src/api/main.py`

### Adding a New Skill

1. Create `skills/my-skill/SKILL.md` with YAML frontmatter (`name`, `description`, `tags`)
2. The `SkillRegistry` picks it up automatically on next startup
3. Agents with a `load_skill` tool can retrieve its full body at runtime

### Adding New Search Index Fields

1. Add the field constant to `AzureSearchConfig` in `src/config/settings.py`
2. Add the `.env` override key (e.g. `AZURE_SEARCH_MY_FIELD=my_field`)
3. Add the field to `get_select_fields()` if it should appear in results

## 🎯 Design Principles

1. **Modularity**: Agents, tools, prompts, config, and skills are fully separated
2. **Streaming-first**: All agent responses flow through an SSE queue; no blocking waits
3. **Configurable auth**: `AUTH_MODE` supports both API key and AAD without code changes
4. **Skill injection**: Domain expertise is externalized to `skills/` Markdown files
5. **Citation integrity**: References preserved even when no public URL is available
6. **Progressive enhancement**: DataInsight and Metadata agents are optional; system works without Databricks

---

**For implementation details, see source code in `src/`.**
