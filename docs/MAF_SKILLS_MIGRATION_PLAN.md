# MAF Native Skills Migration Plan

## Goal

Migrate the application from the custom `SkillRegistry` / `SkillInjector` runtime to Microsoft Agent Framework native `SkillsProvider`, while preserving the existing Python agents, React/Vite Activity UI, multi-agent delegation, SSE streaming, Azure AI Search, Databricks analytics, citations, and Anthropic-inspired skill progress experience.

The directory `anthropic-ai-claude-code-2.1.88-restored/` is read-only reference material and must never be modified.

## Migration Principles

- Keep backend and agents in Python; keep frontend in TypeScript + React + Vite.
- Preserve existing user-facing behavior unless a change is required by the MAF upgrade.
- Use MAF native skill discovery, advertisement, `load_skill`, resource access, caching, filtering, and approval semantics.
- Keep the existing Activity protocol/UI as the product presentation layer for skill and agent progress.
- Make each phase independently testable and repair regressions before advancing.
- Do not delete the custom skill implementation until native-provider behavior is verified end to end.

## Phase 1: Isolated Compatibility Probe

Status: complete

Verified target:
- `agent-framework==1.11.0`
- `agent-framework-core==1.11.0`
- `agent-framework-openai==1.10.1` (declares `agent-framework-core>=1.11.0,<2`)

Verified breaking changes:
- `AzureOpenAIChatClient` moved to the split OpenAI provider and is replaced by `OpenAIChatClient` / `OpenAIChatCompletionClient`.
- Azure constructor arguments are `azure_endpoint` and `model`.
- Agent context accepts `context_providers=[...]`.
- `get_new_thread()` is replaced by `create_session()`.
- `run_stream()` is replaced by `run(..., stream=True, session=...)`.

1. Install `agent-framework==1.11.0` and `agent-framework-core==1.11.0` in a temporary virtual environment.
2. Verify imports for:
   - `AzureOpenAIChatClient`
   - `SkillsProvider`, `FileSkillsSource`, `FilteringSkillsSource`
   - agent construction and context-provider arguments
   - streaming response/content types
3. Inspect signatures needed by the current application.
4. Produce a compatibility map before changing project dependencies.

Exit criteria:
- The target version and exact construction APIs are known.
- Any renamed or removed APIs used by the repository are identified.

## Phase 2: Upgrade Dependencies and Restore Imports

Status: complete

1. Update pinned Agent Framework packages to the verified version.
2. Install into the project virtual environment.
3. Repair import/API breaks without changing business behavior.
4. Replace obsolete import tests with accurate import and API-contract smoke tests.

Validation:
- All application Python modules import and compile.
- Agent constructors can be instantiated with mocked/local dependencies.
- Existing frontend production build remains green.

## Phase 3: Native Skills Provider Adapter

Status: complete

1. Add a centralized Python skills provider factory.
2. Discover file skills from `skills/` using MAF `SkillsProvider.from_paths()`.
3. Configure development caching behavior and trusted read-only skill loading.
4. Filter skills by requesting agent:
   - `DataInsightAgent`: `analytics-spec` and approved analytics skills.
   - `MetadataAgent`: `metadata-mapping` and approved metadata skills.
   - `MasterAgent` / `SearchAgent`: no Databricks skills unless explicitly assigned.
5. Keep a temporary read-only adapter for `/skills` API compatibility.

Validation:
- Native provider advertises only the allowed skill metadata to each agent.
- `load_skill` returns the expected SKILL.md body.
- `analytics-spec` does not appear to unrelated agents.

## Phase 4: Migrate Agents to Native Skills

Status: complete

1. Attach native `SkillsProvider` to `DataInsightAgent` and `MetadataAgent` through the verified MAF context-provider API.
2. Remove their hand-written `load_skill` functions.
3. Remove custom XML skill injection from those agents.
4. Preserve all existing non-skill tools and prompts.
5. Keep MasterAgent and SearchAgent behavior unchanged except for required framework compatibility.

Validation:
- Highest-spending-customer query loads `analytics-spec` and executes the silver SQL template.
- Product-category and monthly-trend queries do not load `analytics-spec`.
- Metadata ambiguity can load `metadata-mapping`.
- Databricks SQL execution, retries, and timeouts still work.

## Phase 5: Activity and Approval Integration

Status: complete

1. Map native skill tool events (`load_skill`, `read_skill_resource`, `run_skill_script`) to the existing Activity protocol.
2. Preserve Anthropic-inspired UI behavior:
   - Skill-specific label and name.
   - Parent agent attribution.
   - Running/completed/error lifecycle.
   - Collapsible details and automatic final collapse.
3. Auto-approve trusted read-only skill tools only.
4. Keep script execution disabled or approval-gated unless an explicitly trusted runner is added.

Validation:
- Skill activity renders under the correct agent.
- No protected reasoning is exposed.
- Approval behavior cannot silently execute untrusted scripts.

## Phase 6: Remove Legacy Runtime

Status: complete

1. Remove `SkillInjector` usage from runtime code.
2. Remove duplicate hand-written `load_skill` tools.
3. Replace `SkillRegistry` runtime responsibilities with a native-provider-backed `/skills` adapter.
4. Delete legacy modules only when no references remain.
5. Update documentation and dependency comments.

Validation:
- No runtime references to `skill_injector` or custom `load_skill` remain.
- `/skills` still returns skill name, description, and tags/metadata where available.
- Skill files remain agentskills.io-compatible.

## Phase 7: Full Regression

Status: complete

Backend checks:
- Python compile/import suite.
- Agent construction and thread/session checks.
- Mock streaming contracts for text, reasoning, function call/result, and skills.
- Real RAG query: embedding, hybrid retrieval, semantic ranking, citations.
- Real data query: metadata delegation, skill load, SQL execution, final answer.
- Negative routing: category/trend queries skip `analytics-spec`.

Frontend checks:
- TypeScript compiler and Vite production build.
- Browser verification of running/completed/error activity states.
- Skill row placement and collapse behavior.
- Final answer remains separate from working narration.

Final audit:
- `git diff --check`.
- No changes under `anthropic-ai-claude-code-2.1.88-restored/`.
- Test services stopped and ports released.
