"""
Trinity × LangChain — create a LangChain agent with Trinity memory.

Prerequisites:
    pip install trinity-memory[langchain]

Usage:
    export OPENAI_API_KEY=sk-...
    python examples/langchain_agent.py
"""

import os
import sys
sys.path.insert(0, "..")

# Check for API key
if not os.environ.get("OPENAI_API_KEY"):
    print("Please set OPENAI_API_KEY environment variable.")
    print("    export OPENAI_API_KEY=sk-...  (Linux/Mac)")
    print("    $env:OPENAI_API_KEY='sk-...' (PowerShell)")
    sys.exit(1)

from trinity.mcp.langchain_adapter import TrinityMemoryToolkit, create_trinity_agent
from langchain_openai import ChatOpenAI


def main():
    # 1. Initialize LLM
    llm = ChatOpenAI(model="gpt-4o", temperature=0)
    print("[1/3] ✅ LLM initialized")

    # 2. Create Trinity agent
    toolkit = TrinityMemoryToolkit(mode="local")
    agent = create_trinity_agent(llm, toolkit, verbose=True)
    print("[2/3] ✅ Trinity agent created with 5 memory tools")

    # 3. Chat
    print("[3/3] 🎯 Agent ready! Type your query below.\n")
    print("=" * 60)

    while True:
        user_input = input("\nYou: ").strip()
        if user_input.lower() in ("exit", "quit", "q"):
            print("Bye!")
            break

        try:
            result = agent.invoke({"input": user_input})
            print(f"\nAgent: {result['output']}")
        except Exception as e:
            print(f"\n[Error] {e}")


if __name__ == "__main__":
    main()
