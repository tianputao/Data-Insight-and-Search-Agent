"""
FastAPI backend server — bridges the React frontend to the Python agent system.

Endpoints
---------
POST  /chat/stream          Server-Sent Events streaming chat (main endpoint)
POST  /threads/new          Create a new conversation thread
GET   /threads              List all active threads
GET   /threads/{id}/history Get message history for a thread
DELETE /threads/{id}        Delete a thread
GET   /skills               List available skills
GET   /health               Health check

SSE event format (matches what the frontend expects)
----------------------------------------------------
data: {"type": "thinking", "message": "<step description>"}
data: {"type": "text",     "content": "<response chunk>"}
data: {"type": "done"}
data: {"type": "error",    "message": "<error description>"}

Architecture
------------
* A single MasterAgent is created at startup and shared across all requests.
* Thread objects are stored in an in-memory dict keyed by thread_id (UUID string).
* The Skill Registry is scanned at startup so all agents get skill context.
* CORS is configured to allow the Vite dev server (localhost:3000) and production origins.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel

from src.agents import MasterAgent, SearchAgent, DataInsightAgent, MetadataAgent
from src.tools import AzureAISearchTool
from src.config import AppConfig, AzureSearchConfig, DatabricksConfig
from src.registry import skill_registry
from src.utils import get_logger

logger = get_logger(__name__)

# ─── Application state ─────────────────────────────────────────────────────────

class AppState:
    """Holds singletons shared across all requests."""

    master_agent: Optional[MasterAgent] = None
    # thread_id (str) → MAF thread object
    threads: Dict[str, object] = {}
    # thread_id → list of {"user": str, "assistant": str, "timestamp": str}
    thread_history: Dict[str, List[dict]] = {}
    initialized: bool = False
    init_error: Optional[str] = None


state = AppState()


# ─── Lifespan: startup / shutdown ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context — runs startup logic before serving requests."""
    logger.info("FastAPI server starting up…")

    # 1. Scan skills directory
    skill_count = skill_registry.scan()
    logger.info(f"Skills loaded: {skill_count}")

    # 2. Initialise agents
    try:
        search_tool = AzureAISearchTool(
            enable_semantic_reranker=AppConfig.DEFAULT_ENABLE_SEMANTIC_RERANKER,
            enable_agentic_retrieval=AppConfig.DEFAULT_ENABLE_AGENTIC_RETRIEVAL,
        )
        search_agent = SearchAgent(search_tool=search_tool)

        # MetadataAgent and DataInsightAgent are optional (need Databricks config)
        metadata_agent: Optional[MetadataAgent] = None
        data_insight_agent: Optional[DataInsightAgent] = None

        metadata_agent = MetadataAgent()
        logger.info("MetadataAgent initialised.")

        data_insight_agent = DataInsightAgent(metadata_agent=metadata_agent)
        logger.info("DataInsightAgent initialised.")

        state.master_agent = MasterAgent(
            search_agent=search_agent,
            data_insight_agent=data_insight_agent,
            metadata_agent=metadata_agent,
        )
        state.initialized = True
        logger.info("MasterAgent initialised successfully.")

    except Exception as exc:
        state.init_error = str(exc)
        state.initialized = False
        logger.error(f"Agent initialisation failed: {exc}", exc_info=True)

    yield  # Server is running

    logger.info("FastAPI server shutting down.")


# ─── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="MAF Data Insight Agent API",
    description="Enterprise AI agent backend (search + data insight + metadata)",
    version="1.0.0",
    lifespan=lifespan,
)

# Allow Vite dev server and same-origin production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Pydantic models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    thread_id: Optional[str] = None


class NewThreadRequest(BaseModel):
    thread_id: Optional[str] = None


class ThreadInfo(BaseModel):
    thread_id: str
    message_count: int
    last_updated: str


# ─── SSE streaming helper ───────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    """Format a dict as a Server-Sent Events data line."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _ensure_blob_sas_url(url: str, is_image: bool = False) -> str:
    """Ensure a blob storage URL carries SAS params for private storage accounts.

    IMPORTANT: The SAS token is appended as a raw query string rather than being
    decoded through parse_qsl / urlencode.  The round-trip decode-then-encode can
    silently corrupt the 'sig' field because urllib.parse.parse_qsl treats '+' as
    a space (HTML form-data convention), which changes the base64 signature and
    causes Azure to return AuthenticationFailed / 'Signature not well formed'.
    """
    if not url or "blob.core.windows.net" not in url:
        return url

    token = AzureSearchConfig.IMAGE_SAS_TOKEN if is_image else AzureSearchConfig.SAS_TOKEN
    if not token:
        return url

    parsed = urlsplit(url.strip().replace("<", "").replace(">", ""))

    # Check for existing SAS signature using decoded key names (safe — we only
    # inspect keys, not values).
    existing_keys = {k.lower() for k, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if "sig" in existing_keys:
        return url

    # Append the raw token string verbatim — NO decode / re-encode cycle — so the
    # sig value is never mangled.
    token_clean = token.lstrip("?&")
    sep = "&" if parsed.query else ""
    new_query = parsed.query + sep + token_clean
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))


def _patch_blob_urls_with_sas(text: str) -> str:
    """
    Patch all blob.core.windows.net URLs in model output with SAS tokens.
    Handles markdown links/images and bare URLs in references.
    """
    if not text or "blob.core.windows.net" not in text:
        return text

    blob_url_pattern = re.compile(r"https://[^\s\]\)>\"']*blob\.core\.windows\.net[^\s\]\)>\"']*", re.IGNORECASE)

    def _repl(match: re.Match) -> str:
        raw = match.group(0)
        trail = ""
        while raw and raw[-1] in ".,;":
            trail = raw[-1] + trail
            raw = raw[:-1]
        is_image = "pictureindoc" in raw.lower()
        return _ensure_blob_sas_url(raw, is_image=is_image) + trail

    return blob_url_pattern.sub(_repl, text)


def _extract_search_references(result_text: str) -> Dict[str, tuple[str, str]]:
    """
    Parse search_knowledge tool result text into citation reference map:
    {"1": ("title", "url"), ...}
    """
    refs: Dict[str, tuple[str, str]] = {}
    if not result_text:
        return refs

    current_num: Optional[str] = None
    current_title: Optional[str] = None

    for raw_line in result_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = re.match(r"\[(\d+)\]\s*(.*)$", line)
        if m:
            current_num = m.group(1)
            current_title = m.group(2).strip()
            continue

        if current_num and line.startswith("Source:"):
            url = line.split("Source:", 1)[1].strip()
            if url and "Internal Document" not in url:
                refs[current_num] = (current_title or f"Reference {current_num}", url)
            else:
                refs[current_num] = (current_title or f"Reference {current_num}", "")
            current_num = None
            current_title = None

    return refs


def _clean_reference_url(raw_url: str) -> str:
    """Normalize a markdown reference URL and strip optional title suffix."""
    if not raw_url:
        return ""

    candidate = str(raw_url).strip()
    if not candidate:
        return ""

    candidate = candidate.strip("<>").strip()

    # Markdown destination may contain optional title text: (url "title")
    if " " in candidate:
        candidate = candidate.split()[0].strip()

    candidate = candidate.rstrip(".,;")

    if not re.match(r"^https?://", candidate, flags=re.I):
        return ""

    return candidate


def _derive_reference_title(num: str, title: str, url: str) -> str:
    """Prefer explicit titles; otherwise infer from URL basename."""
    normalized = (title or "").strip()
    if normalized and not _is_generic_reference_title(normalized):
        return normalized

    cleaned_url = _clean_reference_url(url)
    if cleaned_url:
        parsed = urlsplit(cleaned_url)
        basename = parsed.path.rsplit("/", 1)[-1].strip()
        if basename:
            return unquote(basename)

    return f"Reference {num}"


def _extract_explicit_references_block(refs_text: str) -> Dict[str, tuple[str, str]]:
    """Parse an existing References block into {num: (title, url)}."""
    refs: Dict[str, tuple[str, str]] = {}

    # ── Pattern 1: consolidated range  [1]–[N] title\nurl  ───────────────────
    # The LLM sometimes collapses identical-document citations into one entry like:
    #   [1]–[8] GB/T 31485-2015 document title
    #   https://storage.blob.core.windows.net/...
    for m in re.finditer(
        r"\[(\d+)\][–—-]+\[(\d+)\]\s+([^\n]+?)\s*\n\s*(https?://\S+)",
        refs_text,
    ):
        start_num = int(m.group(1))
        end_num = int(m.group(2))
        title = m.group(3).strip()
        url = _clean_reference_url(m.group(4))
        if url:
            for n in range(start_num, end_num + 1):
                num_str = str(n)
                refs.setdefault(num_str, (_derive_reference_title(num_str, title, url), url))

    # Also handle consolidated range where URL is on same line after title
    for m in re.finditer(
        r"\[(\d+)\][–—-]+\[(\d+)\]\s+(.*?)\s+(https?://\S+)",
        refs_text,
    ):
        start_num = int(m.group(1))
        end_num = int(m.group(2))
        title = m.group(3).strip()
        url = _clean_reference_url(m.group(4))
        if url:
            for n in range(start_num, end_num + 1):
                num_str = str(n)
                refs.setdefault(num_str, (_derive_reference_title(num_str, title, url), url))

    # ── Pattern 2: standard markdown link  [N] [title](url)  ────────────────
    for m in re.finditer(r"\[(\d+)\]\s*\[(.*?)\]\(([^)]+)\)", refs_text, re.S):
        num = m.group(1)
        title = (m.group(2) or "").strip()
        url = _clean_reference_url(m.group(3))
        if url:
            refs.setdefault(num, (_derive_reference_title(num, title, url), url))

    # ── Pattern 3: bare URL  [N]: url  or  [N] url  ─────────────────────────
    for m in re.finditer(r"\[(\d+)\]\s*[:：]?\s*(https?://\S+)", refs_text, re.S):
        num = m.group(1)
        url = _clean_reference_url(m.group(2))
        if url:
            refs.setdefault(num, (_derive_reference_title(num, "", url), url))

    # ── Pattern 4: plain-text title  [N] text (no URL)  ─────────────────────
    for m in re.finditer(r"\[(\d+)\]\s+([^\n\[][^\n]*)", refs_text):
        num = m.group(1)
        title = m.group(2).strip()
        if title and not title.lower().startswith("http"):
            refs.setdefault(num, (title, ""))

    return refs


def _extract_inline_citation_links(body: str) -> Dict[str, tuple[str, str]]:
    """Extract inline citation links like [[3]](url) from answer body."""
    refs: Dict[str, tuple[str, str]] = {}
    for m in re.finditer(r"\[\[(\d+)\]\]\(([^)]+)\)", body):
        n = m.group(1)
        url = _clean_reference_url(m.group(2))
        if url:
            refs[n] = (_derive_reference_title(n, "", url), url)
    return refs


def _extract_search_references_from_payload(payload: Any) -> Dict[str, tuple[str, str]]:
    """
    Best-effort parse of search references from a function_result payload.
    Handles string, dict, and list payload structures.
    """
    refs: Dict[str, tuple[str, str]] = {}
    if payload is None:
        return refs

    if isinstance(payload, str):
        return _extract_search_references(payload)

    if isinstance(payload, dict):
        for key in ("result", "content", "text", "output", "value"):
            if key in payload:
                refs.update(_extract_search_references_from_payload(payload.get(key)))
        return refs

    if isinstance(payload, list):
        for item in payload:
            refs.update(_extract_search_references_from_payload(item))
        return refs

    return refs


def _is_generic_reference_title(title: str) -> bool:
    if not title:
        return True
    normalized = title.strip()

    # Explicit placeholder titles
    if re.fullmatch(r"Reference\s+\d+", normalized, flags=re.I):
        return True

    # UUID-like filenames / ids, e.g. 2ff6f160-6a7c-45e5-a037-79c174eb4488.pdf
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\.pdf", normalized):
        return True

    # Indexed path-like placeholders, e.g. ai_search_regulation_doc/2ff6f160-...
    if re.fullmatch(r"[\w\-]+/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", normalized):
        return True

    return False


def _merge_references(
    base: Dict[str, tuple[str, str]],
    incoming: Dict[str, tuple[str, str]],
) -> Dict[str, tuple[str, str]]:
    """Merge refs while preserving non-generic titles and freshest non-empty URL."""
    for num, (new_title, new_url) in incoming.items():
        new_title = (new_title or "").strip() or f"Reference {num}"
        new_url = (new_url or "").strip()

        if num not in base:
            base[num] = (new_title, new_url)
            continue

        old_title, old_url = base[num]
        old_title = (old_title or "").strip() or f"Reference {num}"
        old_url = (old_url or "").strip()

        if _is_generic_reference_title(old_title) and not _is_generic_reference_title(new_title):
            merged_title = new_title
        else:
            merged_title = old_title

        merged_url = new_url or old_url
        base[num] = (merged_title, merged_url)

    return base


def _split_body_and_refs(text: str) -> tuple[str, str]:
    """
    Split response into (body, refs_text).
    Supports both explicit 'References:' heading and implicit trailing reference lists.
    """
    if "References:" in text:
        return text.split("References:", 1)

    implicit = re.search(r"\n\s*\[(\d+)\]\s*\[.*?\]\(https?://[^)]+\)", text, re.S)
    if implicit:
        idx = implicit.start()
        return text[:idx], text[idx:]

    # Also detect plain-URL reference lists: [1] https://...
    implicit2 = re.search(r"\n\s*\[(\d+)\]\s+https?://\S", text, re.S)
    if implicit2:
        idx = implicit2.start()
        return text[:idx], text[idx:]

    return text, ""


def _propagate_titles_by_url(
    refs_map: Dict[str, tuple[str, str]],
) -> Dict[str, tuple[str, str]]:
    """
    When several citation numbers share the same URL (or same URL with different
    #page=N anchors, i.e. different pages of the same document), make sure they all
    inherit the best (most descriptive) title instead of each keeping whatever title
    fragment happened to be parsed first.
    """

    def _doc_group_keys(url: str) -> List[str]:
        """Generate grouping keys so near-equivalent doc URLs share title context."""
        if not url:
            return []

        cleaned = _clean_reference_url(url)
        if not cleaned:
            return []

        parsed = urlsplit(cleaned)
        path = parsed.path or ""
        base_url = urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
        no_query_no_fragment = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

        basename = path.rsplit("/", 1)[-1]
        stem = basename.rsplit(".", 1)[0] if basename else ""

        keys = [base_url, no_query_no_fragment]
        if stem:
            keys.append(stem)
        return keys

    # Build key → best_title mapping
    key_best_title: Dict[str, str] = {}
    for num, (title, url) in refs_map.items():
        if not url:
            continue
        if not title or _is_generic_reference_title(title):
            continue
        for key in _doc_group_keys(url):
            existing = key_best_title.get(key, "")
            if not existing or _is_generic_reference_title(existing):
                key_best_title[key] = title

    # Fill in any still-generic or empty title from the best available
    result: Dict[str, tuple[str, str]] = {}
    for num, (title, url) in refs_map.items():
        if url and (_is_generic_reference_title(title) or not title):
            better = ""
            for key in _doc_group_keys(url):
                better = key_best_title.get(key, "")
                if better:
                    break
            if better:
                title = better
        result[num] = (title, url)
    return result


def _normalize_citations_and_references(
    body: str,
    refs_map: Dict[str, tuple[str, str]],
) -> tuple[str, List[str]]:
    """
    Ensure citation markers and references are consistent and sequential.
    Citation markers are normalized to [[1]], [[2]], ... in order of appearance.
    """
    cited_markers = re.findall(r"\[\[(\d+)\]\]", body)

    if cited_markers:
        ordered_old: List[str] = []
        seen = set()
        for n in cited_markers:
            if n not in seen:
                seen.add(n)
                ordered_old.append(n)
    else:
        # Model sometimes omits inline footnotes; synthesize from best available refs.
        if not refs_map:
            return body, []
        ordered_old = sorted(refs_map.keys(), key=lambda x: int(x))[:4]

    remap = {old: str(i + 1) for i, old in enumerate(ordered_old)}

    # Propagate best titles across citations sharing the same document URL.
    refs_map = _propagate_titles_by_url(refs_map)

    # Rewrite [[old]] to [[new]] for consistent numbering.
    if cited_markers:
        body = re.sub(r"\[\[(\d+)\]\]", lambda m: f"[[{remap.get(m.group(1), m.group(1))}]]", body)
    else:
        synthesized_markers = " ".join(f"[[{remap[old]}]]" for old in ordered_old)
        body = body.rstrip() + f"\n\n参考依据：{synthesized_markers}"

    ref_lines: List[str] = []
    for old in ordered_old:
        if old not in refs_map:
            continue
        new_num = remap[old]
        title, url = refs_map[old]
        cleaned_url = _clean_reference_url(url)
        resolved_title = _derive_reference_title(new_num, title, cleaned_url)
        if cleaned_url and re.match(r"^https?://", cleaned_url):
            ref_lines.append(f"[{new_num}] [{resolved_title}]({cleaned_url})")
        else:
            ref_lines.append(f"[{new_num}] {resolved_title}")

    return body, ref_lines


def _make_thinking_event(tool_name: str, args: dict) -> Optional[str]:
    """
    Map a tool name + args to a human-readable thinking message with [Agent] prefix.
    Returns None for tools that should be silently ignored.
    """
    if tool_name == "search_knowledge":
        # Thinking for this tool is pushed in real-time from inside search_knowledge() via the
        # combined queue, so we suppress the MAF function_call event here to avoid duplication.
        return None
    elif tool_name == "search_multiple_queries":
        n = len(args.get("queries", []))
        return f"🔍 [MasterAgent → SearchAgent] 并行检索 ({n} 个子查询)"
    elif tool_name == "decompose_query":
        return f"🧩 [MasterAgent] 分解复杂查询…"
    elif tool_name == "delegate_metadata":
        return f"🗂️ [MasterAgent → MetadataAgent] 查询数据结构: {args.get('question', '')[:80]}"
    elif tool_name == "delegate_data_insight":
        return f"📊 [MasterAgent → DataInsightAgent] 数据分析: {args.get('question', '')[:80]}"
    elif tool_name == "execute_sql":
        sql = args.get("sql", "").strip()
        return f"⚡ [DataInsightAgent] 执行 SQL:\n```sql\n{sql}\n```" if sql else "⚡ [DataInsightAgent] 执行 SQL…"
    elif tool_name == "list_tables":
        schema = args.get("schema", "") or "(所有 schema)"
        return f"📄 [MetadataAgent] 列举数据表: {schema}"
    elif tool_name == "get_table_details":
        return f"📄 [MetadataAgent] 获取表结构: {args.get('table_name', '')}"
    elif tool_name == "search_tables":
        return f"🔍 [MetadataAgent] 按关键字搜索表: {args.get('keyword', '')}"
    elif tool_name == "load_skill":
        return f"📖 [DataInsightAgent] 加载业务规则: {args.get('skill_name', '')}"
    elif tool_name == "get_relevant_tables":
        return f"🗂️ [DataInsightAgent → MetadataAgent] 查找相关数据表"
    return None  # unknown tool — skip


async def _stream_agent_response(
    message: str,
    thread,
    thread_id: str,
) -> AsyncGenerator[str, None]:
    """
    Run MasterAgent.chat_stream and convert MAF update objects to SSE events.

    Architecture — single combined asyncio.Queue (no polling):
    ─ feed_master task:  master agent updates → combined.put(("maf", upd))
    ─ insight tool thread: push() → call_soon_threadsafe → combined.put(("text"|"thinking", data))
    ─ main loop: await combined.get() — wakes instantly when any item arrives
    """
    full_response_parts: List[str] = []
    search_ref_map: Dict[str, tuple[str, str]] = {}
    _last_thinking: Optional[str] = None

    def _think(msg: str) -> Optional[str]:
        nonlocal _last_thinking
        if msg and msg != _last_thinking:
            _last_thinking = msg
            return _sse({"type": "thinking", "message": msg})
        return None

    # ── Single combined queue — avoids all polling ────────────────────────────
    combined: asyncio.Queue = asyncio.Queue()
    main_loop = asyncio.get_event_loop()

    # Expose to master_agent tools so they can push insight items directly
    state.master_agent._insight_streaming = (combined, main_loop)

    async def feed_master():
        """Push every MAF update from chat_stream into the combined queue."""
        stream = state.master_agent.chat_stream(message=message, thread=thread)
        try:
            async for upd in stream:
                await combined.put(("maf", upd))
        except Exception as exc:
            await combined.put(("error", exc))
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass
            await combined.put(("done", None))

    master_task = asyncio.create_task(feed_master())

    # ── Pending tool-call tracker (accumulates streamed args) ─────────────────
    # MAF may stream function_call arguments across multiple deltas.
    # We hold the last seen call name + accumulated args string.
    _pending_call_name: Optional[str] = None
    _pending_call_args: str = ""

    def _flush_pending_call() -> Optional[str]:
        """Flush accumulated args for the pending function call and return SSE event."""
        nonlocal _pending_call_name, _pending_call_args
        if not _pending_call_name:
            return None
        try:
            args = json.loads(_pending_call_args) if _pending_call_args else {}
        except Exception:
            args = {}
        msg = _make_thinking_event(_pending_call_name, args)
        _pending_call_name = None
        _pending_call_args = ""
        return _think(msg) if msg else None

    def _process_maf_update(update) -> List[str]:
        """Convert one MAF update object → list of SSE strings."""
        nonlocal _pending_call_name, _pending_call_args, search_ref_map
        events: List[str] = []

        # Text delta
        if hasattr(update, "text") and update.text:
            full_response_parts.append(update.text)
            events.append(_sse({"type": "text", "content": update.text}))

        if not (hasattr(update, "contents") and update.contents):
            return events

        for content in update.contents:
            ct = getattr(content, "type", None)
            if ct == "text":
                continue

            if ct == "function_call":
                tname = getattr(content, "name", "") or ""
                targs = getattr(content, "arguments", "") or ""

                if tname and tname != _pending_call_name:
                    # New tool call started — flush the previous one first
                    evt = _flush_pending_call()
                    if evt:
                        events.append(evt)
                    _pending_call_name = tname
                    _pending_call_args = targs
                else:
                    # Same tool call — accumulate argument delta
                    _pending_call_args += targs

                # If args look complete (valid JSON), flush immediately
                if _pending_call_args:
                    try:
                        parsed = json.loads(_pending_call_args)
                        evt = _flush_pending_call()
                        if evt:
                            events.append(evt)
                    except json.JSONDecodeError:
                        pass  # args still streaming, wait for more

            elif ct == "function_result":
                    result_payload = getattr(content, "result", None)
                    parsed_refs = _extract_search_references_from_payload(result_payload)
                    if parsed_refs:
                        _merge_references(search_ref_map, parsed_refs)
                    # Flush any pending call on result
                    evt = _flush_pending_call()
                    if evt:
                        events.append(evt)
                    # Note: search thinking steps are now pushed in real-time from
                    # inside search_knowledge() — no JSON parsing needed here.
        return events

    try:
        while True:
            # await — no polling; wakes instantly when any item arrives
            item_type, item_data = await combined.get()

            if item_type == "done":
                # Flush any remaining pending call
                evt = _flush_pending_call()
                if evt:
                    yield evt
                break

            elif item_type == "error":
                raise item_data

            elif item_type == "maf":
                for evt in _process_maf_update(item_data):
                    yield evt

            elif item_type == "text":
                # DataInsightAgent or MetadataAgent streaming text
                full_response_parts.append(item_data)
                yield _sse({"type": "text", "content": item_data})

            elif item_type == "refs":
                if isinstance(item_data, dict):
                    _merge_references(search_ref_map, item_data)

            elif item_type == "thinking":
                # Sub-agent thinking steps (DataInsightAgent / MetadataAgent)
                evt = _think(item_data)
                if evt:
                    yield evt

    except Exception as exc:
        logger.error(f"Streaming error: {exc}", exc_info=True)
        yield _sse({"type": "error", "message": str(exc)})
        return
    finally:
        state.master_agent._insight_streaming = None
        # Cancel (if still running) and ALWAYS await master_task so async-generator
        # finalizers are drained before the request scope exits.
        if not master_task.done():
            master_task.cancel()
        try:
            await asyncio.wait_for(master_task, timeout=3)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass

    # ── Post-process final response ───────────────────────────────────────────
    full_response = "".join(full_response_parts)

    # Normalize markdown image alt from chunked figcaption form.
    full_response = re.sub(r'!\[<figcaption>(.*?)</figcaption>\]', r'![\1]', full_response)
    full_response = re.sub(r'!\[<figcaption></figcaption>\]', r'![]', full_response)

    # Normalize references heading variants emitted by the model.
    if "References:" not in full_response and "## References" in full_response:
        full_response = full_response.replace("## References", "References:", 1)

    # Build a unified references map (model-provided refs + parsed search refs), then
    # normalize inline markers and references numbering to keep them consistent.
    body_part, refs_part = _split_body_and_refs(full_response)
    refs_from_answer = _extract_explicit_references_block(refs_part)
    refs_from_inline = _extract_inline_citation_links(body_part)

    unified_refs: Dict[str, tuple[str, str]] = {}
    _merge_references(unified_refs, refs_from_inline)
    _merge_references(unified_refs, refs_from_answer)
    _merge_references(unified_refs, search_ref_map)

    normalized_body, normalized_ref_lines = _normalize_citations_and_references(body_part, unified_refs)
    if normalized_ref_lines:
        full_response = normalized_body.rstrip() + "\n\nReferences:\n" + "\n\n".join(normalized_ref_lines)
    else:
        full_response = normalized_body.rstrip()

    # Ensure every blob URL in the final answer is signed, regardless of how the LLM formats it.
    full_response = _patch_blob_urls_with_sas(full_response)

    _append_history(thread_id, message, full_response)

    yield _sse({"type": "done", "content": full_response})



# ─── Route helpers ──────────────────────────────────────────────────────────────

def _get_or_create_thread(thread_id: Optional[str]) -> tuple[str, object]:
    """
    Return (thread_id, thread_object).
    Creates a new thread if thread_id is None or not found in the store.
    """
    if thread_id and thread_id in state.threads:
        return thread_id, state.threads[thread_id]

    # Create a new thread via MasterAgent
    thread = state.master_agent.get_new_thread()
    new_id = thread_id or str(uuid.uuid4())
    state.threads[new_id] = thread
    state.thread_history[new_id] = []
    logger.info(f"Created new thread: {new_id}")
    return new_id, thread


def _append_history(thread_id: str, user_msg: str, assistant_msg: str) -> None:
    """Store a completed turn in the thread history."""
    if thread_id not in state.thread_history:
        state.thread_history[thread_id] = []
    state.thread_history[thread_id].append(
        {
            "user": user_msg,
            "assistant": assistant_msg,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Return service health and agent status."""
    return {
        "status": "ok" if state.initialized else "degraded",
        "agent_initialized": state.initialized,
        "init_error": state.init_error,
        "active_threads": len(state.threads),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/proxy-image")
async def proxy_image(url: str = Query(..., description="Blob storage URL to proxy")):
    """
    Proxy images from Azure Blob Storage to avoid CORS restrictions in the browser.
    Only proxies URLs from known blob.core.windows.net containers.
    """
    if "blob.core.windows.net" not in url:
        raise HTTPException(status_code=403, detail="Only Azure Blob Storage URLs are supported.")

    try:
        import requests as _requests

        def _fetch():
            r = _requests.get(url, timeout=15)
            return r.status_code, r.headers.get("Content-Type", "image/jpeg"), r.content

        status_code, content_type, data = await asyncio.to_thread(_fetch)

        if status_code != 200:
            raise HTTPException(status_code=status_code, detail="Image not found in blob storage.")

        return Response(
            content=data,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Image proxy failed for {url[:80]}: {exc}")
        raise HTTPException(status_code=502, detail=f"Failed to fetch image: {exc}")


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """
    Main chat endpoint with Server-Sent Events streaming.
    The frontend calls this directly at http://localhost:8000/chat/stream.
    """
    if not state.initialized or state.master_agent is None:
        async def _error_stream():
            yield _sse(
                {
                    "type": "error",
                    "message": f"Agent not initialised. {state.init_error or ''}",
                }
            )
            yield _sse({"type": "done"})

        return StreamingResponse(_error_stream(), media_type="text/event-stream")

    thread_id, thread = _get_or_create_thread(request.thread_id)

    return StreamingResponse(
        _stream_agent_response(request.message, thread, thread_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Thread-Id": thread_id,
        },
    )


@app.post("/threads/new")
async def create_thread(body: NewThreadRequest = NewThreadRequest()):
    """Create a new conversation thread and return its ID."""
    if not state.initialized or state.master_agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialised.")

    thread = state.master_agent.get_new_thread()
    thread_id = body.thread_id or str(uuid.uuid4())
    state.threads[thread_id] = thread
    state.thread_history[thread_id] = []
    return {
        "thread_id": thread_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/threads")
async def list_threads():
    """List all active threads."""
    result = []
    for tid, history in state.thread_history.items():
        last_updated = (
            history[-1]["timestamp"] if history else datetime.now(timezone.utc).isoformat()
        )
        result.append(
            {"id": tid, "message_count": len(history), "last_updated": last_updated}
        )
    return result


@app.get("/threads/{thread_id}/history")
async def get_thread_history(thread_id: str):
    """Return message history for a specific thread."""
    if thread_id not in state.thread_history:
        raise HTTPException(status_code=404, detail="Thread not found.")
    return {"thread_id": thread_id, "messages": state.thread_history[thread_id]}


@app.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str):
    """Delete a thread and its history from memory."""
    state.threads.pop(thread_id, None)
    state.thread_history.pop(thread_id, None)
    return {"deleted": thread_id}


@app.get("/skills")
async def list_skills():
    """Return all indexed skills (name, description, tags)."""
    return [s.to_dict() for s in skill_registry.list_skills()]


# ─── Dev entry-point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent.parent / ".env")

    port = int(__import__("os").getenv("BACKEND_PORT", "8000"))
    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
