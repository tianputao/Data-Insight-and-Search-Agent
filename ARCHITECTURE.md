# Architecture Documentation

## 🏛️ System Architecture

### Overview

The Enterprise Agentic RAG Chatbot uses a **multi-agent orchestration pattern** built on the Microsoft Agent Framework (MAF). The system supports knowledge retrieval (Azure AI Search), read-only OWL business semantics (Owlready2), and structured data analytics (Azure Databricks Unity Catalog) through five agents, a plugin-based skill system, and a streaming FastAPI backend.

## 📊 Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                React + TypeScript Frontend (Vite, port 3000)     │
└──────────────────────────────┬───────────────────────────────────┘
                               │  SSE / REST  (FastAPI, port 8000)
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                FastAPI Backend  (src/api/main.py)                 │
│   POST /chat/stream   POST /threads/new   GET /skills  …         │
│   MAF FileSkillsSource discovery; per-request SSE stream         │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                  MasterAgent  (primary GPT deployment)           │
│  ┌──────────────────┐  ┌────────────────┐  ┌─────────────────┐  │
│  │ decompose_query  │  │ search_multiple│  │ search_knowledge│  │
│  │                  │  │ _queries       │  │                 │  │
│  └──────────────────┘  └────────────────┘  └─────────────────┘  │
│  ┌──────────────────┐  ┌────────────────┐                        │
│  │delegate_metadata │  │delegate_data   │  SkillsProvider scopes   │
│  │                  │  │_analysis       │  skills per sub-agent    │
│  └──────────────────┘  └────────────────┘                        │
└──────────────────────────────┬───────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────┐
│ Specialized agents                                               │
│ SearchAgent │ OntologyAgent │ MetadataAgent │ DataInsightAgent    │
│ Azure Search│ Owlready2     │ Unity Catalog │ Databricks SQL      │
│                                                                  │
│ Analytics: OntologyRouter? → OWL lookup → Metadata → DataInsight │
│ Ontology failure: visible fallback → MetadataAgent → DataInsight │
└──────────────────────────────┬───────────────────────────────────┘
                                     │
            ┌────────────────────┼──────────────────────┐
            ▼                    ▼                      ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────┐
│ Azure AI Search  │  │ Ontology/**/*.owl│  │ Databricks Unity     │
│ hybrid/semantic  │  │ read-only local  │  │ Catalog + SQL        │
└──────────────────┘  └──────────────────┘  └──────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────┐
│                      Azure Services                              │
│  ┌───────────────────┐  ┌───────────────────────────────────┐   │
│  │ Azure OpenAI      │  │  Azure Blob Storage               │   │
│  │ Azure OpenAI     │  │  Document URL resolution + SAS    │   │
│  │ text-embedding-   │  │  Image URL resolution + SAS       │   │
│  │   3-large (3072d) │  └───────────────────────────────────┘   │
│  └───────────────────┘  ┌───────────────────────────────────┐   │
│                          │  Azure AI Foundry                 │   │
│                          │  Optional external evaluation     │   │
│                          └───────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

## 🧩 Component Details

### 1. Frontend Layer

**React + TypeScript** (Vite, `frontend/`)
- Chat interface with token-by-token streaming
- "Thinking" step panel showing live agent reasoning
- Inline citation footnotes `[1]`, `[2]` with optional hyperlinks
- Per-session conversation threads via `/threads/new` REST call
- Per-session Ontology switch initialized from the backend environment default
- Workspace **Business Layer Doc** editor in the chat header

### 2. FastAPI Backend (`src/api/main.py`)

**Endpoints**:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat/stream` | SSE streaming chat (main endpoint) |
| `POST` | `/threads/new` | Create a new MAF conversation thread |
| `GET` | `/threads` | List all active threads |
| `GET` | `/threads/{id}/history` | Message history for a thread |
| `DELETE` | `/threads/{id}` | Delete a thread |
| `POST` | `/threads/{id}/stop` | Cancel the active run of one thread only |
| `GET` | `/business-layer` | Read the workspace business semantic document |
| `PUT` | `/business-layer` | Save the workspace business semantic document |
| `GET` | `/proxy-image` | Proxy Blob Storage images so the browser avoids CORS failures |
| `GET` | `/skills` | List registered skills |
| `GET` | `/config` | Non-sensitive runtime defaults and ontology capability status |
| `GET` | `/health` | Health check |

**SSE event types** streamed to frontend:

| Type | Payload | Meaning |
|------|---------|---------|
| `thinking` | `{"message": "..."}` | Agent reasoning step |
| `text` | `{"content": "..."}` | Response token chunk |
| `answer_reset` | `{}` | Retract text when a following tool call proves it was working narration |
| `done` | `{"content": "<normalized final answer>"}` | Stream complete; authoritative final answer |
| `stopped` | `{"message": "..."}` | Current thread task was cancelled by the user |
| `error` | `{"message": "..."}` | Error description |

**Citation pipeline**:
1. `search_knowledge` pushes reference evidence into the request-local internal queue
2. Backend `_extract_search_references` and `_normalize_citations_and_references` merge and format citations (preserving documents without a URL as plain-text footnotes)
3. The final normalized answer is returned in `done.content`; frontend `normalizeCitationsForDisplay()` renders `[n] [title](url)` (linked) or `[n] title` (plain)

**Lifecycle**: A single `MasterAgent` is created at startup, while each browser thread owns an isolated MAF `AgentSession`. Each user turn creates a request-local `QueryEngineContext`; `ContextVar` propagation keeps tool outcomes, search attempts, original intent, and the SSE sink isolated across concurrent sessions. Agent-scoped native `SkillsProvider` instances advertise and load repository Skills.

**Search loop termination**: After each retrieval, MasterAgent decides whether the accumulated evidence supports the original request and naturally ends the MAF function loop by answering without another tool call. `QUERY_ENGINE_MAX_SEARCH_ATTEMPTS` is only a hard ceiling (default `5`); request-local code also blocks equivalent query sets and stops after a retrieval adds no new document evidence. No separate LLM judge loop is used.

**Session isolation and concurrency**:
- Frontend messages, loading state, and `AbortController` are keyed by `thread_id`; switching sessions never redirects an in-flight stream into another session.
- Ontology mode is keyed by `thread_id` in the frontend and captured in each chat request; one session cannot change another session's workflow.
- Backend `active_runs` permits one active task per thread while different MAF `AgentSession` objects execute concurrently.
- `POST /threads/{thread_id}/stop` cancels only that thread's MAF run and signals cooperative cancellation to delegated-agent worker waits.

**Memory and cache**:
- MAF `AgentSession` retains conversation history for contextual follow-up questions within one thread. A new thread starts with a separate history and memory state.
- The application also keeps a process-local, thread-scoped exact response cache. A normalized repeated question within `SESSION_RESPONSE_CACHE_TTL_SECONDS` returns the completed answer only when its ontology mode also matches.
- Cache entries never cross thread boundaries and are cleared when the backend process restarts or the thread is deleted.

### 3. MasterAgent (`src/agents/master_agent.py`)

**Framework**: MAF 1.11 `OpenAIChatCompletionClient.as_agent()` with in-memory `AgentSession`

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
| `delegate_data_analysis` | Runs progressive Skill routing first in enabled mode; governed analytics can skip Metadata, while non-Skill requests use property-first Ontology → physical Metadata verification → DataInsight |

**Input routing logic**: MasterAgent uses `MASTER_AGENT_PROMPT` to decide which tool to call. Questions requiring data analytics or schema discovery are delegated; knowledge questions flow through `search_knowledge` / `search_multiple_queries`. Databricks Skills are advertised only inside their assigned sub-agents.

**Agentic loop ownership**: A user turn invokes `MasterAgent` exactly once. Its MAF `OpenAIChatCompletionClient` owns the bounded function-invocation loop: it streams a model response, executes requested Agent/tools, appends each function result as an observation, and calls the model again. The loop exits when the model emits no further function call, reaches the configured model-roundtrip/function-call limit, or reaches the consecutive-error limit. There is no second answer judge, hidden feedback turn, or fixed 180-second MasterAgent timeout.

This matches the central Claude QueryEngine control path while retaining MAF's native function-call protocol. Request-local `ToolOutcome` records are observability data only; they do not trigger a second `agent.run()`.

### 4. SearchAgent (`src/agents/search_agent.py`)

**Tools**: `search_knowledge_base` (single query), `parallel_search` (multi-query concurrent)

**Search modes** (configured when the backend creates SearchAgent):
- **Hybrid** (always on): vector similarity (text-embedding-3-large) + BM25 keyword
- **Semantic reranking** (`DEFAULT_ENABLE_SEMANTIC_RERANKER`): Azure semantic reranker; restart after changing the environment value
- **Agentic retrieval** (`DEFAULT_ENABLE_AGENTIC_RETRIEVAL`): Azure-managed query planning; restart after changing the environment value

**Index schema** (`index-dev-figure-01-chunk`):
- Core fields: `id`, `session_id`, `content`, `title`, `filepath`, `url`, `metadata`, `doc_metadata`, `description`
- Vector fields: `contentVector` (3072d), `full_metadata_vector`
- Document metadata: `main_title`, `sub_title`, `publisher`, `document_code`, `document_category`, `document_schema`
- Language fields: `primary_language`, `secondary_language`, localized title fields
- Header hierarchy: `full_headers`, `h1`–`h6`
- Timestamps: `timestamp`, `publish_date`
- Image mapping: `image_mapping` (resolved to Azure Blob URLs with SAS token)

### 5. OntologyAgent (`src/agents/ontology_agent.py`)

**Backend**: Owlready2 with a dedicated in-memory `World`. The service recursively loads `Ontology/**/*.owl` in read-only mode and builds normalized entity and graph indexes. HermiT startup reasoning is disabled by default; enable it with `ONTOLOGY_ENABLE_REASONER=true` and Java 11+.

**Runtime path**: `OntologyRouter` uses the primary model only to evaluate the progressively disclosed `analytics-spec` governed Skill. If no governed route matches, code calls `get_business_context` and `list_defined_classes` directly. A non-`ok` result or confidence below `ONTOLOGY_ESCALATION_MIN_CONFIDENCE` escalates to the full OntologyAgent tool loop. This keeps normal semantic lookup deterministic while preserving model-driven recovery for weak results.

**Tools**: `search_entities`, `describe_entity`, `expand_neighbors`, `find_paths`, `find_related_by_type`, `get_schema_mapping`, `get_join_paths`, `get_lineage`, `get_semantic_candidates`, `list_defined_classes`, and `get_business_context`.

`describe_entity` also returns explicit `disjoint_with` classes and non-label annotation values, and `list_defined_classes` enumerates equivalentClass-defined business concepts (for example a threshold-defined order class) together with their rendered definitions.

Entity resolution combines exact IRI/name, multilingual labels, normalized tokens, type constraints, and fuzzy candidates. Query tools return stable JSON envelopes with evidence, confidence, attempted strategies, warnings, and unresolved concepts. Physical joins remain empty unless an OWL annotation explicitly grounds them.

`OntologyService` is deliberately analysis-neutral. It returns role-neutral OWL properties with labels, comments, domains/ranges, class hierarchy and restrictions, plus ordered semantic relationships and paths. It does not select measures or dimensions and does not define baselines, decompositions, or SQL formulas. Business aliases belong in the OWL rather than Python lookup dictionaries.

HermiT may reject ontology datatypes outside its OWL 2 datatype map. A reasoner failure is exposed as capability status while asserted OWL facts remain queryable. If OntologyAgent still cannot produce usable context for a request, MasterAgent emits a visible fallback activity and continues with the ordinary metadata-driven workflow.

### 6. DataInsightAgent (`src/agents/data_insight_agent.py`)

**Backend**: Azure Databricks SQL Warehouse via `databricks-sql-connector`; Unity Catalog metadata is supplied by MetadataAgent.

**Tools**:

| Tool | Description |
|------|-------------|
| `execute_sql` | Runs a SQL query against the Databricks SQL Warehouse; returns rows as JSON |
| `recover_metadata_context` | Exceptional, once-per-request MetadataAgent recovery when the schema handoff is missing or incomplete |
| `recover_ontology_context` | Exceptional, once-per-request OntologyAgent recovery when ontology was enabled but context is unexpectedly missing |
| Native Skills | `SkillsProvider` advertises governed templates plus `sql-planning` on demand |

**Configuration**: `DatabricksConfig` — `HOST`, `TOKEN`, `HTTP_PATH`, `CATALOG`, `SCHEMAS` (comma-separated list), `MAX_ROWS`, `QUERY_TIMEOUT`. The agent is instantiated at startup; its SQL tool returns a configuration error when `DatabricksConfig.is_configured()` is false.

### 7. MetadataAgent (`src/agents/metadata_agent.py`)

**Backend**: Azure Databricks Unity Catalog via `databricks-sdk`

**Tools**:

| Tool | Description |
|------|-------------|
| `list_schemas` | Lists all schemas in the configured Unity Catalog |
| `list_tables` | Lists tables within a schema |
| `get_table_details` | Returns column names, types, and comments for a table |
| `search_tables` | Fuzzy-matches table names by keyword |
| Native Skill | `SkillsProvider` advertises and loads `metadata-mapping` on demand |

MetadataAgent remains necessary after adding OntologyAgent: ontology semantics identify business concepts and paths first, while Unity Catalog is the authority for executable table names, columns, keys, join cardinality, and availability. MasterAgent owns the canonical Ontology artifact and passes it directly to DataInsightAgent; MetadataAgent receives a bounded verification projection and cannot replace or discard the semantic context.

For analytics, code first lists cached table summaries (bounded by `METADATA_INDEX_MAX_TABLES`), scores them against the question and ontology terms, and batch-fetches up to `METADATA_CANDIDATE_MAX_TABLES` details with a bounded thread pool. Identifier-based join closure may add bridge tables. When recall finds nothing, schemas no larger than `METADATA_SNAPSHOT_MAX_TABLES` use a complete snapshot; larger schemas leave discovery to the MetadataAgent tools. The snapshot is a candidate subset, so the model can still call `search_tables` or `get_table_details` to close a named gap.

MetadataAgent then performs one discovery/verification turn: ontology-disabled discovery progressively loads `metadata-mapping`; ontology-enabled verification has no Skill provider. It returns selected tables, verified joins, Skill-only business mappings, rejected candidates, and unresolved terms without copying raw columns. DataInsightAgent receives that decision JSON plus every authoritative raw UC payload. Process-local TTL caches are keyed by catalog, schema table list, and fully qualified table detail.

### 8. Skill System

**SkillsProvider factory** (`src/skills_provider.py`): Creates agent-scoped native MAF providers backed by `FileSkillsSource`. OntologyRouter receives governed template routing Skills; DataInsightAgent receives those governed Skills plus `sql-planning`; Metadata discovery receives `metadata-mapping`; Ontology recovery, Metadata verification, MasterAgent, and SearchAgent receive no data Skills.

MAF applies progressive disclosure: advertise Skill metadata, load `SKILL.md` on demand, then optionally read resources or execute approval-gated scripts.

**Current skills**:

| Skill | Purpose |
|-------|---------|
| `analytics-spec` | Governed highest-spending-customer SQL template and matching contract |
| `sql-planning` | Dynamic planning and SQL engineering method for every non-governed query; uses OWL when available and verified UC in all modes |
| `metadata-mapping` | Unity Catalog metadata field mapping rules |

### 9. Business Semantic Layer (`src/business_layer.py`)

A workspace-level document that business users author in the UI (**Business Layer Doc** in the chat header) to record terminology, metric definitions, and reporting conventions that the OWL ontology does not define.

- **Storage**: a plain data file at `data/business_layer.md`, never imported or executed as code. `load_business_layer()` / `save_business_layer()` are the only entry points, so moving to Blob or a database changes just those two functions.
- **Delivery**: `chat_stream` reads the document on every request and threads it through `QueryEngineContext` to DataInsightAgent, which wraps it as `<business_layer_context>`. Because it is read per request rather than baked into a system prompt, edits take effect on the next question with no restart.
- **Precedence**: advisory only. It ranks below `<schema_context>`, cannot override verified Unity Catalog objects, and is treated as reference data — the prompt instructs the model to ignore any embedded instruction, and the SELECT-only plus catalog/schema guards in `execute_sql` remain the enforcing control.
- **Scope**: applies whether Ontology is enabled or disabled, and is empty (block omitted entirely) until someone saves content.

### 10. AzureAISearchTool (`src/tools/ai_search_tool.py`)

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
MasterAgent.chat_stream()  (one main AgentSession run)
      ↓
  ┌──────────────────────────────┐
      │ Normalize/correct/enrich      │
      │ Simple question?             │  → search_knowledge (single SearchAgent call)
      │ Complex/multi-part question? │  → decompose_query → search_multiple_queries
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
MasterAgent: collect references in the request-local queue
      ↓
MasterAgent: synthesize answer with retrieved context
      ↓
Stream answer tokens immediately
      └── if a later tool call follows, send answer_reset and classify prior text as narration
      ↓
SSE stream: thinking → text chunks → thinking_done → done
      ↓
Frontend: render answer + citations
```

### Data Analytics Flow

```
User Question (analytics intent detected)
      ↓
MasterAgent → delegate_data_analysis()
      ↓
Ontology enabled for this session?
  ├── No  → MetadataAgent resolves question-relevant UC objects
      └── Yes → OntologyAgent resolves role-neutral properties, restrictions, semantic paths, and lineage
              ├── success → MetadataAgent verifies the ontology-derived physical candidates
              └── failure → emit visible fallback, then run ordinary MetadataAgent lookup
      └── deterministic recall batch-fetches candidates; Metadata tools may close named gaps
      ↓
DataInsightAgent receives canonical ontology evidence (when available) plus independent authoritative schema context and reconciles both before SQL
      ↓
The workspace business layer document, when non-empty, is attached as <business_layer_context> in both ontology modes
      ↓
DataInsightAgent.query_stream()
      ├── governed route → load template Skill + indexed SQL resource
      ├── ordinary ontology route → load_skill("sql-planning")
      │      └── primary model dynamically selects metric, grain, comparisons, and SQL
      ├── recover_* only if an expected context handoff is missing/incomplete
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
      ├── AZURE_BLOB_BASE_URL + AZURE_BLOB_SAS_TOKEN (document Blob Storage)
  ├── IMAGE_BASE_URL + IMAGE_SAS_TOKEN (image Blob Storage)
  └── SEMANTIC_CONFIG_NAME, VECTOR_SEARCH_PROFILE

AzureAIFoundryConfig
  └── CONNECTION_STRING

DatabricksConfig
  ├── HOST, TOKEN, HTTP_PATH
  ├── CATALOG, SCHEMAS (comma-separated list)
      ├── MAX_ROWS, QUERY_TIMEOUT, METADATA_CACHE_TTL_SECONDS
      ├── METADATA_INDEX_MAX_TABLES, METADATA_CANDIDATE_MAX_TABLES
      ├── METADATA_SNAPSHOT_MAX_TABLES
  └── is_configured() → bool

OntologyConfig
      ├── DIRECTORY, FILE_GLOB, ONLY_LOCAL
      ├── ENABLE_REASONER, REASONER
      └── query limits, FUZZY_THRESHOLD, ESCALATION_MIN_CONFIDENCE, AGENT_TIMEOUT_SECONDS

AppConfig
      ├── LOG_LEVEL, MAX_SEARCH_RESULTS
  ├── DEFAULT_ENABLE_SEMANTIC_RERANKER
  ├── DEFAULT_ENABLE_AGENTIC_RETRIEVAL
      ├── DEFAULT_ENABLE_ONTOLOGY
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

### External evaluation
- `AZURE_AI_PROJECT_CONNECTION_STRING` is retained as a configuration placeholder; this repository does not currently register a Foundry or Application Insights exporter.
- Rotating logs can be exported by a deployment-specific pipeline for groundedness, relevance, and coherence evaluation.
- Feature flags support A/B testing semantic reranker and agentic retrieval configurations.

## 🔄 Extension Points

### Adding a New Agent

1. Create `src/agents/my_agent.py` implementing `_create_tools()` and `_create_agent()`
2. Add to `src/agents/__init__.py`
3. Create a corresponding `delegate_my_agent` tool in `MasterAgent._create_tools()`
4. Instantiate and pass into `MasterAgent.__init__()` in `src/api/main.py`

### Adding a New Skill

1. Create `skills/my-skill/SKILL.md` with YAML frontmatter (`name`, `description`, `tags`)
2. Assign the directory name to the intended agent in `src/skills_provider.py`
3. MAF `SkillsProvider` advertises it on the next startup and loads it on demand

### Adding New Search Index Fields

1. Add the field constant to `AzureSearchConfig` in `src/config/settings.py`
2. Add the `.env` override key (e.g. `AZURE_SEARCH_MY_FIELD=my_field`)
3. Add the field to `get_select_fields()` if it should appear in results

## 🎯 Design Principles

1. **Modularity**: Agents, tools, prompts, config, and skills are fully separated
2. **Streaming-first**: User-visible progress and answers flow through SSE; delegated workers use bounded waits and cooperative cancellation
3. **Configurable auth**: `AUTH_MODE` supports both API key and AAD without code changes
4. **Skill injection**: Domain expertise is externalized to `skills/` Markdown files
5. **Citation integrity**: References preserved even when no public URL is available
6. **Progressive enhancement**: DataInsight and Metadata agents are optional; system works without Databricks

---

**For implementation details, see source code in `src/`.**
