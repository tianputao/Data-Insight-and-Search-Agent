# Azure AI Search Index Configuration Guide

## Current Index Schema: `index-dev-figure-01-chunk`

This document describes the Azure AI Search index schema the application is configured to work with, and explains how each configuration value maps to source code and environment variables.

### Index Overview

| Property | Value |
|----------|-------|
| **Index name** | `index-dev-figure-01-chunk` (set via `AZURE_SEARCH_INDEX_NAME`) |
| **Vector dimensions** | **3072** (text-embedding-3-large) |
| **Semantic configuration** | `default` (set via `AZURE_SEARCH_SEMANTIC_CONFIG`) |
| **Vector search profile** | `vectorSearchProfile` (set via `AZURE_SEARCH_VECTOR_PROFILE`) |

> **Important**: The embedding model is **text-embedding-3-large** (3072 dimensions). The code and `.env` must use `AZURE_OPENAI_EMBEDDING_DIMENSIONS=3072`. Using text-embedding-ada-002 (1536d) with this index will cause dimension mismatch errors.

---

## Field Mapping

All field names are configurable via `AZURE_SEARCH_*_FIELD` environment variables. The defaults below match the production index schema.

### Core Fields

| Env variable | Default value | Description |
|---|---|---|
| `AZURE_SEARCH_ID_FIELD` | `id` | Document key (Edm.String) |
| `AZURE_SEARCH_SESSION_ID_FIELD` | `session_id` | Session identifier |
| `AZURE_SEARCH_CONTENT_FIELD` | `content` | Main chunk text (searchable) |
| `AZURE_SEARCH_TITLE_FIELD` | `title` | Chunk-level title (searchable) |
| `AZURE_SEARCH_FILEPATH_FIELD` | `filepath` | Relative path inside blob container |
| `AZURE_SEARCH_URL_FIELD` | `url` | Public URL (may be empty; resolved with SAS token) |
| `AZURE_SEARCH_METADATA_FIELD` | `metadata` | Raw metadata JSON |
| `AZURE_SEARCH_DOC_METADATA_FIELD` | `doc_metadata` | Structured document metadata |
| `AZURE_SEARCH_DESCRIPTION_FIELD` | `description` | Short document description |

### Vector Fields

| Env variable | Default value | Dimensions | Description |
|---|---|---|---|
| `AZURE_SEARCH_VECTOR_FIELD` | `contentVector` | 3072 | Content embedding (text-embedding-3-large) |
| `AZURE_SEARCH_METADATA_VECTOR_FIELD` | `full_metadata_vector` | 3072 | Metadata embedding |

### Document Metadata Fields

| Env variable | Default value | Description |
|---|---|---|
| `AZURE_SEARCH_MAIN_TITLE_FIELD` | `main_title` | Top-level document title |
| `AZURE_SEARCH_SUB_TITLE_FIELD` | `sub_title` | Section or chapter title |
| `AZURE_SEARCH_PUBLISHER_FIELD` | `publisher` | Publishing organization |
| `AZURE_SEARCH_DOCUMENT_CODE_FIELD` | `document_code` | Standard or regulation number |
| `AZURE_SEARCH_DOCUMENT_CATEGORY_FIELD` | `document_category` | Document type/category |
| `AZURE_SEARCH_DOCUMENT_SCHEMA_FIELD` | `document_schema` | Schema version identifier |

### Language Fields

| Env variable | Default value | Description |
|---|---|---|
| `AZURE_SEARCH_PRIMARY_LANGUAGE_FIELD` | `primary_language` | Primary language code (e.g. `zh`, `en`) |
| `AZURE_SEARCH_SECONDARY_LANGUAGE_FIELD` | `secondary_language` | Secondary language code |
| `AZURE_SEARCH_MAIN_TITLE_SEC_LANGUAGE_FIELD` | `main_title_sec_language` | Main title in secondary language |
| `AZURE_SEARCH_SUB_TITLE_SEC_LANGUAGE_FIELD` | `sub_title_sec_language` | Sub-title in secondary language |

### Header Hierarchy Fields

| Env variable | Default value | Description |
|---|---|---|
| `AZURE_SEARCH_FULL_HEADERS_FIELD` | `full_headers` | Full breadcrumb path of headings |
| `AZURE_SEARCH_H1_FIELD` | `h1` | Heading level 1 |
| `AZURE_SEARCH_H2_FIELD` | `h2` | Heading level 2 |
| `AZURE_SEARCH_H3_FIELD` | `h3` | Heading level 3 |
| `AZURE_SEARCH_H4_FIELD` | `h4` | Heading level 4 |
| `AZURE_SEARCH_H5_FIELD` | `h5` | Heading level 5 |
| `AZURE_SEARCH_H6_FIELD` | `h6` | Heading level 6 |

### Timestamp Fields

| Env variable | Default value | Description |
|---|---|---|
| `AZURE_SEARCH_TIMESTAMP_FIELD` | `timestamp` | Indexing timestamp |
| `AZURE_SEARCH_PUBLISH_DATE_FIELD` | `publish_date` | Original document publish date |

### Other Fields

| Env variable | Default value | Description |
|---|---|---|
| `AZURE_SEARCH_IMAGE_MAPPING_FIELD` | `image_mapping` | JSON mapping of image filenames to page numbers; resolved to Blob Storage URLs with SAS token at query time |

---

## Search Configuration

### Semantic Search

Create a semantic configuration named `default` on your index (or set `AZURE_SEARCH_SEMANTIC_CONFIG` to a different name):

```json
{
  "name": "default",
  "prioritizedFields": {
    "titleField": { "fieldName": "title" },
    "prioritizedContentFields": [
      { "fieldName": "content" }
    ],
    "prioritizedKeywordsFields": [
      { "fieldName": "full_headers" },
      { "fieldName": "doc_metadata" }
    ]
  }
}
```

Semantic reranking is toggled at runtime via `DEFAULT_ENABLE_SEMANTIC_RERANKER` (default `true`). When enabled, the search tool sets `query_type = QueryType.SEMANTIC` and passes `semantic_configuration_name`.

### Vector Search

The index must have an HNSW vector search algorithm and a profile named `vectorSearchProfile` (or override via `AZURE_SEARCH_VECTOR_PROFILE`):

```json
{
  "algorithms": [
    {
      "name": "my-hnsw-config-1",
      "kind": "hnsw",
      "hnswParameters": {
        "metric": "cosine",
        "m": 4,
        "efConstruction": 400,
        "efSearch": 500
      }
    }
  ],
  "profiles": [
    {
      "name": "vectorSearchProfile",
      "algorithm": "my-hnsw-config-1"
    }
  ]
}
```

The search tool constructs a `VectorizedQuery` over `contentVector` with `k=50` candidates before reranking, then returns the top `DEFAULT_TOP_K` results (default `20`).

### Agentic Retrieval

When `DEFAULT_ENABLE_AGENTIC_RETRIEVAL=true`, the search tool uses Azure AI Search's built-in agentic retrieval API instead of the standard hybrid path. Toggle via `.env` or at runtime from the frontend.

---

## Blob Storage URL Resolution

The search tool automatically appends a SAS token to document and image URLs:

| Config variable | Purpose |
|---|---|
| `BASE_URL` | Base URL of the blob container holding indexed documents |
| `SAS_TOKEN` | SAS token appended to `filepath`-based citation URLs |
| `AZURE_IMAGE_BASE_URL` | Base URL of the blob container holding document images |
| `AZURE_IMAGE_SAS_TOKEN` | SAS token for image URLs extracted from `image_mapping` |

Documents without a public URL are cited as plain-text footnotes in the answer (no broken links).

---

## Using the Config in Code

```python
from src.config import AzureSearchConfig, get_search_field_config, get_select_fields

# Access individual field names
content_field = AzureSearchConfig.CONTENT_FIELD      # "content"
vector_field  = AzureSearchConfig.VECTOR_FIELD        # "contentVector"
main_title    = AzureSearchConfig.MAIN_TITLE_FIELD    # "main_title"

# Full field name dictionary (passed to AzureAISearchTool)
field_config = get_search_field_config()

# List of fields returned in every search result
select_fields = get_select_fields()
```

---

## Search Result Schema

Each result returned by `AzureAISearchTool.search()` contains:

```python
{
    "id":                "chunk-abc123",
    "content":           "The text chunk content…",
    "title":             "Section 4.2 Brake Systems",
    "filepath":          "standards/GB7258-2022.pdf",
    "url":               "https://account.blob.core.windows.net/docs/GB7258-2022.pdf?<sas>",
    "main_title":        "GB 7258-2022",
    "sub_title":         "Technical Specifications for Safety of Power-Driven Vehicles",
    "publisher":         "SAC / TC114",
    "document_code":     "GB 7258-2022",
    "document_category": "National Standard",
    "description":       "Safety requirements for motor vehicles…",
    "full_headers":      "4 > 4.2 > Brake systems",
    "h1":                "4",
    "h2":                "4.2",
    "h3":                "Brake systems",
    "timestamp":         "2024-03-15T08:00:00Z",
    "publish_date":      "2022-09-30",
    "image_mapping":     "[{\"filename\": \"fig4_2.png\", \"page\": 12}]",
    "score":             0.95,        # hybrid score
    "reranker_score":    3.2          # semantic reranker score (when enabled)
}
```

---

## Changing Field Names

If your index uses different field names:

1. Set the relevant `AZURE_SEARCH_*_FIELD` variables in `.env`
2. Set `AZURE_SEARCH_VECTOR_DIMENSIONS=3072` (or your actual dimension)
3. Restart the application — `settings.py` reloads from `.env` on startup

No source code changes are needed for field name overrides.

---

## Common Mistakes

| Mistake | Symptom | Fix |
|---------|---------|-----|
| Using `text-embedding-ada-002` (1536d) with this index | `VectorizedQuery` dimension mismatch error | Set `AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large` and `AZURE_OPENAI_EMBEDDING_DIMENSIONS=3072` |
| Wrong `AZURE_SEARCH_VECTOR_FIELD` value | Vector search silently skipped | Verify field name matches the index schema exactly (default: `contentVector`) |
| Expired `SAS_TOKEN` | Citation links return 403 / documents show as "Internal Document" | Regenerate SAS token and update `.env` |
| Missing semantic configuration | `SemanticConfigurationNotFound` error | Create a semantic config named `default` in the Azure portal or set `AZURE_SEARCH_SEMANTIC_CONFIG` to an existing config name |
