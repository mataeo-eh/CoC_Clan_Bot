import json
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

def convert_mcp_tool_to_openai(mcp_tool) -> dict:
    """
    Robustly converts an MCP tool to an OpenAI function tool format.
    Handles missing properties and required fields in the schema safely.
    
    Args:
        mcp_tool: An MCP tool object (has .name, .description, .inputSchema)
        
    Returns:
        dict: A dictionary formatted as an OpenAI tool.
    """
    schema = getattr(mcp_tool, "inputSchema", {})
    
    # Provide safe defaults if schema keys are missing
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    
    return {
        "type": "function",
        "function": {
            "name": mcp_tool.name,
            "description": getattr(mcp_tool, "description", ""),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    }

class MCPToolManager:
    """
    Manages MCP sessions and provides a pipeline for OpenAI tool calling.
    It caches the mapping of tools to their respective servers to avoid
    broadcasting tool execution to all servers.
    """
    def __init__(self, sessions: Optional[Dict[str, Any]] = None):
        """
        Args:
            sessions: A dictionary mapping server names to MCP ClientSession objects.
        """
        self.sessions = sessions or {}
        # Caches tool name -> server name mapping
        self._tool_registry: Dict[str, str] = {}

    def add_session(self, name: str, session: Any):
        """
        Registers an MCP ClientSession.
        """
        self.sessions[name] = session

    async def get_all_openai_tools(self) -> List[dict]:
        """
        Retrieves all tools from all registered MCP sessions and returns them
        in OpenAI compatible format.
        
        Returns:
            List[dict]: A list of OpenAI formatted tool definitions.
        """
        all_tools = []
        for server_name, session in self.sessions.items():
            try:
                response = await session.list_tools()
                for tool in response.tools:
                    # Register which server provides this tool
                    self._tool_registry[tool.name] = server_name
                    # Convert to OpenAI format
                    all_tools.append(convert_mcp_tool_to_openai(tool))
            except Exception as e:
                logger.error(f"Error fetching tools from server '{server_name}': {e}")
                
        return all_tools

    async def execute_tool_call(self, tool_name: str, tool_args: dict) -> str:
        """
        Finds the server that owns the tool and executes it.
        
        Args:
            tool_name (str): The name of the tool to execute.
            tool_args (dict): The arguments to pass to the tool.
            
        Returns:
            str: The robustly formatted string result from the tool execution.
            
        Raises:
            ValueError: If the tool could not be executed on any registered server.
        """
        # Fast path: Use cached mapping
        server_name = self._tool_registry.get(tool_name)
        
        if server_name and server_name in self.sessions:
            session = self.sessions[server_name]
            try:
                result = await session.call_tool(tool_name, tool_args)
                return self._format_result(result)
            except Exception as e:
                logger.warning(
                    f"Error executing '{tool_name}' on cached server '{server_name}': {e}. "
                    "Falling back to broadcast execution."
                )
                
        # Fallback: Try all servers if cache miss or cached server failed
        return await self._execute_fallback(tool_name, tool_args)

    async def _execute_fallback(self, tool_name: str, tool_args: dict) -> str:
        """
        Attempts to execute the tool on all registered servers until one succeeds.
        """
        for server_name, session in self.sessions.items():
            try:
                result = await session.call_tool(tool_name, tool_args)
                # Cache success for future calls
                self._tool_registry[tool_name] = server_name 
                return self._format_result(result)
            except Exception:
                # Tool doesn't exist on this server, or execution failed.
                continue
                
        raise ValueError(f"Tool '{tool_name}' could not be executed on any registered server.")

    def _format_result(self, result: Any) -> str:
        """
        Robustly formats the MCP tool execution result into a string.
        """
        content = getattr(result, 'content', result)
        
        # If content is a list of blocks, concatenate their text.
        if isinstance(content, list):
            return "\n".join(
                item.text if hasattr(item, 'text') else str(item) 
                for item in content
            )
            
        return str(content)
