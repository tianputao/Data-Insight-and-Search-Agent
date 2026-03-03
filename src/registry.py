"""
Skill Registry — discovers and indexes SKILL.md files from the /skills/ directory.

Architecture
------------
* Startup scan: walks `<project_root>/skills/` to find every SKILL.md file.
* YAML front-matter: uses `python-frontmatter` to extract metadata (name, description, tags).
* Progressive disclosure: only front-matter is loaded at startup; full body is read on demand.
* Thread-safe: a simple Lock protects the internal index so multiple FastAPI workers can share
  the registry without races.

Typical usage
-------------
    from src.registry import skill_registry

    # At app startup
    skill_registry.scan()

    # Get the skills index (list of SkillMeta)
    skills = skill_registry.list_skills()

    # Load the full body of one skill on demand
    body = skill_registry.get_skill_body("metadata-mapping")
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

try:
    import frontmatter  # python-frontmatter library
    _FRONTMATTER_AVAILABLE = True
except ImportError:
    _FRONTMATTER_AVAILABLE = False

from src.utils import get_logger

logger = get_logger(__name__)

# Root of the project (two levels up from this file: src/ → project root)
_PROJECT_ROOT = Path(__file__).parent.parent
_SKILLS_DIR = _PROJECT_ROOT / "skills"


@dataclass
class SkillMeta:
    """Lightweight metadata record for a skill — loaded at startup."""

    name: str
    description: str
    tags: List[str] = field(default_factory=list)
    path: Path = field(default_factory=Path)

    def to_dict(self) -> dict:
        """Serialise to a plain dict (used by the API)."""
        return {
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
        }


class SkillRegistry:
    """
    Singleton registry that discovers SKILL.md files and exposes their metadata.

    Design goals
    ------------
    * Fast startup — only front-matter is read at scan time.
    * On-demand body loading — full Markdown body is read only when an agent needs it.
    * Re-scannable — call `scan()` again to reload without restarting the process.
    """

    def __init__(self, skills_dir: Path = _SKILLS_DIR) -> None:
        self._skills_dir = skills_dir
        self._index: dict[str, SkillMeta] = {}  # name → SkillMeta
        self._lock = threading.Lock()

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def scan(self) -> int:
        """
        Walk the skills directory, parse YAML front-matter from every SKILL.md,
        and rebuild the internal index.

        Returns
        -------
        int
            Number of skills successfully indexed.
        """
        if not self._skills_dir.exists():
            logger.warning(
                f"Skills directory not found: {self._skills_dir}. "
                "Creating it and continuing with an empty skill set."
            )
            self._skills_dir.mkdir(parents=True, exist_ok=True)
            return 0

        discovered: dict[str, SkillMeta] = {}

        for skill_md in sorted(self._skills_dir.rglob("SKILL.md")):
            meta = self._parse_skill_meta(skill_md)
            if meta is not None:
                discovered[meta.name] = meta
                logger.debug(f"[SkillRegistry] Indexed skill: '{meta.name}' @ {skill_md}")

        with self._lock:
            self._index = discovered

        logger.info(f"[SkillRegistry] Scan complete — {len(discovered)} skill(s) indexed.")
        return len(discovered)

    def list_skills(self) -> List[SkillMeta]:
        """Return all currently indexed skills (sorted by name)."""
        with self._lock:
            return sorted(self._index.values(), key=lambda s: s.name)

    def get_skill_meta(self, name: str) -> Optional[SkillMeta]:
        """Return the SkillMeta for *name*, or None if not found."""
        with self._lock:
            return self._index.get(name)

    def get_skill_body(self, name: str) -> Optional[str]:
        """
        Load and return the full Markdown body of the named skill.
        The body is read from disk on every call (not cached) to avoid stale content.

        Returns None if the skill is not registered.
        """
        with self._lock:
            meta = self._index.get(name)

        if meta is None:
            logger.warning(f"[SkillRegistry] Skill '{name}' not found in registry.")
            return None

        skill_md_path = meta.path
        try:
            if _FRONTMATTER_AVAILABLE:
                post = frontmatter.load(str(skill_md_path))
                return post.content  # Markdown body without front-matter
            else:
                # Fallback: strip the YAML block manually
                raw = skill_md_path.read_text(encoding="utf-8")
                return self._strip_frontmatter(raw)
        except Exception as exc:
            logger.error(f"[SkillRegistry] Failed to read skill body '{name}': {exc}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _parse_skill_meta(self, skill_md_path: Path) -> Optional[SkillMeta]:
        """Parse YAML front-matter from a SKILL.md file and return a SkillMeta."""
        try:
            if _FRONTMATTER_AVAILABLE:
                post = frontmatter.load(str(skill_md_path))
                name = post.get("name") or skill_md_path.parent.name
                description = post.get("description", "")
                tags = post.get("tags", [])
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
            else:
                # Minimal YAML parser fallback
                raw = skill_md_path.read_text(encoding="utf-8")
                meta_dict = self._parse_yaml_frontmatter(raw)
                name = meta_dict.get("name") or skill_md_path.parent.name
                description = meta_dict.get("description", "")
                tags = meta_dict.get("tags", [])
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]

            return SkillMeta(
                name=str(name),
                description=str(description),
                tags=tags if isinstance(tags, list) else [],
                path=skill_md_path,
            )

        except Exception as exc:
            logger.warning(
                f"[SkillRegistry] Could not parse metadata from {skill_md_path}: {exc}"
            )
            return None

    @staticmethod
    def _strip_frontmatter(raw: str) -> str:
        """Remove YAML front-matter delimiters and return the body."""
        if not raw.startswith("---"):
            return raw
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
        return raw

    @staticmethod
    def _parse_yaml_frontmatter(raw: str) -> dict:
        """Very small YAML key-value parser (no external dependency)."""
        result: dict = {}
        if not raw.startswith("---"):
            return result
        parts = raw.split("---", 2)
        if len(parts) < 3:
            return result
        for line in parts[1].splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                result[key.strip()] = value.strip()
        return result


# ─── Module-level singleton ────────────────────────────────────────────────────
skill_registry = SkillRegistry()
