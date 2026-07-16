"""
Trinity — MCP Client Example
===============================
Connects to Trinity via MCP protocol for agent memory.
"""

# Using the MCP Server (stdio mode):
# Start: python -m trinity mcp --mode stdio
#
# In your MCP client config:
# {
#   "mcpServers": {
#     "trinity-memory": {
#       "command": "trinity-mcp",
#       "args": ["--mode", "stdio"]
#     }
#   }
# }

# Available MCP tools:
# - memory_search(query, top_k=10)
# - memory_write(content, tags=[], importance=0.5)
# - memory_update(memory_id, content=None, tags=None, importance=None)
# - memory_delete(memory_id)
# - memory_tag_search(tag)
# - memory_chronicle(persona_id=None)
# - trinity_diagnostics()
# - audit_query(query)
