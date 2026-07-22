"""Native Microsoft Agent Framework Skill providers for application agents."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from agent_framework import FileSkillsSource, SkillsProvider, SkillsSourceContext

_PROJECT_ROOT = Path(__file__).parent.parent
_SKILLS_ROOT = _PROJECT_ROOT / "skills"

_AGENT_SKILL_DIRECTORIES: dict[str, tuple[str, ...]] = {
    "DataInsightAgent": ("analytics-spec",),
    "MetadataAgent": ("metadata-mapping",),
}


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

    return SkillsProvider.from_paths(
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
