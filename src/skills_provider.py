"""Native Microsoft Agent Framework Skill providers for application agents."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Optional, Sequence

from agent_framework import FileSkillsSource, Skill, SkillsProvider, SkillsSourceContext

_PROJECT_ROOT = Path(__file__).parent.parent
_SKILLS_ROOT = _PROJECT_ROOT / "skills"

_AGENT_SKILL_DIRECTORIES: dict[str, tuple[str, ...]] = {
    "DataInsightAgent": ("analytics-spec", "sql-planning"),
    "MetadataAgent": ("metadata-mapping",),
    "OntologyAgent": ("analytics-spec",),
}


@dataclass
class _SkillUsage:
    loaded_skills: set[str]
    read_resources: set[tuple[str, str]]


_SKILL_USAGE: ContextVar[Optional[_SkillUsage]] = ContextVar(
    "agent_skill_usage",
    default=None,
)


class _TrackingSkillsProvider(SkillsProvider):
    """Record successful native Skill operations in the current request context."""

    async def _load_skill(self, skills: Sequence[Skill], skill_name: str) -> str:
        result = await super()._load_skill(skills, skill_name)
        skill = self._find_skill(skills, skill_name)
        if skill is not None and not result.startswith("Error:"):
            usage = _SKILL_USAGE.get()
            if usage is not None:
                usage.loaded_skills.add(skill.frontmatter.name.casefold())
        return result

    async def _read_skill_resource(
        self,
        skills: Sequence[Skill],
        skill_name: str,
        resource_name: str,
        **kwargs: Any,
    ) -> Any:
        result = await super()._read_skill_resource(
            skills,
            skill_name,
            resource_name,
            **kwargs,
        )
        skill = self._find_skill(skills, skill_name)
        failed = isinstance(result, str) and result.startswith("Error:")
        if skill is not None and not failed:
            usage = _SKILL_USAGE.get()
            if usage is not None:
                usage.read_resources.add(
                    (
                        skill.frontmatter.name.casefold(),
                        resource_name.casefold(),
                    )
                )
        return result


def begin_skill_usage_tracking() -> Token[Optional[_SkillUsage]]:
    """Start an isolated Skill-usage scope for one agent request."""
    return _SKILL_USAGE.set(_SkillUsage(set(), set()))


def reset_skill_usage_tracking(token: Token[Optional[_SkillUsage]]) -> None:
    """Restore the Skill-usage scope that preceded a request."""
    _SKILL_USAGE.reset(token)


def skill_was_loaded(skill_name: str) -> bool:
    """Return whether the named Skill was successfully loaded in this request."""
    usage = _SKILL_USAGE.get()
    return usage is not None and skill_name.casefold() in usage.loaded_skills


def skill_resource_was_read(skill_name: str, resource_name: str) -> bool:
    """Return whether the named Skill resource was read in this request."""
    usage = _SKILL_USAGE.get()
    if usage is None:
        return False
    return (
        skill_name.casefold(),
        resource_name.casefold(),
    ) in usage.read_resources


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_skills_provider(agent_name: str) -> Optional[SkillsProvider]:
    """Create a native, agent-scoped file Skill provider.

    Read-only Skill operations are trusted for repository-owned Skill files.
    Script execution remains approval-gated and no script runner is configured.
    """
    directory_names = _AGENT_SKILL_DIRECTORIES.get(agent_name, ())
    skill_paths = [
        _SKILLS_ROOT / directory_name
        for directory_name in directory_names
        if (_SKILLS_ROOT / directory_name / "SKILL.md").is_file()
    ]
    if not skill_paths:
        return None

    return _TrackingSkillsProvider.from_paths(
        skill_paths=skill_paths,
        resource_extensions=(".md", ".json", ".yaml", ".yml", ".csv", ".xml", ".txt", ".sql"),
        disable_caching=_env_flag("SKILLS_DISABLE_CACHING"),
        disable_load_skill_approval=True,
        disable_read_skill_resource_approval=True,
        disable_run_skill_script_approval=False,
        source_id=f"{agent_name}-skills",
    )


def configured_skill_names(agent_name: str) -> tuple[str, ...]:
    """Return the repository Skill names assigned to an agent."""
    return _AGENT_SKILL_DIRECTORIES.get(agent_name, ())


def configured_skill_resource_exists(
    agent_name: str,
    skill_name: str,
    resource_name: str,
) -> bool:
    """Return whether a resource is safely contained in an assigned Skill."""
    if skill_name not in configured_skill_names(agent_name) or not resource_name:
        return False
    skill_root = (_SKILLS_ROOT / skill_name).resolve()
    resource_path = (skill_root / resource_name).resolve()
    return resource_path.is_relative_to(skill_root) and resource_path.is_file()


def read_configured_skill(agent_name: str, skill_name: str) -> str:
    """Read one repository-owned Skill assigned to an agent."""
    if skill_name not in configured_skill_names(agent_name):
        raise ValueError(f"Skill {skill_name!r} is not assigned to {agent_name}")
    skill_path = _SKILLS_ROOT / skill_name / "SKILL.md"
    return skill_path.read_text(encoding="utf-8")


async def list_skill_metadata(agent) -> list[dict[str, object]]:
    """Discover repository Skill metadata through MAF's native file source."""
    source = FileSkillsSource(
        _SKILLS_ROOT,
        resource_extensions=(".md", ".json", ".yaml", ".yml", ".csv", ".xml", ".txt", ".sql"),
    )
    skills = await source.get_skills(
        SkillsSourceContext(agent=agent, session=agent.create_session())
    )
    result: list[dict[str, object]] = []
    for skill in sorted(skills, key=lambda item: item.frontmatter.name):
        metadata = skill.frontmatter.metadata or {}
        raw_tags = metadata.get("tags", "")
        tags = [tag.strip() for tag in raw_tags.split(",") if tag.strip()]
        result.append(
            {
                "name": skill.frontmatter.name,
                "description": skill.frontmatter.description,
                "tags": tags,
            }
        )
    return result
