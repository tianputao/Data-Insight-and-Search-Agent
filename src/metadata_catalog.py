"""On-demand, object-scoped Unity Catalog metadata caching."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import threading
import time
from typing import Any, Optional

from .config import DatabricksConfig

_SQL_RESERVED = {
    "ON", "WHERE", "JOIN", "LEFT", "RIGHT", "FULL", "INNER", "OUTER",
    "GROUP", "ORDER", "LIMIT", "QUALIFY", "HAVING", "UNION", "CROSS",
}


@dataclass
class _CacheEntry:
    value: Any
    loaded_at: float


class MetadataCatalogService:
    """Cache only the UC objects requested by MetadataAgent tools."""

    def __init__(self, ttl_seconds: Optional[int] = None) -> None:
        self.ttl_seconds = (
            DatabricksConfig.METADATA_CACHE_TTL_SECONDS
            if ttl_seconds is None
            else max(0, ttl_seconds)
        )
        self._lock = threading.RLock()
        self._schema_cache: dict[str, _CacheEntry] = {}
        self._table_list_cache: dict[tuple[str, str], _CacheEntry] = {}
        self._table_detail_cache: dict[str, _CacheEntry] = {}

    def invalidate(self) -> None:
        """Clear every object cache without changing UC data."""
        with self._lock:
            self._schema_cache.clear()
            self._table_list_cache.clear()
            self._table_detail_cache.clear()

    def list_schemas(
        self,
        *,
        catalog: str = "",
        force_refresh: bool = False,
    ) -> tuple[list[str], bool]:
        """List schemas, caching only this catalog-level result."""
        catalog = catalog or DatabricksConfig.CATALOG
        key = catalog.casefold()
        cached = self._read_cache(self._schema_cache, key, force_refresh)
        if cached is not None:
            return cached, True

        schemas = [
            schema.name
            for schema in self._workspace_client().schemas.list(
                catalog_name=catalog
            )
            if schema.name
        ]
        schemas.sort(key=str.casefold)
        self._write_cache(self._schema_cache, key, schemas)
        return deepcopy(schemas), False

    def list_tables(
        self,
        *,
        schema: str,
        catalog: str = "",
        force_refresh: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        """List table summaries without fetching any table's columns."""
        if not schema:
            raise ValueError("schema is required when listing tables")
        catalog = catalog or DatabricksConfig.CATALOG
        key = (catalog.casefold(), schema.casefold())
        cached = self._read_cache(self._table_list_cache, key, force_refresh)
        if cached is not None:
            return cached, True

        tables = [
            {
                "name": table.name,
                "full_name": table.full_name
                or f"{catalog}.{schema}.{table.name}",
                "schema": schema,
                "table_type": str(table.table_type),
                "comment": table.comment or "",
            }
            for table in self._workspace_client().tables.list(
                catalog_name=catalog,
                schema_name=schema,
            )
            if table.name
        ]
        tables.sort(key=lambda table: table["full_name"].casefold())
        self._write_cache(self._table_list_cache, key, tables)
        return deepcopy(tables), False

    def get_table(
        self,
        table_name: str,
        *,
        catalog: str = "",
        schema: str = "",
        force_refresh: bool = False,
    ) -> tuple[Optional[dict[str, Any]], bool]:
        """Fetch and cache columns for exactly one selected table."""
        catalog, schema, bare_name = self._qualified_table_parts(
            table_name,
            catalog=catalog,
            schema=schema,
        )
        full_name = f"{catalog}.{schema}.{bare_name}"
        key = full_name.casefold()
        cached = self._read_cache(self._table_detail_cache, key, force_refresh)
        if cached is not None:
            return cached, True

        try:
            table = self._workspace_client().tables.get(full_name=full_name)
        except Exception as exc:
            if self._is_not_found(exc):
                return None, False
            raise

        detail = {
            "name": bare_name,
            "full_name": full_name,
            "schema": schema,
            "table_type": str(table.table_type),
            "comment": getattr(table, "comment", "") or "",
            "owner": getattr(table, "owner", "") or "",
            "columns": [
                self._column_payload(column)
                for column in (table.columns or [])
                if column.name
            ],
        }
        table_tags = self._tags_payload(getattr(table, "tags", None))
        if table_tags:
            detail["table_tags"] = table_tags
        self._write_cache(self._table_detail_cache, key, detail)
        return deepcopy(detail), False

    def search_tables(
        self,
        keyword: str,
        *,
        schema: str,
        catalog: str = "",
    ) -> tuple[list[dict[str, Any]], bool]:
        """Search table names/comments; column details remain unloaded."""
        tables, cache_hit = self.list_tables(
            catalog=catalog,
            schema=schema,
        )
        return self._matching_tables(tables, keyword), cache_hit

    def rewrite_sql_identifiers(
        self,
        sql: str,
    ) -> tuple[str, list[dict[str, str]]]:
        """Correct identifiers only from already-cached selected table details."""
        with self._lock:
            details = [
                deepcopy(entry.value)
                for entry in self._table_detail_cache.values()
                if self._is_fresh(entry)
            ]
        if not details:
            return sql, []

        tables_by_name = {
            table["name"].casefold(): table
            for table in details
        }
        tables_by_full_name = {
            table["full_name"].casefold(): table
            for table in details
        }
        aliases: dict[str, dict[str, Any]] = {}
        unverified_aliases: set[str] = set()
        table_pattern = re.compile(
            r"(?is)\b(?:FROM|JOIN)\s+([`A-Za-z0-9_.]+)"
            r"(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?"
        )
        for match in table_pattern.finditer(sql):
            full_name = match.group(1).replace("`", "")
            bare_name = full_name.rsplit(".", 1)[-1]
            possible_alias = (match.group(2) or bare_name).strip()
            if possible_alias.upper() in _SQL_RESERVED:
                possible_alias = bare_name
            table = (
                tables_by_full_name.get(full_name.casefold())
                or tables_by_name.get(bare_name.casefold())
            )
            if table is None:
                unverified_aliases.add(possible_alias.casefold())
                continue
            aliases[possible_alias.casefold()] = table

        for alias in unverified_aliases:
            aliases.pop(alias, None)

        corrections: list[dict[str, str]] = []

        def replace_identifier(match: re.Match[str]) -> str:
            alias = match.group(1)
            requested = match.group(2)
            table = aliases.get(alias.casefold())
            if table is None:
                return match.group(0)
            columns = [column["name"] for column in table["columns"]]
            exact = next(
                (
                    column
                    for column in columns
                    if column.casefold() == requested.casefold()
                ),
                None,
            )
            if exact:
                return f"{alias}.{exact}"

            resolved = self._resolve_column_name(requested, columns)
            if resolved is None:
                return match.group(0)
            corrections.append(
                {
                    "table": table["full_name"],
                    "from": f"{alias}.{requested}",
                    "to": f"{alias}.{resolved}",
                }
            )
            return f"{alias}.{resolved}"

        rewritten = re.sub(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
            replace_identifier,
            sql,
        )
        return rewritten, corrections

    def cache_stats(self) -> dict[str, int]:
        """Return non-sensitive object counts for diagnostics and tests."""
        with self._lock:
            return {
                "catalogs": len(self._schema_cache),
                "table_lists": len(self._table_list_cache),
                "table_details": len(self._table_detail_cache),
            }

    def _read_cache(
        self,
        cache: dict[Any, _CacheEntry],
        key: Any,
        force_refresh: bool,
    ) -> Any:
        with self._lock:
            entry = cache.get(key)
            if not force_refresh and entry is not None and self._is_fresh(entry):
                return deepcopy(entry.value)
        return None

    def _write_cache(
        self,
        cache: dict[Any, _CacheEntry],
        key: Any,
        value: Any,
    ) -> None:
        with self._lock:
            cache[key] = _CacheEntry(deepcopy(value), time.monotonic())

    def _is_fresh(self, entry: _CacheEntry) -> bool:
        return (
            self.ttl_seconds == 0
            or time.monotonic() - entry.loaded_at < self.ttl_seconds
        )

    @staticmethod
    def _workspace_client():
        if not DatabricksConfig.is_configured():
            raise RuntimeError("Databricks connection is not configured")
        from databricks.sdk import WorkspaceClient

        return WorkspaceClient(
            host=DatabricksConfig.HOST,
            token=DatabricksConfig.TOKEN,
        )

    @staticmethod
    def _qualified_table_parts(
        table_name: str,
        *,
        catalog: str,
        schema: str,
    ) -> tuple[str, str, str]:
        parts = table_name.replace("`", "").split(".")
        catalog = catalog or DatabricksConfig.CATALOG
        schema = schema or DatabricksConfig.SCHEMA
        if len(parts) == 3:
            catalog, schema, table_name = parts
        elif len(parts) == 2:
            schema, table_name = parts
        elif len(parts) == 1:
            table_name = parts[0]
        else:
            raise ValueError(f"Invalid table name: {table_name}")
        return catalog, schema, table_name

    @staticmethod
    def _column_payload(column: Any) -> dict[str, Any]:
        result = {
            "name": column.name,
            "type": str(column.type_name),
            "nullable": getattr(column, "nullable", None),
            "comment": getattr(column, "comment", "") or "",
        }
        tags = MetadataCatalogService._tags_payload(
            getattr(column, "tags", None)
        )
        if tags:
            result["tags"] = tags
        return result

    @staticmethod
    def _tags_payload(tags: Any) -> dict[str, Any]:
        if not tags:
            return {}
        try:
            return dict(tags.items())
        except (AttributeError, TypeError, ValueError):
            return {}

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        text = str(exc).casefold()
        return "not found" in text or "does not exist" in text

    @classmethod
    def _matching_tables(
        cls,
        tables: list[dict[str, Any]],
        keyword: str,
    ) -> list[dict[str, Any]]:
        normalized_keyword = cls._normalize_text(keyword)
        compact_keyword = normalized_keyword.replace(" ", "")
        matches: list[tuple[int, float, dict[str, Any]]] = []
        for table in tables:
            metadata_text = cls._normalize_text(
                " ".join(
                    [
                        table.get("name", ""),
                        table.get("full_name", ""),
                        table.get("comment", ""),
                    ]
                )
            )
            token_overlap = len(
                set(normalized_keyword.split()) & set(metadata_text.split())
            )
            fuzzy = SequenceMatcher(
                None,
                compact_keyword,
                cls._normalize_text(table.get("name", "")).replace(" ", ""),
            ).ratio()
            if token_overlap > 0 or fuzzy >= 0.55:
                matches.append((token_overlap, fuzzy, table))
        matches.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                item[2]["full_name"].casefold(),
            )
        )
        return [deepcopy(table) for _, _, table in matches]

    @staticmethod
    def _resolve_column_name(
        requested: str,
        columns: list[str],
    ) -> Optional[str]:
        normalized_requested = MetadataCatalogService._normalize(requested)
        suffix_matches = [
            column
            for column in columns
            if len(MetadataCatalogService._normalize(column)) >= 3
            and normalized_requested.endswith(
                MetadataCatalogService._normalize(column)
            )
        ]
        if len(suffix_matches) == 1:
            return suffix_matches[0]

        return None

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())

    @staticmethod
    def _normalize_text(value: str) -> str:
        text = str(value or "")
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        text = text.casefold()
        text = re.sub(r"[_\-/]+", " ", text)
        text = re.sub(
            r"[^\w\u3400-\u9fff]+",
            " ",
            text,
            flags=re.UNICODE,
        )
        return " ".join(text.split())