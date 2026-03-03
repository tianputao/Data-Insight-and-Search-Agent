"""
Streamlit Web Application for Agentic RAG Chatbot.
Uses Microsoft Agent Framework threading for multi-turn conversations.
"""

import streamlit as st
import asyncio
import json
import re
from datetime import datetime

from src.agents import SearchAgent, MasterAgent
from src.tools import AzureAISearchTool
from src.config import AppConfig, AzureSearchConfig
from src.utils import get_logger


# Configure page
st.set_page_config(
    page_title="Enterprise Agentic RAG Chatbot with MAF",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

logger = get_logger(__name__)


# Example questions
EXAMPLE_QUESTIONS = [
    "乘用车国家标准的主要内容是什么？",
    "电动汽车续航测试标准?",
    "汽车用液化天然气加注装置的性能要求有哪些？",
    "汽车用液化天然气的加液口基本构型",
    "电动汽车用动力蓄电池安全要求",
    "电动汽车用动力蓄电池测量仪器、仪表准确度应满足什么要求",
    "What are the recall criteria for defective automotive products?",
    "什么是management body, 它在乘用车法规里做什么用的，目前发行了几个版本？"
]


def initialize_session_state():
    """Initialize Streamlit session state."""
    
    if 'messages' not in st.session_state:
        st.session_state.messages = []
    
    if 'agent' not in st.session_state:
        st.session_state.agent = None
    
    if 'thread' not in st.session_state:
        st.session_state.thread = None
    
    if 'enable_semantic_reranker' not in st.session_state:
        st.session_state.enable_semantic_reranker = AppConfig.DEFAULT_ENABLE_SEMANTIC_RERANKER
    
    if 'enable_agentic_retrieval' not in st.session_state:
        st.session_state.enable_agentic_retrieval = AppConfig.DEFAULT_ENABLE_AGENTIC_RETRIEVAL
    
    if 'pending_prompt' not in st.session_state:
        st.session_state.pending_prompt = None


def create_agent():
    """Create the master agent with current configuration."""
    
    try:
        with st.spinner("Initializing agent..."):
            # Create search tool (uses config from environment variables)
            search_tool = AzureAISearchTool(
                enable_semantic_reranker=st.session_state.enable_semantic_reranker,
                enable_agentic_retrieval=st.session_state.enable_agentic_retrieval
            )
            
            # Create search agent
            search_agent = SearchAgent(search_tool=search_tool)
            
            # Create master agent
            master_agent = MasterAgent(search_agent=search_agent)
            
            # Create a new conversation thread
            thread = master_agent.get_new_thread()
            
            st.session_state.agent = master_agent
            st.session_state.thread = thread
            
            logger.info("Agent initialized successfully")
            
    except Exception as e:
        st.error(f"❌ Error initializing agent: {str(e)}")
        logger.error(f"Agent initialization error: {e}", exc_info=True)


def display_sidebar():
    """Display sidebar with configuration options."""
    
    with st.sidebar:
        st.title("⚙️ Configuration")
        st.markdown("---")
        
        # Feature toggles
        st.subheader("Search Features")
        
        semantic_reranker = st.toggle(
            "Enable Semantic Reranker",
            value=st.session_state.enable_semantic_reranker,
            help="Use Azure AI Search semantic reranking"
        )
        
        agentic_retrieval = st.toggle(
            "Enable Agentic Retrieval",
            value=st.session_state.enable_agentic_retrieval,
            help="Use agentic retrieval for query decomposition"
        )
        
        # Update configuration if changed
        if (semantic_reranker != st.session_state.enable_semantic_reranker or 
            agentic_retrieval != st.session_state.enable_agentic_retrieval):
            
            st.session_state.enable_semantic_reranker = semantic_reranker
            st.session_state.enable_agentic_retrieval = agentic_retrieval
            
            # Update agent configuration
            if st.session_state.agent:
                st.session_state.agent.update_config(
                    enable_semantic_reranker=semantic_reranker,
                    enable_agentic_retrieval=agentic_retrieval
                )
                st.success("✓ Configuration updated")
        
        st.markdown("---")
        
        # Example questions
        st.subheader("💡 Example Questions")
        for i, question in enumerate(EXAMPLE_QUESTIONS):
            if st.button(question, key=f"example_{i}", use_container_width=True):
                # Set pending prompt to be processed in main chat area
                st.session_state.pending_prompt = question
        
        st.markdown("---")
        
        # Conversation controls
        st.subheader("🔄 Conversation")
        if st.button("Clear Conversation"):
            st.session_state.messages = []
            # Create new thread
            if st.session_state.agent:
                st.session_state.thread = st.session_state.agent.get_new_thread()
            st.rerun()


async def process_message(user_message: str):
    """Process user message and get response."""
    
    if not st.session_state.agent:
        st.error("❌ Agent not initialized. Please check configuration.")
        return
    
    # Add user message to chat
    st.session_state.messages.append({
        "role": "user",
        "content": user_message,
        "timestamp": datetime.now()
    })
    
    # Display user message
    with st.chat_message("user"):
        st.write(user_message)
    
    # Get agent response with streaming
    with st.chat_message("assistant"):
        message_placeholder = st.empty()
        full_response = ""
        
        # Status container for agent thought process
        status_container = st.status("Thinking...", expanded=True)
        
        try:
            # Stream response
            async for update in st.session_state.agent.chat_stream(
                message=user_message,
                thread=st.session_state.thread
            ):
                # Debug logging
                logger.info(f"=== UPDATE: {type(update).__name__} ===")
                logger.info(f"Attributes: {[a for a in dir(update) if not a.startswith('_')][:20]}")
                
                # Log key attributes
                if hasattr(update, 'text'):
                    logger.info(f"  .text = '{update.text}'")
                if hasattr(update, 'contents'):
                    logger.info(f"  .contents = {len(update.contents) if update.contents else 0} items")
                    if update.contents:
                        for idx, content in enumerate(update.contents):
                            logger.info(f"    Content[{idx}].type = {getattr(content, 'type', 'N/A')}")
                
                # CRITICAL FIX: Handle .text attribute FIRST (for AgentResponseUpdate)
                # This must be checked independently, not in elif chain
                if hasattr(update, 'text') and update.text:
                    logger.info(f"  >>> Adding text to response: '{update.text}'")
                    status_container.update(label="✍️ Generating Answer...", state="running", expanded=False)
                    full_response += update.text
                    message_placeholder.markdown(full_response + "▌")
                elif hasattr(update, 'text'):
                    logger.warning(f"  >>> Has .text but it's empty/None")
                
                # Handle contents for function calls and results (skip "text" type since already handled above)
                if hasattr(update, "contents") and update.contents:
                    for content in update.contents:
                        content_type = getattr(content, "type", None)
                        logger.debug(f"Content type: {content_type}")

                        # Skip "text" type as it's already handled via update.text above
                        if content_type == "text":
                            continue

                        if content_type == "function_call":
                            status_container.update(label="🔍 Searching Knowledge Base...", state="running", expanded=True)
                            tool_name = getattr(content, "name", None)
                            tool_args = getattr(content, "arguments", None)
                            if tool_name:
                                status_container.write(f"🛠️ **Tool:** `{tool_name}`")
                            if tool_args:
                                try:
                                    args_dict = json.loads(tool_args) if isinstance(tool_args, str) else tool_args
                                    query = args_dict.get('query', 'N/A')
                                    status_container.write(f"📝 **Query:** {query}")
                                except:
                                    pass
                        
                        elif content_type == "function_result":
                            status_container.update(label="📊 Processing Results...", state="running", expanded=True)
                            result_data = getattr(content, "result", None)
                            logger.info(f"  function_result.result type: {type(result_data)}")
                            logger.info(f"  function_result.result preview: {str(result_data)[:300]}")
                            
                            if result_data:
                                # Try to parse metadata JSON from the beginning of result_data
                                data_dict = None
                                if isinstance(result_data, str):
                                    # Check if result starts with JSON metadata
                                    if result_data.startswith("{"):
                                        try:
                                            import json
                                            # Try to extract JSON from first line
                                            lines = result_data.split("\n\n", 1)
                                            if len(lines) >= 1:
                                                parsed = json.loads(lines[0])
                                                if isinstance(parsed, dict):
                                                    data_dict = parsed
                                        except:
                                            pass
                                elif isinstance(result_data, dict):
                                    data_dict = result_data

                                # Display thinking process if metadata was parsed
                                if data_dict:
                                    # Display thinking process if available
                                    if "thinking_log" in data_dict:
                                        for step in data_dict["thinking_log"]:
                                            status_container.write(f"🧠 {step}")
                                    
                                    # Display result count
                                    if "result_count" in data_dict:
                                        status_container.write(f"✅ Selected **{data_dict['result_count']}** top documents")
                                    
                                    # Determine if results found
                                    if data_dict.get("result_count", 0) > 0:
                                        pass # Already noted above
                                    else:
                                        status_container.write(f"⚠️ No relevant documents found")

                                # Legacy String Handling (Fallthrough)
                                elif isinstance(result_data, str):
                                    if "Found" in result_data and "documents" in result_data:
                                        # Extract number from "Found X relevant documents:"
                                        match = re.search(r'Found (\d+) relevant documents', result_data)
                                        if match:
                                            count = int(match.group(1))
                                            status_container.write(f"✅ Found **{count}** relevant documents")
                                        else:
                                            status_container.write(f"✅ Search results received")
                                    elif "No relevant documents" in result_data:
                                        status_container.write(f"⚠️ No results found")
                                    else:
                                        status_container.write(f"📦 Results received")
                                else:
                                    status_container.write(f"📦 Results received")

                        elif content_type == "error":
                            status_container.update(label="Error", state="error", expanded=True)
                            status_container.write(str(content))

                # Check for choice-based delta (for other OpenAI client types)
                if hasattr(update, 'choices') and update.choices:
                    delta = update.choices[0].delta
                    if hasattr(delta, 'tool_calls') and delta.tool_calls:
                        status_container.update(label="🔍 Searching...", state="running", expanded=True)
                        for tool_call in delta.tool_calls:
                            if hasattr(tool_call, 'function'):
                                if tool_call.function.name:
                                    status_container.write(f"🛠️ **Tool:** `{tool_call.function.name}`")
                    
                    if hasattr(delta, 'content') and delta.content:
                        status_container.update(label="✍️ Generating Answer...", state="running", expanded=False)
                        full_response += delta.content
                        message_placeholder.markdown(full_response + "▌")
                
            status_container.update(label="Completed", state="complete", expanded=False)
            
            # Clean up markdown image syntax - remove HTML tags from alt text
            # Pattern: ![<figcaption>text</figcaption>](url) -> ![text](url)
            import re
            full_response = re.sub(
                r'!\[<figcaption>(.*?)</figcaption>\]',
                r'![\1]',
                full_response
            )
            # Also handle empty figcaption: ![<figcaption></figcaption>](url) -> ![](url)
            full_response = re.sub(
                r'!\[<figcaption></figcaption>\]',
                r'![]',
                full_response
            )
            
            # Filter references to only those actually cited in the body
            if "References:" in full_response:
                body, refs = full_response.split("References:", 1)
                cited_numbers = set(re.findall(r"\[(\d+)\]", body))
                logger.info(f"[CITATION FILTER] Citations found in body: {sorted(cited_numbers)}")
                logger.info(f"[CITATION FILTER] References section (first 500 chars):\n{refs[:500]}")
                
                if cited_numbers:
                    kept_lines = []
                    all_ref_lines = [line for line in refs.splitlines() if line.strip()]
                    logger.info(f"[CITATION FILTER] Total reference lines: {len(all_ref_lines)}")
                    
                    # Import config for SAS token
                    from src.config.settings import AzureSearchConfig
                    
                    for line in all_ref_lines:
                        match = re.search(r"\[(\d+)\]", line)
                        if match:
                            ref_num = match.group(1)
                            logger.info(f"[CITATION FILTER] Found reference [{ref_num}] in line")
                            if ref_num in cited_numbers:
                                # Fix URL if it's missing SAS token
                                # Pattern: [1]: https://...blob.core.windows.net/aisearchdoc/...pdf (without ?)
                                if 'aisearchdoc' in line and AzureSearchConfig.SAS_TOKEN:
                                    # Check if URL doesn't have query parameters
                                    url_match = re.search(r'(https://[^\s]+\.pdf)(?!\?)', line)
                                    if url_match:
                                        original_url = url_match.group(1)
                                        fixed_url = f"{original_url}?{AzureSearchConfig.SAS_TOKEN}"
                                        line = line.replace(original_url, fixed_url)
                                        logger.info(f"[CITATION FILTER] Fixed URL for [{ref_num}]: added SAS token")
                                
                                kept_lines.append(line)
                                logger.info(f"[CITATION FILTER] Kept reference [{ref_num}]")
                            else:
                                logger.info(f"[CITATION FILTER] Filtered out reference [{ref_num}] (not cited in body)")
                    
                    logger.info(f"[CITATION FILTER] Kept {len(kept_lines)} of {len(all_ref_lines)} references")
                    if kept_lines:
                        full_response = body.rstrip() + "\n\nReferences:\n" + "\n".join(kept_lines)
                    else:
                        full_response = body.rstrip()

            message_placeholder.markdown(full_response)
            
            logger.info(f"=== STREAM COMPLETED ===")
            logger.info(f"Total response length: {len(full_response)} chars")
            logger.info(f"Response preview: {full_response[:200]}")
            
            # If no response was generated, show a warning
            if not full_response or full_response.strip() == "":
                logger.error("No response text was generated despite tool calls")
                message_placeholder.warning("⚠️ 搜索完成但未生成回答。请检查日志或重新提问。")
            else:
                logger.info(f"Successfully generated response with {len(full_response)} characters")
            
            # Add assistant response to messages
            st.session_state.messages.append({
                "role": "assistant",
                "content": full_response,
                "timestamp": datetime.now()
            })
            
        except Exception as e:
            # Check if this is a content filter error
            error_str = str(e).lower()
            if "content" in error_str and ("filter" in error_str or "filtered" in error_str):
                error_msg = "⚠️ 您的问题因安全策略被拦截。请尝试:\n- 使用不同的表达方式\n- 更具体地描述您的问题\n- 避免可能触发安全过滤器的词汇"
                message_placeholder.warning(error_msg)
                logger.warning(f"Content filter triggered: {e}")
            else:
                error_msg = f"❌ Error: {str(e)}"
                message_placeholder.error(error_msg)
                logger.error(f"Chat error: {e}", exc_info=True)


def display_chat():
    """Display chat interface."""
    
    st.title("🤖 Enterprise RAG Chatbot")
    st.caption("Powered by Azure OpenAI GPT-5.1 & Azure AI Search")
    
    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.write(message["content"])
    
    # Chat input
    prompt = st.chat_input("Ask a question...")
    
    # Handle pending prompt from example questions
    if st.session_state.pending_prompt:
        prompt = st.session_state.pending_prompt
        st.session_state.pending_prompt = None
    
    if prompt:
        asyncio.run(process_message(prompt))
        st.rerun()


def main():
    """Main application entry point."""
    
    # Initialize session state
    initialize_session_state()
    
    # Display sidebar
    display_sidebar()
    
    # Initialize agent if not already done
    if not st.session_state.agent:
        create_agent()
    
    # Display chat interface
    display_chat()


if __name__ == "__main__":
    main()
