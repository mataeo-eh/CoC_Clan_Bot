import asyncio
from typing import Optional
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI
from dotenv import load_dotenv
import json
import os
load_dotenv()  # load environment variables from .env

# Checks for your API key to use set as an environment variable
api_key = os.getenv("OPENROUTER_API_KEY")
if not api_key:
    raise RuntimeError("Missing OPENAI_API_KEY environment variable.")
# Checks for the base URL to use for API calls from environment variables
base_url = os.getenv("OPENROUTER_BASE_URL")
if not base_url:
    raise RuntimeError("Missing API endpoint URL environment variable.")
# You may want to validate that OPENAI_MODEL is set as well
MODEL = os.getenv("OPENROUTER_MODEL")
if not MODEL:
    raise RuntimeError("Missing OPENAI_MODEL environment variable.")


# Configuration for multiple servers
SERVER_CONFIGS = {
    "python": {
        "command": "uvx",
        "args": ["mcp-run-python@latest", "stdio"],
        "env": None
    }
}

def convert_tool_format(tool):
    converted_tool = {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": tool.inputSchema["properties"],
                "required": tool.inputSchema["required"]
            }
        }
    }
    return converted_tool

class MCPClient:
    def __init__(self, model=None, temperature=0.7, streaming = False, extra_body = None):
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        self.openai = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model or MODEL

        # Stores conversation history for the current session
        self.messages = []  # Move this here from connect_to_servers

        # Store additional API parameters
        self.temperature = temperature
        self.streaming = streaming
        self.extra_body = extra_body or {}
    
    async def __aenter__(self):
        """Called when entering 'async with' block"""
        await self.connect_to_servers(SERVER_CONFIGS)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Called when exiting 'async with' block"""
        await self.cleanup()
    
    async def connect_to_servers(self, server_configs):
        """Connect to multiple MCP servers"""
        for name, config in server_configs.items():
            server_params = StdioServerParameters(**config)
            stdio_transport = await self.exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            stdio, write = stdio_transport
            session = await self.exit_stack.enter_async_context(
                ClientSession(stdio, write)
            )
            await session.initialize()
            
            self.sessions[name] = session
            
            response = await session.list_tools()
            print(f"\n{name} server tools:", [tool.name for tool in response.tools])
    
    async def query(self, prompt: str) -> str:
        """Send a query and get a response - maintains conversation history"""
        self.messages.append({"role": "user", "content": prompt})
        
        # Collect tools from ALL servers
        all_tools = []
        for session in self.sessions.values():
            response = await session.list_tools()
            all_tools.extend([convert_tool_format(tool) for tool in response.tools])
        
        # Build API call parameters
        api_params = {
            "model": self.model,
            "tools": all_tools,
            "messages": self.messages,
            "temperature": self.temperature,
        }
        
        # Add extra_body if provided
        if self.extra_body:
            api_params["extra_body"] = self.extra_body
        
        # Send to LLM with all available tools
        response = self.openai.chat.completions.create(**api_params)
        self.messages.append(response.choices[0].message.model_dump())
        
        final_text = []
        content = response.choices[0].message
        
        if content.tool_calls:
            tool_name = content.tool_calls[0].function.name
            tool_args = json.loads(content.tool_calls[0].function.arguments or "{}")
            
            # Find which server has this tool
            result = None
            for session in self.sessions.values():
                try:
                    result = await session.call_tool(tool_name, tool_args)
                    break
                except:
                    continue
            
            if result:
                # Convert MCP result content to string
                if isinstance(result.content, list):
                    content_str = "\n".join(
                        item.text if hasattr(item, 'text') else str(item) 
                        for item in result.content
                    )
                else:
                    content_str = str(result.content)
                
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": content.tool_calls[0].id,
                    "name": tool_name,
                    "content": content_str
                })
                
                # Get final response (also use same parameters)
                response = self.openai.chat.completions.create(
                    model=self.model,
                    max_tokens=1000,
                    messages=self.messages,
                    temperature=self.temperature,
                )
                final_text.append(response.choices[0].message.content)
        else:
            final_text.append(content.content)
        
        return "\n".join(final_text)
    
    async def cleanup(self):
        await self.exit_stack.aclose()

# Example for bot integration
'''
# In your Discord slash command handler:
@bot.tree.command(name="ask")
async def ask_command(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()  # Discord: thinking...
    
    # Each interaction gets its own isolated session
    async with MCPClient() as client:
        # First query
        response = await client.query(prompt)
        await interaction.followup.send(response)
        
        # Optional: Add buttons for follow-up questions
        # Each follow-up maintains conversation memory
        # When interaction ends, memory is cleared
'''
async def test():
    async with MCPClient() as client:
        print(await client.query("What is 2+2?"))
        print(await client.query("Now multiply that by 3"))  # Remembers previous answer
    # Memory cleared here when context exits

async def main():
    client = MCPClient()
    try:
        await client.connect_to_servers(SERVER_CONFIGS)  # Changed
        await client.chat_loop()
    finally:
        await client.cleanup()


if __name__ == "__main__":
    import sys
    asyncio.run(test())