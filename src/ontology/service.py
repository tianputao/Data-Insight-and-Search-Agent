"""Fault-tolerant, read-only queries over repository OWL files."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
import re
import threading
import unicodedata
from typing import Any, Callable, Iterable, Optional

from owlready2 import (
    AnnotationPropertyClass,
    ClassConstruct,
    DataPropertyClass,
    EXACTLY,
    HAS_SELF,
    MAX,
    MIN,
    ObjectPropertyClass,
    ONLY,
    PropertyClass,
    Restriction,
    SOME,
    Thing,
    ThingClass,
    VALUE,
    World,
    sync_reasoner,
    sync_reasoner_pellet,
)

from ..config import OntologyConfig
from ..utils import get_logger

logger = get_logger(__name__)

_RESTRICTION_NAMES = {
    SOME: "some",
    ONLY: "only",
    VALUE: "value",
    EXACTLY: "exactly",
    MIN: "min",
    MAX: "max",
    HAS_SELF: "has_self",
}

_TABLE_MAPPING_TERMS = {
    "table",
    "tablename",
    "sourcetable",
    "physicaltable",
    "databrickstable",
}
_COLUMN_MAPPING_TERMS = {
    "column",
    "columnname",
    "sourcecolumn",
    "physicalcolumn",
    "databrickscolumn",
}
_LINEAGE_TERMS = {
    "lineage",
    "derivedfrom",
    "dependson",
    "upstreamof",
    "downstreamof",
    "sourceof",
}
@dataclass(frozen=True)
class _EntityRecord:
    entity: Any
    iri: str
    name: str
    kind: str
    labels: tuple[dict[str, str], ...]
    comments: tuple[dict[str, str], ...]
    normalized_names: tuple[str, ...]
    search_text: str


def _safe_query(method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Return a stable error envelope for unexpected query failures."""

    @wraps(method)
    def wrapped(self: "OntologyService", *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            with self._lock:
                self._require_loaded()
                return method(self, *args, **kwargs)
        except Exception as exc:
            logger.error("Ontology query %s failed: %s", method.__name__, exc, exc_info=True)
            return self._envelope(
                "error",
                warnings=[f"{method.__name__} failed: {exc}"],
                strategies_tried=[method.__name__],
            )

    return wrapped


class OntologyService:
    """Load repository ontologies once and expose bounded read-only queries."""

    def __init__(
        self,
        directory: Optional[Path | str] = None,
        *,
        file_glob: Optional[str] = None,
        enable_reasoner: Optional[bool] = None,
        reasoner: Optional[str] = None,
        only_local: Optional[bool] = None,
        max_results: Optional[int] = None,
        max_depth: Optional[int] = None,
        max_paths: Optional[int] = None,
        max_nodes: Optional[int] = None,
        fuzzy_threshold: Optional[float] = None,
    ) -> None:
        self.directory = Path(directory or OntologyConfig.DIRECTORY).expanduser().resolve()
        self.file_glob = file_glob or OntologyConfig.FILE_GLOB
        self.enable_reasoner = (
            OntologyConfig.ENABLE_REASONER
            if enable_reasoner is None
            else enable_reasoner
        )
        self.reasoner = (reasoner or OntologyConfig.REASONER).lower()
        self.only_local = OntologyConfig.ONLY_LOCAL if only_local is None else only_local
        self.max_results = max_results or OntologyConfig.MAX_RESULTS
        self.max_depth = max_depth or OntologyConfig.MAX_DEPTH
        self.max_paths = max_paths or OntologyConfig.MAX_PATHS
        self.max_nodes = max_nodes or OntologyConfig.MAX_NODES
        self.fuzzy_threshold = (
            OntologyConfig.FUZZY_THRESHOLD
            if fuzzy_threshold is None
            else fuzzy_threshold
        )

        self._lock = threading.RLock()
        self.world: Optional[World] = None
        self.ontologies: list[Any] = []
        self.files: list[Path] = []
        self.load_errors: list[dict[str, str]] = []
        self.reasoning_status = "not_started"
        self.reasoning_error: Optional[str] = None
        self.loaded = False
        self._records: list[_EntityRecord] = []
        self._records_by_iri: dict[str, _EntityRecord] = {}
        self._adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._edge_keys: set[tuple[str, str, str, str]] = set()

    def load(self) -> "OntologyService":
        """Recursively load configured OWL files and build immutable query indexes."""
        with self._lock:
            if self.loaded:
                return self
            if not self.directory.exists():
                raise RuntimeError(f"Ontology directory does not exist: {self.directory}")

            self.files = sorted(
                path.resolve()
                for path in self.directory.glob(self.file_glob)
                if path.is_file()
            )
            if not self.files:
                raise RuntimeError(
                    f"No ontology files matched {self.file_glob!r} in {self.directory}"
                )

            self.world = World()
            for path in self.files:
                try:
                    ontology = self.world.get_ontology(path.as_uri()).load(
                        only_local=self.only_local
                    )
                    self.ontologies.append(ontology)
                except Exception as exc:
                    self.load_errors.append({"file": str(path), "error": str(exc)})
                    logger.error("Failed to load ontology %s: %s", path, exc, exc_info=True)

            if not self.ontologies:
                errors = "; ".join(item["error"] for item in self.load_errors)
                raise RuntimeError(f"All ontology files failed to load: {errors}")

            if self.enable_reasoner:
                try:
                    self._run_reasoner()
                except RuntimeError as exc:
                    logger.warning(
                        "Ontology reasoning failed; continuing with asserted OWL facts: %s",
                        exc,
                    )
            else:
                self.reasoning_status = "disabled"

            self._build_indexes()
            self.loaded = True
            logger.info(
                "Ontology service loaded %s file(s), %s entities; reasoning=%s",
                len(self.ontologies),
                len(self._records),
                self.reasoning_status,
            )
            return self

    def close(self) -> None:
        """Release the in-memory Owlready2 world."""
        with self._lock:
            if self.world is not None:
                self.world.close()
            self.world = None
            self.loaded = False

    def health(self) -> dict[str, Any]:
        """Return non-sensitive ontology capability status."""
        return {
            "available": self.loaded,
            "directory": str(self.directory),
            "file_count": len(self.files),
            "ontology_count": len(self.ontologies),
            "entity_count": len(self._records),
            "load_errors": list(self.load_errors),
            "reasoner_enabled": self.enable_reasoner,
            "reasoner": self.reasoner,
            "reasoning_status": self.reasoning_status,
            "reasoning_error": self.reasoning_error,
        }

    def _run_reasoner(self) -> None:
        if self.world is None:
            raise RuntimeError("Ontology world has not been created")
        self.reasoning_status = "running"
        try:
            if self.reasoner == "pellet":
                sync_reasoner_pellet(
                    self.world,
                    infer_property_values=True,
                    infer_data_property_values=True,
                    debug=0,
                )
            elif self.reasoner == "hermit":
                sync_reasoner(
                    self.world,
                    infer_property_values=True,
                    debug=0,
                )
            else:
                raise ValueError(f"Unsupported ontology reasoner: {self.reasoner}")
            self.reasoning_status = "completed"
        except Exception as exc:
            self.reasoning_status = "failed"
            self.reasoning_error = str(exc)
            raise RuntimeError(f"{self.reasoner} reasoning failed: {exc}") from exc

    def _build_indexes(self) -> None:
        if self.world is None:
            raise RuntimeError("Ontology world has not been created")

        entities: list[Any] = []
        entities.extend(list(self.world.classes()))
        entities.extend(list(self.world.properties()))
        entities.extend(list(self.world.individuals()))

        records: list[_EntityRecord] = []
        for entity in entities:
            iri = str(getattr(entity, "iri", "") or "")
            if not iri or iri in self._records_by_iri:
                continue
            name = str(getattr(entity, "name", "") or iri.rsplit("#", 1)[-1])
            labels = tuple(self._literal_items(getattr(entity, "label", [])))
            comments = tuple(self._literal_items(getattr(entity, "comment", [])))
            aliases = [name, iri, *[item["value"] for item in labels]]
            normalized_names = tuple(
                dict.fromkeys(value for value in map(self._normalize, aliases) if value)
            )
            search_text = " ".join(
                [*normalized_names, *[self._normalize(item["value"]) for item in comments]]
            )
            record = _EntityRecord(
                entity=entity,
                iri=iri,
                name=name,
                kind=self._entity_kind(entity),
                labels=labels,
                comments=comments,
                normalized_names=normalized_names,
                search_text=search_text,
            )
            records.append(record)
            self._records_by_iri[iri] = record

        self._records = sorted(records, key=lambda item: (item.kind, item.name.casefold()))
        self._build_adjacency()

    def _build_adjacency(self) -> None:
        if self.world is None:
            return

        for class_entity in self.world.classes():
            class_iri = self._known_iri(class_entity)
            if not class_iri:
                continue
            for parent in list(getattr(class_entity, "is_a", []) or []):
                if isinstance(parent, ThingClass):
                    parent_iri = self._known_iri(parent)
                    if parent_iri:
                        self._add_edge(
                            class_iri,
                            "subclass_of",
                            parent_iri,
                            direction="outgoing",
                            source="class_hierarchy",
                        )
                        self._add_edge(
                            parent_iri,
                            "superclass_of",
                            class_iri,
                            direction="incoming",
                            source="class_hierarchy",
                        )
                elif isinstance(parent, Restriction):
                    self._add_restriction_edge(class_iri, parent)

            for equivalent in list(getattr(class_entity, "equivalent_to", []) or []):
                equivalent_iri = self._known_iri(equivalent)
                if equivalent_iri:
                    self._add_edge(
                        class_iri,
                        "equivalent_to",
                        equivalent_iri,
                        direction="outgoing",
                        source="equivalence",
                    )
                    self._add_edge(
                        equivalent_iri,
                        "equivalent_to",
                        class_iri,
                        direction="incoming",
                        source="equivalence",
                    )

        for prop in self.world.object_properties():
            inverse = getattr(prop, "inverse_property", None)
            inverse_name = str(getattr(inverse, "name", "") or f"inverse_of_{prop.name}")
            domains = [value for value in list(prop.domain) if self._known_iri(value)]
            ranges = [value for value in list(prop.range) if self._known_iri(value)]
            for domain in domains:
                for range_value in ranges:
                    domain_iri = self._known_iri(domain)
                    range_iri = self._known_iri(range_value)
                    if domain_iri and range_iri:
                        self._add_edge(
                            domain_iri,
                            str(prop.name),
                            range_iri,
                            direction="outgoing",
                            source="property_domain_range",
                        )
                        self._add_edge(
                            range_iri,
                            inverse_name,
                            domain_iri,
                            direction="incoming",
                            source="property_domain_range",
                        )

            try:
                relations = list(prop.get_relations())
            except Exception as exc:
                logger.warning("Could not enumerate relations for %s: %s", prop.name, exc)
                relations = []
            for subject, object_value in relations:
                subject_iri = self._known_iri(subject)
                object_iri = self._known_iri(object_value)
                if subject_iri and object_iri:
                    self._add_edge(
                        subject_iri,
                        str(prop.name),
                        object_iri,
                        direction="outgoing",
                        source="assertion",
                    )
                    self._add_edge(
                        object_iri,
                        inverse_name,
                        subject_iri,
                        direction="incoming",
                        source="assertion",
                    )

    def _add_restriction_edge(self, source_iri: str, restriction: Restriction) -> None:
        prop = getattr(restriction, "property", None)
        value = getattr(restriction, "value", None)
        target_iri = self._known_iri(value)
        if prop is not None and target_iri:
            self._add_edge(
                source_iri,
                str(getattr(prop, "name", "restricted_by")),
                target_iri,
                direction="outgoing",
                source="class_restriction",
            )

    def _add_edge(
        self,
        source_iri: str,
        relation: str,
        target_iri: str,
        *,
        direction: str,
        source: str,
    ) -> None:
        key = (source_iri, relation, target_iri, source)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self._adjacency[source_iri].append(
            {
                "from": source_iri,
                "relation": relation,
                "to": target_iri,
                "direction": direction,
                "source": source,
            }
        )

    @_safe_query
    def search_entities(
        self,
        query: str,
        *,
        entity_types: Optional[list[str] | tuple[str, ...] | set[str]] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        """Search entities using exact, normalized, token, and fuzzy strategies."""
        matches = self._search_records(query, entity_types=entity_types, limit=limit)
        if not matches:
            return self._envelope(
                "no_match",
                matches=[],
                confidence=0.0,
                strategies_tried=self._search_strategies(),
                unresolved=[query],
            )
        status = "ok"
        if len(matches) > 1 and abs(matches[0][1] - matches[1][1]) < 0.03:
            status = "ambiguous"
        rendered = [self._match_payload(record, score, strategy) for record, score, strategy in matches]
        return self._envelope(
            status,
            data=rendered,
            matches=rendered,
            evidence=[item["evidence"] for item in rendered],
            confidence=matches[0][1],
            strategies_tried=self._search_strategies(),
        )

    @_safe_query
    def describe_entity(
        self,
        entity: str,
        *,
        entity_types: Optional[list[str] | tuple[str, ...] | set[str]] = None,
    ) -> dict[str, Any]:
        """Describe one unambiguous class, property, or individual."""
        record, matches, status = self._resolve_record(entity, entity_types=entity_types)
        rendered_matches = [
            self._match_payload(item, score, strategy)
            for item, score, strategy in matches
        ]
        if record is None:
            return self._envelope(
                status,
                matches=rendered_matches,
                confidence=matches[0][1] if matches else 0.0,
                strategies_tried=self._search_strategies(),
                unresolved=[entity],
            )

        data = self._describe_record(record)
        return self._envelope(
            "ok",
            data=data,
            matches=rendered_matches[:1],
            evidence=[{"iri": record.iri, "source": "ontology"}],
            confidence=matches[0][1] if matches else 1.0,
            strategies_tried=self._search_strategies(),
        )

    @_safe_query
    def expand_neighbors(
        self,
        entity: str,
        *,
        depth: int = 2,
        direction: str = "both",
        relation_types: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Expand a bounded semantic neighborhood around an entity."""
        record, matches, status = self._resolve_record(entity)
        if record is None:
            return self._resolution_envelope(entity, matches, status)

        bounded_depth = min(max(1, depth), self.max_depth)
        allowed = {self._normalize(value) for value in relation_types or []}
        queue: deque[tuple[str, int]] = deque([(record.iri, 0)])
        visited = {record.iri}
        edges: list[dict[str, Any]] = []

        while queue and len(visited) < self.max_nodes:
            current_iri, current_depth = queue.popleft()
            if current_depth >= bounded_depth:
                continue
            for edge in self._adjacency.get(current_iri, []):
                if direction != "both" and edge["direction"] != direction:
                    continue
                if allowed and self._normalize(edge["relation"]) not in allowed:
                    continue
                edges.append(self._render_edge(edge))
                target_iri = edge["to"]
                if target_iri not in visited:
                    visited.add(target_iri)
                    queue.append((target_iri, current_depth + 1))
                if len(visited) >= self.max_nodes:
                    break

        nodes = [
            self._entity_ref(self._records_by_iri[iri].entity)
            for iri in visited
            if iri in self._records_by_iri
        ]
        warnings = []
        if len(visited) >= self.max_nodes:
            warnings.append(f"Neighborhood truncated at {self.max_nodes} nodes")
        return self._envelope(
            "ok" if edges else "partial",
            data={"root": self._entity_ref(record.entity), "nodes": nodes, "edges": edges},
            matches=[self._match_payload(*matches[0])] if matches else [],
            evidence=[{"source": edge["source"], "relation": edge["relation"]} for edge in edges],
            confidence=matches[0][1] if matches else 1.0,
            strategies_tried=["bounded_breadth_first_traversal"],
            warnings=warnings,
        )

    @_safe_query
    def find_paths(
        self,
        source: str,
        target: str,
        *,
        max_depth: Optional[int] = None,
        max_paths: Optional[int] = None,
        relation_types: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Find bounded, cycle-free semantic paths between two entities."""
        source_record, source_matches, source_status = self._resolve_record(source)
        if source_record is None:
            return self._resolution_envelope(source, source_matches, source_status)
        target_record, target_matches, target_status = self._resolve_record(target)
        if target_record is None:
            return self._resolution_envelope(target, target_matches, target_status)

        depth_limit = min(max_depth or self.max_depth, self.max_depth)
        path_limit = min(max_paths or self.max_paths, self.max_paths)
        allowed = {self._normalize(value) for value in relation_types or []}
        queue: deque[tuple[str, list[dict[str, Any]], set[str]]] = deque(
            [(source_record.iri, [], {source_record.iri})]
        )
        paths: list[dict[str, Any]] = []
        visited_states = 0

        while queue and len(paths) < path_limit and visited_states < self.max_nodes:
            current_iri, path_edges, seen = queue.popleft()
            visited_states += 1
            if len(path_edges) >= depth_limit:
                continue
            for edge in self._adjacency.get(current_iri, []):
                if allowed and self._normalize(edge["relation"]) not in allowed:
                    continue
                next_iri = edge["to"]
                if next_iri in seen:
                    continue
                next_edges = [*path_edges, edge]
                if next_iri == target_record.iri:
                    paths.append(self._path_payload(source_record.iri, next_edges))
                    if len(paths) >= path_limit:
                        break
                else:
                    queue.append((next_iri, next_edges, {*seen, next_iri}))

        if not paths:
            return self._envelope(
                "no_match",
                data={"source": self._entity_ref(source_record.entity), "target": self._entity_ref(target_record.entity), "paths": []},
                confidence=min(source_matches[0][1], target_matches[0][1]),
                strategies_tried=[
                    "direct_and_inverse_relations",
                    "class_hierarchy",
                    "owl_restrictions",
                    f"bounded_path_depth_{depth_limit}",
                ],
                unresolved=[f"No path from {source} to {target}"],
            )
        return self._envelope(
            "ok",
            data={
                "source": self._entity_ref(source_record.entity),
                "target": self._entity_ref(target_record.entity),
                "paths": paths,
            },
            evidence=[
                {"source": edge["source"], "relation": edge["relation"]}
                for path in paths
                for edge in path["edges"]
            ],
            confidence=min(source_matches[0][1], target_matches[0][1]),
            strategies_tried=["bounded_bidirectional_semantic_graph"],
        )

    @_safe_query
    def find_related_by_type(
        self,
        entity: str,
        target_type: str,
        *,
        max_depth: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        """Find reachable classes or individuals matching a target OWL class."""
        record, matches, status = self._resolve_record(entity)
        if record is None:
            return self._resolution_envelope(entity, matches, status)
        type_record, type_matches, type_status = self._resolve_record(
            target_type,
            entity_types={"class"},
        )
        if type_record is None:
            return self._resolution_envelope(target_type, type_matches, type_status)

        depth_limit = min(max_depth or self.max_depth, self.max_depth)
        result_limit = min(limit or self.max_results, self.max_results)
        queue: deque[tuple[str, int, list[dict[str, Any]]]] = deque(
            [(record.iri, 0, [])]
        )
        visited = {record.iri}
        related: list[dict[str, Any]] = []

        while queue and len(visited) < self.max_nodes and len(related) < result_limit:
            current_iri, current_depth, path = queue.popleft()
            if current_depth >= depth_limit:
                continue
            for edge in self._adjacency.get(current_iri, []):
                target_iri = edge["to"]
                if target_iri in visited:
                    continue
                visited.add(target_iri)
                next_path = [*path, edge]
                target_entity_record = self._records_by_iri.get(target_iri)
                if target_entity_record and self._record_matches_class(
                    target_entity_record,
                    type_record.entity,
                ):
                    related.append(
                        {
                            "entity": self._entity_ref(target_entity_record.entity),
                            "path": self._path_payload(record.iri, next_path),
                        }
                    )
                    if len(related) >= result_limit:
                        break
                queue.append((target_iri, current_depth + 1, next_path))

        return self._envelope(
            "ok" if related else "no_match",
            data={
                "root": self._entity_ref(record.entity),
                "target_type": self._entity_ref(type_record.entity),
                "related": related,
            },
            confidence=min(matches[0][1], type_matches[0][1]),
            strategies_tried=[
                "direct_and_inverse_relations",
                "descendant_type_matching",
                "bounded_graph_expansion",
            ],
            unresolved=[] if related else [f"No reachable {target_type} from {entity}"],
        )

    @_safe_query
    def get_schema_mapping(self, entity: str) -> dict[str, Any]:
        """Return explicit physical mappings or low-confidence naming candidates."""
        record, matches, status = self._resolve_record(entity)
        if record is None:
            return self._resolution_envelope(entity, matches, status)

        explicit_tables = self._mapping_annotations(record.entity, _TABLE_MAPPING_TERMS)
        explicit_columns = self._mapping_annotations(record.entity, _COLUMN_MAPPING_TERMS)
        candidate_tables: list[dict[str, Any]] = []
        candidate_columns: list[dict[str, Any]] = []

        if record.kind == "class":
            snake_name = self._snake_case(record.name)
            table_names = [snake_name, f"{snake_name}s"]
            if any(term in self._normalize(record.name) for term in ("order", "sales", "line")):
                table_names.append(f"fact_{snake_name}")
            else:
                table_names.append(f"dim_{snake_name}")
            candidate_tables = [
                {
                    "name": value,
                    "confidence": 0.35,
                    "source": "normalized_ontology_name",
                }
                for value in dict.fromkeys(table_names)
            ]
            for prop in self._properties_for_class(record.entity):
                candidate_columns.append(
                    {
                        "property": prop["name"],
                        "candidates": [self._snake_case(prop["name"]), prop["name"]],
                        "confidence": 0.4,
                        "source": "ontology_property_name",
                    }
                )
        elif record.kind.endswith("_property"):
            candidate_columns.append(
                {
                    "property": record.name,
                    "candidates": [self._snake_case(record.name), record.name],
                    "confidence": 0.4,
                    "source": "ontology_property_name",
                }
            )

        physical_mappings = {
            "tables": explicit_tables,
            "columns": explicit_columns,
        }
        has_explicit_mapping = bool(explicit_tables or explicit_columns)
        warnings = []
        if not has_explicit_mapping:
            warnings.append(
                "No authoritative physical mapping annotations were found; candidates require Unity Catalog verification"
            )
        return self._envelope(
            "ok" if has_explicit_mapping else "partial",
            data={
                "entity": self._entity_ref(record.entity),
                "physical_mappings": physical_mappings,
                "candidate_tables": candidate_tables,
                "candidate_columns": candidate_columns,
                "requires_metadata_resolution": not has_explicit_mapping,
            },
            evidence=[
                {"source": "ontology_annotation", "value": value}
                for value in [*explicit_tables, *explicit_columns]
            ],
            confidence=1.0 if has_explicit_mapping else 0.4,
            strategies_tried=[
                "explicit_mapping_annotations",
                "normalized_name_candidates",
            ],
            warnings=warnings,
        )

    @_safe_query
    def get_join_paths(
        self,
        source: str,
        target: str,
        *,
        max_depth: Optional[int] = None,
        max_paths: Optional[int] = None,
    ) -> dict[str, Any]:
        """Return semantic join paths and only explicitly grounded physical joins."""
        path_result = self.find_paths(
            source,
            target,
            max_depth=max_depth,
            max_paths=max_paths,
        )
        if path_result["status"] not in {"ok", "partial"}:
            return path_result

        semantic_paths = (path_result.get("data") or {}).get("paths", [])
        join_paths = [
            {
                "semantic_path": semantic_path["semantic_path"],
                "semantic_edges": semantic_path["edges"],
                "physical_joins": [],
                "requires_metadata_resolution": True,
            }
            for semantic_path in semantic_paths
        ]
        return self._envelope(
            "partial",
            data={"source": source, "target": target, "join_paths": join_paths},
            evidence=path_result["evidence"],
            confidence=path_result["confidence"],
            strategies_tried=[
                *path_result["strategies_tried"],
                "explicit_physical_join_annotations",
            ],
            warnings=[
                "Semantic paths were found, but physical joins must be verified by MetadataAgent against Unity Catalog"
            ],
        )

    @_safe_query
    def get_lineage(
        self,
        entity: str,
        *,
        direction: str = "both",
        depth: int = 2,
    ) -> dict[str, Any]:
        """Return explicit lineage separately from ordinary semantic dependencies."""
        record, matches, status = self._resolve_record(entity)
        if record is None:
            return self._resolution_envelope(entity, matches, status)

        depth_limit = min(max(1, depth), self.max_depth)
        queue: deque[tuple[str, int]] = deque([(record.iri, 0)])
        visited = {record.iri}
        explicit_edges: list[dict[str, Any]] = []
        dependency_edges: list[dict[str, Any]] = []
        while queue and len(visited) < self.max_nodes:
            current_iri, current_depth = queue.popleft()
            if current_depth >= depth_limit:
                continue
            for edge in self._adjacency.get(current_iri, []):
                if direction != "both" and edge["direction"] != direction:
                    continue
                normalized_relation = self._normalize(edge["relation"]).replace(" ", "")
                rendered = self._render_edge(edge)
                if any(term in normalized_relation for term in _LINEAGE_TERMS):
                    explicit_edges.append(rendered)
                else:
                    dependency_edges.append(rendered)
                if edge["to"] not in visited:
                    visited.add(edge["to"])
                    queue.append((edge["to"], current_depth + 1))

        warnings = []
        if not explicit_edges:
            warnings.append(
                "No explicit lineage predicates were found; semantic dependencies are reported separately"
            )
        return self._envelope(
            "ok" if explicit_edges else "partial",
            data={
                "entity": self._entity_ref(record.entity),
                "explicit_lineage": explicit_edges,
                "semantic_dependencies": dependency_edges[: self.max_results],
                "has_explicit_lineage": bool(explicit_edges),
            },
            evidence=[
                {"source": edge["source"], "relation": edge["relation"]}
                for edge in [*explicit_edges, *dependency_edges]
            ],
            confidence=matches[0][1],
            strategies_tried=[
                "explicit_lineage_predicates",
                "semantic_dependency_fallback",
            ],
            warnings=warnings,
        )

    @_safe_query
    def get_semantic_candidates(
        self,
        question: str,
        *,
        root_entity: str = "",
        max_depth: int = 2,
    ) -> dict[str, Any]:
        """Return relevant OWL properties and relationships without assigning analysis roles."""
        roots = self._candidate_root_records(question, root_entity=root_entity)
        if not roots:
            return self._envelope(
                "no_match",
                strategies_tried=[
                    *self._search_strategies(),
                    "property_domain_to_root_class",
                ],
                unresolved=[question],
            )

        root_record, root_score, root_strategy = roots[0]
        class_records = self._related_class_records(
            root_record,
            depth=min(max_depth, self.max_depth),
        )
        semantic_properties: list[dict[str, Any]] = []
        semantic_relationships: list[dict[str, Any]] = []
        seen_properties: set[str] = set()
        property_matches = self._search_records(
            question,
            entity_types={"property"},
            limit=self.max_results,
        )
        property_relevance = {
            record.iri: score for record, score, _ in property_matches
        }

        for property_record, relevance, _ in property_matches:
            prop = self._property_summary(property_record.entity)
            domain_records = [
                self._records_by_iri.get(self._known_iri(domain))
                for domain in list(
                    getattr(property_record.entity, "domain", []) or []
                )
            ]
            domain_record = next(
                (
                    record
                    for record in domain_records
                    if record is not None and record.kind == "class"
                ),
                None,
            )
            seen_properties.add(property_record.iri)
            semantic_properties.append(
                {
                    "name": prop["name"],
                    "entity": domain_record.name if domain_record else root_record.name,
                    "iri": property_record.iri,
                    "evidence": "question match and property domain/range",
                    "relevance": round(relevance, 4),
                    "type": prop.get("type"),
                    "labels": prop.get("labels", []),
                    "comments": prop.get("comments", []),
                    "domain": prop.get("domain", []),
                    "range": prop.get("range", []),
                    "characteristics": prop.get("characteristics", []),
                }
            )

        for class_record in class_records:
            for prop in self._properties_for_class(class_record.entity):
                prop_iri = prop["iri"]
                if prop_iri in seen_properties:
                    continue
                seen_properties.add(prop_iri)
                relevance = round(property_relevance.get(prop_iri, 0.0), 4)
                if relevance <= 0:
                    continue
                semantic_property = {
                    "name": prop["name"],
                    "entity": class_record.name,
                    "iri": prop_iri,
                    "evidence": "property domain/range and ontology name",
                    "relevance": relevance,
                    "type": prop.get("type"),
                    "labels": prop.get("labels", []),
                    "comments": prop.get("comments", []),
                    "domain": prop.get("domain", []),
                    "range": prop.get("range", []),
                    "characteristics": prop.get("characteristics", []),
                }
                semantic_properties.append(semantic_property)

        for edge in self._adjacency.get(root_record.iri, []):
            target_record = self._records_by_iri.get(edge["to"])
            if target_record and target_record.kind == "class":
                semantic_relationships.append(
                    {
                        "entity": target_record.name,
                        "relation": edge["relation"],
                        "direction": edge["direction"],
                        "evidence": edge["source"],
                    }
                )

        semantic_properties.sort(
            key=lambda item: (-item["relevance"], item["name"].casefold())
        )

        return self._envelope(
            "ok",
            data={
                "root_entity": self._entity_ref(root_record.entity),
                "semantic_properties": semantic_properties[:16],
                "semantic_relationships": semantic_relationships[:8],
                "filters": [],
            },
            matches=[self._match_payload(root_record, root_score, root_strategy)],
            evidence=[
                {"source": "ontology", "iri": root_record.iri},
                *[
                    {"source": item["evidence"], "property": item["name"]}
                    for item in semantic_properties[:8]
                ],
            ],
            confidence=root_score,
            strategies_tried=[
                "root_entity_resolution",
                "property_domain_range_analysis",
                "semantic_neighbor_analysis",
            ],
        )

    @_safe_query
    def get_business_context(
        self,
        question: str,
        *,
        root_entity: str = "",
        max_depth: int = 3,
    ) -> dict[str, Any]:
        """Build a composite, provenance-rich context for downstream SQL planning."""
        roots = self._candidate_root_records(question, root_entity=root_entity)
        if not roots:
            return self._envelope(
                "no_match",
                data={
                    "root_entity": None,
                    "filters": [],
                    "semantic_properties": [],
                    "semantic_relationships": [],
                    "join_paths": [],
                },
                strategies_tried=[
                    *self._search_strategies(),
                    "property_domain_to_root_class",
                    "alternative_question_terms",
                ],
                unresolved=[question],
            )

        primary, primary_score, primary_strategy = roots[0]
        semantic_result = self.get_semantic_candidates(
            question,
            root_entity=primary.iri,
            max_depth=min(max_depth, self.max_depth),
        )
        schema_result = self.get_schema_mapping(primary.iri)
        if self._is_lineage_question(question):
            lineage_result = self.get_lineage(
                primary.iri,
                depth=min(max_depth, 2),
            )
        else:
            lineage_result = self._envelope(
                "not_requested",
                data={
                    "entity": self._entity_ref(primary.entity),
                    "explicit_lineage": [],
                    "semantic_dependencies": [],
                    "has_explicit_lineage": False,
                },
                strategies_tried=["lineage_not_requested"],
            )

        semantic_data = semantic_result.get("data") or {}
        target_names = {
            item.get("entity")
            for item in semantic_data.get("semantic_properties", [])
            if item.get("entity") and item.get("entity") != primary.name
        }
        target_names.update(
            record.name
            for record, score, _ in roots[1:5]
            if score >= self.fuzzy_threshold and record.name != primary.name
        )
        join_paths = []
        seen_semantic_paths: set[tuple[str, ...]] = set()
        for target_name in sorted(target_names)[:6]:
            join_result = self.get_join_paths(
                primary.iri,
                target_name,
                max_depth=min(max_depth, self.max_depth),
                max_paths=1,
            )
            for path in (join_result.get("data") or {}).get("join_paths", []):
                path_key = tuple(path.get("semantic_path", []))
                if path_key and path_key not in seen_semantic_paths:
                    seen_semantic_paths.add(path_key)
                    join_paths.append(path)

        primary_description = self._describe_record(primary)
        root_entity_detail = {
            key: primary_description.get(key)
            for key in (
                "iri",
                "name",
                "type",
                "labels",
                "comments",
                "parents",
                "ancestors",
                "equivalent_to",
                "restrictions",
            )
            if key in primary_description
        }
        warnings = [
            *semantic_result.get("warnings", []),
            *schema_result.get("warnings", []),
            *lineage_result.get("warnings", []),
        ]
        unresolved = list(
            dict.fromkeys(
                [
                    *semantic_result.get("unresolved", []),
                    *schema_result.get("unresolved", []),
                    *lineage_result.get("unresolved", []),
                ]
            )
        )
        return self._envelope(
            "partial" if unresolved else "ok",
            data={
                "root_entity": primary.name,
                "root_entity_detail": root_entity_detail,
                "filters": [],
                "semantic_properties": semantic_data.get(
                    "semantic_properties", []
                ),
                "semantic_relationships": semantic_data.get(
                    "semantic_relationships", []
                ),
                "join_paths": join_paths,
                "schema_mapping": schema_result.get("data"),
                "lineage": lineage_result.get("data"),
                "entity_candidates": [
                    self._match_payload(record, score, strategy)
                    for record, score, strategy in roots[:5]
                ],
                "requires_metadata_resolution": True,
            },
            matches=[self._match_payload(primary, primary_score, primary_strategy)],
            evidence=[
                *semantic_result.get("evidence", []),
                *schema_result.get("evidence", []),
                *lineage_result.get("evidence", []),
            ],
            confidence=primary_score,
            strategies_tried=[
                "composite_business_context",
                *semantic_result.get("strategies_tried", []),
                *schema_result.get("strategies_tried", []),
            ],
            warnings=list(dict.fromkeys(warnings)),
            unresolved=unresolved,
        )

    def _candidate_root_records(
        self,
        question: str,
        *,
        root_entity: str = "",
    ) -> list[tuple[_EntityRecord, float, str]]:
        if root_entity:
            direct = self._search_records(root_entity, entity_types={"class"}, limit=5)
            if direct:
                return direct

        derived: dict[str, tuple[_EntityRecord, float, str]] = {}
        class_matches = self._search_records(question, entity_types={"class"}, limit=10)
        for class_record, score, strategy in class_matches:
            derived[class_record.iri] = (class_record, score, strategy)

        property_matches = self._search_records(question, entity_types={"property"}, limit=10)
        for property_record, score, _ in property_matches:
            for domain in list(getattr(property_record.entity, "domain", []) or []):
                iri = self._known_iri(domain)
                domain_record = self._records_by_iri.get(iri)
                if domain_record and domain_record.kind == "class":
                    candidate = (domain_record, score * 0.9, "property_domain_to_root_class")
                    current = derived.get(iri)
                    if current is None or candidate[1] > current[1]:
                        derived[iri] = candidate
        return sorted(derived.values(), key=lambda item: -item[1])

    def _related_class_records(
        self,
        root: _EntityRecord,
        *,
        depth: int,
    ) -> list[_EntityRecord]:
        records = [root]
        queue: deque[tuple[str, int]] = deque([(root.iri, 0)])
        visited = {root.iri}
        while queue and len(visited) < self.max_nodes:
            current_iri, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            for edge in self._adjacency.get(current_iri, []):
                target_iri = edge["to"]
                if target_iri in visited:
                    continue
                visited.add(target_iri)
                target_record = self._records_by_iri.get(target_iri)
                if target_record and target_record.kind == "class":
                    records.append(target_record)
                    queue.append((target_iri, current_depth + 1))
        return records[: self.max_results]

    def _record_matches_class(self, record: _EntityRecord, target_class: Any) -> bool:
        try:
            if record.kind == "class":
                return target_class in record.entity.ancestors()
            if record.kind == "individual":
                return any(
                    isinstance(entity_type, ThingClass)
                    and target_class in entity_type.ancestors()
                    for entity_type in list(getattr(record.entity, "is_a", []) or [])
                )
        except Exception:
            return False
        return False

    def _mapping_annotations(self, entity: Any, terms: set[str]) -> list[dict[str, Any]]:
        if self.world is None:
            return []
        mappings = []
        for prop in self.world.annotation_properties():
            normalized_name = self._normalize(str(getattr(prop, "name", ""))).replace(" ", "")
            if normalized_name not in terms:
                continue
            try:
                values = self._property_values(prop, entity)
            except Exception:
                continue
            for value in values:
                mappings.append(
                    {
                        "value": self._json_value(value),
                        "annotation": self._entity_ref(prop),
                        "confidence": 1.0,
                        "source": "explicit_ontology_annotation",
                    }
                )
        return mappings

    @staticmethod
    def _is_lineage_question(question: str) -> bool:
        normalized = OntologyService._normalize(question)
        compact = normalized.replace(" ", "")
        return any(
            term in normalized or term.replace(" ", "") in compact
            for term in (
                "lineage",
                "upstream",
                "downstream",
                "dependency",
                "血缘",
                "上游",
                "下游",
                "依赖",
            )
        )

    @staticmethod
    def _snake_case(value: str) -> str:
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value or ""))
        text = re.sub(r"[^A-Za-z0-9]+", "_", text)
        return text.strip("_").lower()

    def _describe_record(self, record: _EntityRecord) -> dict[str, Any]:
        entity = record.entity
        description: dict[str, Any] = {
            **self._entity_ref(entity),
            "labels": list(record.labels),
            "comments": list(record.comments),
            "ontology": str(getattr(getattr(entity, "namespace", None), "base_iri", "")),
        }

        if isinstance(entity, ThingClass):
            parents = []
            restrictions = []
            for parent in list(getattr(entity, "is_a", []) or []):
                if isinstance(parent, Restriction):
                    restrictions.append(self._json_value(parent))
                elif self._known_iri(parent):
                    parents.append(self._entity_ref(parent))
            description.update(
                {
                    "parents": parents,
                    "ancestors": self._limited_entity_refs(entity.ancestors()),
                    "descendants": self._limited_entity_refs(entity.descendants()),
                    "equivalent_to": [self._json_value(value) for value in list(entity.equivalent_to)],
                    "restrictions": restrictions,
                    "properties": self._properties_for_class(entity),
                    "instance_count": len(list(entity.instances())),
                    "instance_examples": self._limited_entity_refs(entity.instances(), limit=5),
                }
            )
        elif isinstance(entity, PropertyClass):
            description.update(self._property_summary(entity))
            if isinstance(entity, ObjectPropertyClass):
                description["inverse_property"] = self._json_value(
                    getattr(entity, "inverse_property", None)
                )
            try:
                relations = list(entity.get_relations())[:5]
                description["relation_examples"] = [
                    {"subject": self._json_value(subject), "object": self._json_value(value)}
                    for subject, value in relations
                ]
            except Exception as exc:
                description["relation_examples"] = []
                description["relation_warning"] = str(exc)
        elif isinstance(entity, Thing):
            assertions = []
            for prop in list(entity.get_properties()):
                try:
                    values = self._property_values(prop, entity)
                except Exception as exc:
                    values = [{"error": str(exc)}]
                assertions.append(
                    {"property": self._entity_ref(prop), "values": [self._json_value(value) for value in values]}
                )
            inverse_assertions = []
            try:
                for subject, prop in entity.get_inverse_properties():
                    inverse_assertions.append(
                        {"subject": self._entity_ref(subject), "property": self._entity_ref(prop)}
                    )
            except Exception as exc:
                inverse_assertions.append({"warning": str(exc)})
            description.update(
                {
                    "types": self._limited_entity_refs(getattr(entity, "is_a", [])),
                    "assertions": assertions,
                    "inverse_assertions": inverse_assertions,
                }
            )
        return description

    def _properties_for_class(self, class_entity: Any) -> list[dict[str, Any]]:
        if self.world is None:
            return []
        ancestors = set(class_entity.ancestors())
        properties = []
        for prop in self.world.properties():
            domains = list(getattr(prop, "domain", []) or [])
            if any(domain in ancestors for domain in domains):
                properties.append(self._property_summary(prop))
            if len(properties) >= self.max_results:
                break
        return properties

    def _property_summary(self, prop: Any) -> dict[str, Any]:
        record = self._records_by_iri.get(str(getattr(prop, "iri", "") or ""))
        return {
            **self._entity_ref(prop),
            "labels": list(record.labels) if record else [],
            "comments": list(record.comments) if record else [],
            "domain": [self._json_value(value) for value in list(getattr(prop, "domain", []) or [])],
            "range": [self._json_value(value) for value in list(getattr(prop, "range", []) or [])],
            "characteristics": [
                str(getattr(value, "name", value))
                for value in list(getattr(prop, "is_a", []) or [])
                if str(getattr(value, "name", value)) not in {"ObjectProperty", "DataProperty", "AnnotationProperty"}
            ],
        }

    def _search_records(
        self,
        query: str,
        *,
        entity_types: Optional[Iterable[str]] = None,
        limit: Optional[int] = None,
    ) -> list[tuple[_EntityRecord, float, str]]:
        raw_query = str(query or "").strip()
        normalized_query = self._normalize(raw_query)
        if not normalized_query:
            return []
        allowed_types = {str(value).strip().lower() for value in entity_types or []}
        bounded_limit = min(limit or self.max_results, self.max_results)
        scored: list[tuple[_EntityRecord, float, str]] = []

        for record in self._records:
            if allowed_types and not self._kind_allowed(record.kind, allowed_types):
                continue
            score, strategy = self._score_record(record, raw_query, normalized_query)
            if score >= self.fuzzy_threshold:
                scored.append((record, score, strategy))

        scored.sort(key=lambda item: (-item[1], item[0].kind, item[0].name.casefold()))
        return scored[:bounded_limit]

    def _score_record(
        self,
        record: _EntityRecord,
        raw_query: str,
        normalized_query: str,
    ) -> tuple[float, str]:
        if raw_query == record.iri:
            return 1.0, "exact_iri"
        if normalized_query in record.normalized_names:
            return (1.0 if normalized_query == self._normalize(record.name) else 0.98), "normalized_exact"

        best = 0.0
        strategy = "fuzzy"
        query_tokens = set(normalized_query.split())
        for candidate in record.normalized_names:
            if not candidate:
                continue
            if normalized_query in candidate or candidate in normalized_query:
                candidate_tokens = candidate.split()
                substring_score = (
                    0.94
                    if candidate in normalized_query and len(candidate_tokens) > 1
                    else 0.88
                    if min(len(normalized_query), len(candidate)) >= 3
                    else 0.72
                )
                if substring_score > best:
                    best, strategy = substring_score, "substring"
            candidate_token_set = set(candidate.split())
            embedded_cjk_tokens = [
                token
                for token in candidate_token_set
                if len(token) >= 2
                and re.fullmatch(r"[\u3400-\u9fff]+", token)
                and token in normalized_query
            ]
            if embedded_cjk_tokens and best < 0.86:
                best, strategy = 0.86, "label_token_in_query"
            if query_tokens and candidate_token_set:
                shared_count = len(query_tokens & candidate_token_set)
                overlap = shared_count / len(query_tokens | candidate_token_set)
                token_score = 0.60 + (0.30 * overlap)
                if shared_count and token_score > best:
                    best, strategy = token_score, "token_overlap"
                candidate_coverage = shared_count / len(candidate_token_set)
                coverage_score = 0.62 + (0.28 * candidate_coverage)
                if shared_count and coverage_score > best:
                    best, strategy = coverage_score, "candidate_token_coverage"
            fuzzy_score = SequenceMatcher(None, normalized_query, candidate).ratio()
            if fuzzy_score > best:
                best, strategy = fuzzy_score, "fuzzy"

        if normalized_query in record.search_text and best < 0.76:
            best, strategy = 0.76, "comment_or_label_context"
        return min(best, 1.0), strategy

    def _resolve_record(
        self,
        query: str,
        *,
        entity_types: Optional[Iterable[str]] = None,
    ) -> tuple[Optional[_EntityRecord], list[tuple[_EntityRecord, float, str]], str]:
        matches = self._search_records(query, entity_types=entity_types)
        if not matches:
            return None, [], "no_match"
        if matches[0][2] == "exact_iri":
            return matches[0][0], matches, "ok"
        automatic_resolution_threshold = max(self.fuzzy_threshold, 0.8)
        if matches[0][1] < automatic_resolution_threshold:
            return None, matches, "no_match"
        if len(matches) > 1:
            top_score = matches[0][1]
            second_score = matches[1][1]
            if abs(top_score - second_score) < 0.03:
                return None, matches, "ambiguous"
        return matches[0][0], matches, "ok"

    def _resolution_envelope(
        self,
        query: str,
        matches: list[tuple[_EntityRecord, float, str]],
        status: str,
    ) -> dict[str, Any]:
        rendered = [self._match_payload(*match) for match in matches]
        return self._envelope(
            status,
            matches=rendered,
            confidence=matches[0][1] if matches else 0.0,
            strategies_tried=self._search_strategies(),
            unresolved=[query],
        )

    def _match_payload(
        self,
        record: _EntityRecord,
        score: float,
        strategy: str,
    ) -> dict[str, Any]:
        return {
            **self._entity_ref(record.entity),
            "labels": list(record.labels),
            "score": round(score, 4),
            "strategy": strategy,
            "evidence": {"iri": record.iri, "source": "ontology", "strategy": strategy},
        }

    def _path_payload(self, source_iri: str, edges: list[dict[str, Any]]) -> dict[str, Any]:
        semantic_path: list[str] = [self._display_name(source_iri)]
        rendered_edges = []
        for edge in edges:
            semantic_path.extend([edge["relation"], self._display_name(edge["to"])])
            rendered_edges.append(self._render_edge(edge))
        return {"semantic_path": semantic_path, "edges": rendered_edges}

    def _render_edge(self, edge: dict[str, Any]) -> dict[str, Any]:
        return {
            **edge,
            "from_entity": self._json_value(self._records_by_iri.get(edge["from"], None).entity if edge["from"] in self._records_by_iri else edge["from"]),
            "to_entity": self._json_value(self._records_by_iri.get(edge["to"], None).entity if edge["to"] in self._records_by_iri else edge["to"]),
        }

    def _display_name(self, iri: str) -> str:
        record = self._records_by_iri.get(iri)
        return record.name if record else iri

    def _entity_ref(self, entity: Any) -> dict[str, Any]:
        iri = str(getattr(entity, "iri", "") or "")
        record = self._records_by_iri.get(iri)
        return {
            "iri": iri,
            "name": str(getattr(entity, "name", "") or iri),
            "type": record.kind if record else self._entity_kind(entity),
        }

    def _json_value(self, value: Any, *, depth: int = 0) -> Any:
        if depth > 5:
            return str(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Restriction):
            return {
                "construct": "restriction",
                "property": self._json_value(getattr(value, "property", None), depth=depth + 1),
                "restriction": _RESTRICTION_NAMES.get(getattr(value, "type", None), str(getattr(value, "type", "unknown"))),
                "cardinality": getattr(value, "cardinality", None),
                "value": self._json_value(getattr(value, "value", None), depth=depth + 1),
            }
        if isinstance(value, ClassConstruct):
            classes = getattr(value, "Classes", None)
            return {
                "construct": value.__class__.__name__,
                "values": [self._json_value(item, depth=depth + 1) for item in classes] if classes else [],
                "expression": str(value),
            }
        if hasattr(value, "iri"):
            return self._entity_ref(value)
        if isinstance(value, dict):
            return {str(key): self._json_value(item, depth=depth + 1) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._json_value(item, depth=depth + 1) for item in value]
        return str(value)

    @staticmethod
    def _property_values(prop: Any, entity: Any) -> list[Any]:
        value = prop[entity]
        if value is None:
            return []
        if isinstance(value, (list, tuple, set, frozenset)):
            return list(value)
        return [value]

    def _limited_entity_refs(self, values: Iterable[Any], *, limit: Optional[int] = None) -> list[dict[str, Any]]:
        result = []
        for value in values:
            if self._known_iri(value):
                result.append(self._entity_ref(value))
            if len(result) >= (limit or self.max_results):
                break
        return result

    def _known_iri(self, entity: Any) -> str:
        iri = str(getattr(entity, "iri", "") or "")
        return iri if iri in self._records_by_iri or not self._records else ""

    @staticmethod
    def _entity_kind(entity: Any) -> str:
        if isinstance(entity, ThingClass):
            return "class"
        if isinstance(entity, ObjectPropertyClass):
            return "object_property"
        if isinstance(entity, DataPropertyClass):
            return "data_property"
        if isinstance(entity, AnnotationPropertyClass):
            return "annotation_property"
        if isinstance(entity, Thing):
            return "individual"
        return "construct"

    @staticmethod
    def _kind_allowed(kind: str, allowed: set[str]) -> bool:
        if kind in allowed:
            return True
        return "property" in allowed and kind.endswith("_property")

    @staticmethod
    def _literal_items(values: Iterable[Any]) -> list[dict[str, str]]:
        result = []
        for value in list(values or []):
            result.append(
                {
                    "value": str(value),
                    "language": str(getattr(value, "lang", "") or ""),
                }
            )
        return result

    @staticmethod
    def _normalize(value: str) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        text = text.casefold()
        text = re.sub(r"[_\-/]+", " ", text)
        text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text, flags=re.UNICODE)
        return " ".join(text.split())

    @staticmethod
    def _search_strategies() -> list[str]:
        return [
            "exact_iri",
            "normalized_exact_name_or_label",
            "type_constrained_match",
            "token_or_substring_match",
            "multilingual_label_token_in_query",
            "candidate_token_coverage",
            "fuzzy_match",
        ]

    @staticmethod
    def _envelope(
        status: str,
        *,
        data: Any = None,
        matches: Optional[list[Any]] = None,
        evidence: Optional[list[Any]] = None,
        confidence: float = 0.0,
        strategies_tried: Optional[list[str]] = None,
        warnings: Optional[list[str]] = None,
        unresolved: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "data": data,
            "matches": matches or [],
            "evidence": evidence or [],
            "confidence": round(float(confidence or 0.0), 4),
            "strategies_tried": strategies_tried or [],
            "warnings": warnings or [],
            "unresolved": unresolved or [],
        }

    def _require_loaded(self) -> None:
        if not self.loaded or self.world is None:
            raise RuntimeError("Ontology service is not loaded")
