"""
This file is mainly intended as a reference for how to 
use the MCPClient class in an application like a Discord bot.
It includes an example of how to integrate the MCPClient into a Discord slash command, 
as well as a test function that demonstrates both non-streaming and 
streaming queries to the LLM with tool usage.
"""
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

cwd = os.getcwd()

# Configuration for multiple servers
SERVER_CONFIGS = {
    "python": {
        "command": "uvx",
        "args": ["mcp-run-python@latest", "stdio"],
        "env": None
    },
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", cwd],
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
    def __init__(
            self, model=None, temperature=1.0, streaming=False, 
            extra_body=None, system_prompt=None
        ):
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        self.openai = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model or MODEL
        self.messages = []
        
        # Store API parameters
        self.temperature = temperature
        self.streaming = streaming
        self.extra_body = extra_body or {}
        
        # Store system prompt
        self.system_prompt = system_prompt
        
        # Default system prompt for tool usage guidance
        self.default_tool_guidance = (
            "You have access to a Python code execution tool. Only use it when the calculation "
            "is complex or when you're uncertain. For simple arithmetic (like factorial, basic "
            "multiplication, etc.), calculate directly without using tools."
        )
    
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
    
    def _ensure_system_prompt(self):
        """Add system prompt if this is the first message and system prompt is set"""
        if len(self.messages) == 0:
            # Combine custom system prompt with default tool guidance
            if self.system_prompt:
                # User provided a custom system prompt
                full_prompt = f"{self.system_prompt}\n\n{self.default_tool_guidance}"
            else:
                # Just use tool guidance
                full_prompt = self.default_tool_guidance
            
            self.messages.append({
                "role": "system",
                "content": full_prompt
            })
    
    async def query(self, prompt: str) -> str:
        """Send a query and get a response - maintains conversation history"""
        # Add system prompt on first query
        self._ensure_system_prompt()
        
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
                
                # Get final response
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
    
    async def query_stream(self, prompt: str):
        """
        Send a query and stream the response - maintains conversation history
        Yields response chunks as they arrive
        """
        # Add system prompt on first query
        self._ensure_system_prompt()
        
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
            "stream": True,
        }
        
        # Add extra_body if provided
        if self.extra_body:
            api_params["extra_body"] = self.extra_body
        
        # Send to LLM with streaming enabled
        stream = self.openai.chat.completions.create(**api_params)
        
        # Collect the streamed response
        full_content = ""
        tool_calls = []
        finish_reason = None
        
        for chunk in stream:
            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason
            
            # Handle text content
            if delta.content:
                full_content += delta.content
                yield delta.content
            
            # Handle tool calls (streamed in parts)
            if delta.tool_calls:
                for tool_call_delta in delta.tool_calls:
                    # Extend or append tool call
                    if tool_call_delta.index >= len(tool_calls):
                        tool_calls.append({
                            "id": tool_call_delta.id,
                            "type": "function",
                            "function": {
                                "name": tool_call_delta.function.name or "",
                                "arguments": tool_call_delta.function.arguments or ""
                            }
                        })
                    else:
                        # Append to existing tool call arguments
                        if tool_call_delta.function.arguments:
                            tool_calls[tool_call_delta.index]["function"]["arguments"] += \
                                tool_call_delta.function.arguments
        
        # Save the complete message to history
        assistant_message = {
            "role": "assistant",
            "content": full_content if full_content else None
        }
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        self.messages.append(assistant_message)
        
        # If LLM wants to use a tool, execute it and stream the final response
        if tool_calls:
            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                tool_args = json.loads(tool_call["function"]["arguments"] or "{}")
                
                # Find which server has this tool and execute
                result = None
                for session_name, session in self.sessions.items():
                    try:
                        result = await session.call_tool(tool_name, tool_args)
                        yield f"\n[Executed {tool_name}]\n"
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
                    
                    # Add tool result to conversation
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": tool_name,
                        "content": content_str
                    })
            
            # Get final response after tool execution, also streamed
            final_stream = self.openai.chat.completions.create(
                model=self.model,
                max_tokens=1000,
                messages=self.messages,
                temperature=self.temperature,
                stream=True,
            )
            
            final_content = ""
            for chunk in final_stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    final_content += delta.content
                    yield delta.content
            
            # Save final response to history
            self.messages.append({
                "role": "assistant",
                "content": final_content
            })
    
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


'''
# Example usage of streaming query in Discord bot:
async def example_discord_usage(interaction, prompt):
    """Example of how to use streaming in Discord"""
    await interaction.response.defer()  # "Bot is thinking..."
    
    async with MCPClient() as client:
        accumulated_response = ""
        message = None
        
        async for chunk in client.query_stream(prompt):
            accumulated_response += chunk
            
            # Update Discord message every N characters or on tool execution
            if len(accumulated_response) % 100 == 0 or "[Executed" in chunk:
                if message is None:
                    message = await interaction.followup.send(accumulated_response)
                else:
                    await message.edit(content=accumulated_response)
        
        # Final update
        if message:
            await message.edit(content=accumulated_response)
        else:
            await interaction.followup.send(accumulated_response)
'''


# Test both query methods:
async def test():
    EXTRA_BODY = {
            "reasoning": {
                "effort": "medium",
                "exclude": True
            }
        }
    prompt = f"""
        You are a helpdesk bot. You purpose is to assist users with their questions.
        You have access to some helpful tools to call as needed to best assist them.
        You are called when the user uses the discord command /help_from_ai and the
        user's question is passed as the prompt. Always try to answer the user's 
        question as best as you can to help guide them through using the bot.
    """
    async with MCPClient(temperature=1, extra_body=EXTRA_BODY, system_prompt=prompt) as client:
        # Non-streaming
        print("=== Non-streaming ===")
        response = await client.query("What is 5 factorial?")
        print(response)
        # Streaming 
        print("\n=== Streaming ===")
        print("Response: ", end="", flush=True)
        async for chunk in client.query_stream("Now multiply that by 3 and explain the steps."):
            print(chunk, end="", flush=True)
        print()


async def main():
    client = MCPClient()
    try:
        await client.connect_to_servers(SERVER_CONFIGS)  # Changed
        await client.chat_loop()
    finally:
        await client.cleanup()


if __name__ == "__main__":
    asyncio.run(test())
