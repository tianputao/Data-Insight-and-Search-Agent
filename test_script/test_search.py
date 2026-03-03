#!/usr/bin/env python3
"""
Quick test script to verify search functionality after code fix.
"""

import asyncio
import sys
from src.tools.ai_search_tool import AzureAISearchTool
from src.utils import get_logger

logger = get_logger(__name__)


async def test_search():
    """Test both agentic and standard search modes."""
    
    print("\n" + "="*80)
    print("TESTING SEARCH FUNCTIONALITY")
    print("="*80)
    
    # Test 1: Agentic Mode
    print("\n[TEST 1] Testing AGENTIC MODE (enable_agentic_retrieval=True)")
    print("-" * 80)
    tool_agentic = AzureAISearchTool(
        enable_semantic_reranker=True,  # Note: This should be overridden in agentic mode
        enable_agentic_retrieval=True,
        top_k=5
    )
    
    try:
        results_agentic = await tool_agentic.search("乘用车国家标准的主要内容")
        print(f"✓ Agentic mode returned {len(results_agentic)} results")
        if results_agentic:
            print(f"  First result: {results_agentic[0].get('title', 'N/A')[:80]}")
            print(f"  Reranker score: {results_agentic[0].get('reranker_score', 'N/A')}")
    except Exception as e:
        print(f"✗ Agentic mode failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Test 2: Standard Mode  
    print("\n[TEST 2] Testing STANDARD MODE (enable_agentic_retrieval=False)")
    print("-" * 80)
    tool_standard = AzureAISearchTool(
        enable_semantic_reranker=True,
        enable_agentic_retrieval=False,
        top_k=5
    )
    
    try:
        results_standard = await tool_standard.search("乘用车国家标准的主要内容")
        print(f"✓ Standard mode returned {len(results_standard)} results")
        if results_standard:
            print(f"  First result: {results_standard[0].get('title', 'N/A')[:80]}")
            print(f"  Reranker score: {results_standard[0].get('reranker_score', 'N/A')}")
    except Exception as e:
        print(f"✗ Standard mode failed: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "="*80)
    print("TEST COMPLETED - Check logs above for [AGENTIC MODE] or [STANDARD MODE] markers")
    print("="*80 + "\n")


if __name__ == "__main__":
    asyncio.run(test_search())
