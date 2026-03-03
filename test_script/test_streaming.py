"""
Test script to understand AgentResponseUpdate structure
"""
import asyncio
from src.agents.master_agent import MasterAgent

async def test_streaming():
    agent = MasterAgent()
    thread = agent.get_new_thread()
    
    print("=== Testing streaming response ===")
    async for update in agent.chat_stream("测试", thread=thread):
        print(f"\nUpdate type: {type(update).__name__}")
        print(f"Update class: {update.__class__}")
        print(f"Has message: {hasattr(update, 'message')}")
        print(f"Has content: {hasattr(update, 'content')}")
        print(f"Has delta: {hasattr(update, 'delta')}")
        print(f"Has text: {hasattr(update, 'text')}")
        
        # List all non-private attributes
        attrs = [attr for attr in dir(update) if not attr.startswith('_')]
        print(f"Attributes: {attrs[:20]}")  # First 20 attributes
        
        # Try to access common attributes
        for attr in ['message', 'content', 'delta', 'text', 'choices']:
            if hasattr(update, attr):
                val = getattr(update, attr)
                print(f"  {attr}: {type(val).__name__} = {str(val)[:100]}")
        
        break  # Just check the first update

if __name__ == "__main__":
    asyncio.run(test_streaming())
