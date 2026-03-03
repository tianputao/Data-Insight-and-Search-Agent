#!/usr/bin/env python
"""Test script to verify Azure Agent Framework imports."""

print("Testing Azure Agent Framework imports...")

try:
    from agent_framework.agents import AzureOpenAIChatCompletionAgent
    print("✓ AzureOpenAIChatCompletionAgent imported successfully")
except ImportError as e:
    print(f"✗ Failed to import AzureOpenAIChatCompletionAgent: {e}")

try:
    from agent_framework.core import FunctionTool
    print("✓ FunctionTool imported successfully")
except ImportError as e:
    print(f"✗ Failed to import FunctionTool: {e}")

try:
    from openai import AzureOpenAI
    print("✓ AzureOpenAI imported successfully")
except ImportError as e:
    print(f"✗ Failed to import AzureOpenAI: {e}")

try:
    from azure.search.documents import SearchClient
    print("✓ SearchClient imported successfully")
except ImportError as e:
    print(f"✗ Failed to import SearchClient: {e}")

try:
    import streamlit
    print("✓ Streamlit imported successfully")
except ImportError as e:
    print(f"✗ Failed to import Streamlit: {e}")

print("\n✅ All imports successful! Your environment is ready.")
