"""
Skill Injector — dynamically injects skill context into agent system prompts.

Design
------
* Two modes of injection:
  1. **Metadata list** — injects an XML block with all available skills' names and descriptions.
     Small token footprint; used in every system prompt so the LLM knows what's available.
  2. **Full body** — appends the complete Markdown body of a specific skill into the conversation
     context when the LLM decides to use it.

* Agents that receive skill injection: all sub-agents (SearchAgent, DataInsightAgent,
  MetadataAgent).  The MasterAgent orchestrates which skill is loaded; sub-agents consume
  the full body as additional context appended to their instructions.

Usage
-----
    from src.injector import skill_injector

    # Build the <available_skills> XML block for system prompts
    xml_block = skill_injector.build_available_skills_xml()

    # Inject metadata into a prompt template that has a {skills_context} placeholder
    enriched_prompt = skill_injector.inject_skills_metadata(prompt_template)

    # Get the full body of a skill that the LLM chose to use
    full_context = skill_injector.load_skill_full_body("metadata-mapping")
"""

from __future__ import annotations

from typing import Optional
import hashlib

from src.registry import skill_registry, SkillMeta
from src.utils import get_logger

logger = get_logger(__name__)


class SkillInjector:
    """
    Formats skill information for injection into agent prompts and conversation context.

    XML format chosen for GPT-5.1 (OpenAI o-series / GPT-4 class models) because:
    - Explicit structure the model can parse unambiguously
    - Keeps each element clearly delimited
    - Easy to extend without touching model logic
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def build_available_skills_xml(self) -> str:
        """
        Build an XML block listing all currently indexed skills.

        Example output:
            <available_skills>
              <skill>
                <name>metadata-mapping</name>
                <description>Maps Azure Databricks UC column names to business terms.</description>
                <location>/path/to/SKILL.md</location>
              </skill>
              ...
            </available_skills>

        Returns an empty ``<available_skills />`` tag when no skills are registered.
        """
        skills = skill_registry.list_skills()

        if not skills:
            return "<available_skills />"

        lines = ["<available_skills>"]
        for skill in skills:
            lines.append("  <skill>")
            lines.append(f"    <name>{self._escape_xml(skill.name)}</name>")
            lines.append(
                f"    <description>{self._escape_xml(skill.description)}</description>"
            )
            lines.append(f"    <location>{skill.path}</location>")
            if skill.tags:
                tags_str = ", ".join(self._escape_xml(t) for t in skill.tags)
                lines.append(f"    <tags>{tags_str}</tags>")
            lines.append("  </skill>")
        lines.append("</available_skills>")
        return "\n".join(lines)

    def inject_skills_metadata(self, prompt_template: str) -> str:
        """
        Replace the ``{skills_context}`` placeholder in *prompt_template* with the
        XML skills listing.  If no placeholder is present the template is returned unchanged.

        Parameters
        ----------
        prompt_template:
            A system-prompt string that may contain ``{skills_context}``.

        Returns
        -------
        str
            Prompt with skills metadata injected.
        """
        xml_block = self.build_available_skills_xml()
        if "{skills_context}" in prompt_template:
            return prompt_template.replace("{skills_context}", xml_block)
        # No placeholder — append at the end
        return prompt_template + f"\n\n## Available Skills\n{xml_block}"

    def load_skill_full_body(self, skill_name: str) -> Optional[str]:
        """
        Load the full Markdown body of *skill_name* and wrap it in an XML context block
        suitable for appending to a conversation message or system prompt.

        Returns None if the skill is not found in the registry.
        """
        meta = skill_registry.get_skill_meta(skill_name)
        body = skill_registry.get_skill_body(skill_name)
        if body is None:
            logger.warning(f"[SkillInjector] Skill '{skill_name}' body not found.")
            return None

        body_clean = body.strip()
        body_hash = hashlib.sha256(body_clean.encode("utf-8")).hexdigest()[:12]
        source_path = str(meta.path) if meta and meta.path else "<unknown>"

        context_block = (
            f"<skill_context name=\"{self._escape_xml(skill_name)}\">\n"
            f"{body_clean}\n"
            f"</skill_context>"
        )
        logger.info(
            f"[SkillInjector] Loaded full body for skill '{skill_name}' "
            f"from '{source_path}' (chars={len(body_clean)}, sha256_12={body_hash})."
        )
        return context_block

    def build_skill_selection_info(self, question: str) -> str:
        """
        Return a compact summary string listing available skills with their descriptions.
        Intended for inclusion in tool call context when the agent needs to decide
        which skill to load.

        Parameters
        ----------
        question:
            The user question (not used for filtering here; reserved for future
            semantic-matching extension).
        """
        skills = skill_registry.list_skills()
        if not skills:
            return "No skills currently available."

        lines = ["Available skills (use the skill name to load the full instructions):"]
        for skill in skills:
            tag_str = f"  [tags: {', '.join(skill.tags)}]" if skill.tags else ""
            lines.append(f"  • {skill.name}: {skill.description}{tag_str}")
        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _escape_xml(text: str) -> str:
        """Escape characters that are special in XML."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )


# Module-level singleton
skill_injector = SkillInjector()
