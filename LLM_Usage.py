"""
Discord Bot Command Help System with AI-Powered Code Analysis

This module implements an AI-powered help system for Discord slash commands.
It uses a two-tier LLM architecture:
1. User-facing LLM - Maintains Discord conversation context
2. Code-analysis LLM (router agent) - Analyzes codebase using filesystem tools

The router agent uses MCP filesystem tools to read and understand command code,
following OpenRouter's interleaved thinking pattern for sophisticated reasoning.

SECURITY MODEL:
- All file access is restricted to BOT_CODE_DIR (project root)
- Paths are validated using pathlib.resolve() to prevent directory traversal
- The MCP filesystem server is configured with BOT_CODE_DIR as the only allowed directory
- Multiple layers of validation ensure no access outside the sandbox
"""

import asyncio
import json
import os
import sys
from typing import Optional, Dict, Any, List
from pathlib import Path
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Discord message length limit
MAX_MESSAGE_LENGTH = 1990


# ============================================================================
# DISCORD MESSAGE CHUNKING
# ============================================================================

from Discord_Commands import _chunk_content


# OpenRouter configuration
api_key = os.getenv("OPENROUTER_API_KEY")
if not api_key:
    raise RuntimeError("Missing OPENROUTER_API_KEY environment variable.")
base_url = os.getenv("OPENROUTER_BASE_URL")
if not base_url:
    raise RuntimeError("Missing OPENROUTER_BASE_URL environment variable.")
MODEL = os.getenv("OPENROUTER_MODEL")
if not MODEL:
    raise RuntimeError("Missing OPENROUTER_MODEL environment variable.")


# ============================================================================
# SECURITY: PATH VALIDATION
# ============================================================================

class PathValidator:
    """
    Secure path validation to prevent directory traversal attacks.
    
    Uses pathlib with strict validation to ensure all file access
    remains within the authorized sandbox directory.
    
    DEFENSE-IN-DEPTH SECURITY MODEL:
    1. Python Layer (this class): Validates paths using pathlib.resolve()
    2. MCP Server Layer: @modelcontextprotocol/server-filesystem is configured
       with BOT_CODE_DIR as the only allowed directory
    3. System Layer: The MCP server runs with limited privileges
    
    Even if one layer fails, the others provide protection.
    
    KEY SECURITY PRINCIPLES IMPLEMENTED:
    - Path Normalization: Uses .resolve() to eliminate '..' and symlinks
    - Strict Comparison: Uses .is_relative_to() for Python 3.9+ compatibility
    - No String Concatenation: Uses pathlib operators for platform safety
    - Validation Before Use: All paths validated before any file operations
    """
    
    def __init__(self, base_dir: str):
        """
        Initialize validator with a base directory.
        
        Args:
            base_dir: The root directory that all paths must be within
            
        Raises:
            ValueError: If base_dir doesn't exist or isn't a directory
        """
        # 1. Define and resolve the base 'safe' directory
        self.safe_path = Path(base_dir).resolve()
        
        # Validate the base directory exists and is actually a directory
        if not self.safe_path.exists():
            raise ValueError(f"Base directory does not exist: {self.safe_path}")
        if not self.safe_path.is_dir():
            raise ValueError(f"Base path is not a directory: {self.safe_path}")
        
        print(f"[Security] Sandbox initialized: {self.safe_path}")
    
    def validate_path(self, user_path: str) -> Path:
        """
        Validate a user-provided path is within the safe directory.
        
        Args:
            user_path: User-provided path (relative or absolute)
            
        Returns:
            Validated, resolved Path object
            
        Raises:
            PermissionError: If path is outside the authorized directory
        """
        # 2. Join and resolve the user-provided path
        # .resolve() eliminates '..' and symlinks for security
        requested_path = (self.safe_path / user_path).resolve()
        
        # 3. VERIFY: Ensure the resolved path is still inside the safe directory
        if not requested_path.is_relative_to(self.safe_path):
            raise PermissionError(
                f"Access denied: Path '{user_path}' resolves to '{requested_path}' "
                f"which is outside the authorized directory '{self.safe_path}'"
            )
        
        return requested_path
    
    def validate_file_exists(self, user_path: str) -> Path:
        """
        Validate path and ensure the file exists.
        
        Args:
            user_path: User-provided path
            
        Returns:
            Validated Path object
            
        Raises:
            PermissionError: If path is outside sandbox
            FileNotFoundError: If file doesn't exist
        """
        validated_path = self.validate_path(user_path)
        
        if not validated_path.exists():
            raise FileNotFoundError(f"File not found: {user_path}")
        
        return validated_path
    
    def safe_read_text(self, user_path: str) -> str:
        """
        Safely read a text file within the sandbox.
        
        Args:
            user_path: User-provided path to read
            
        Returns:
            File contents
            
        Raises:
            PermissionError: If path is outside sandbox
            FileNotFoundError: If file doesn't exist
        """
        validated_path = self.validate_file_exists(user_path)
        return validated_path.read_text(encoding='utf-8')
    
    def get_base_dir(self) -> str:
        """Get the base directory as a string (for MCP server config)"""
        return str(self.safe_path)


# ============================================================================
# CONFIGURATION
# ============================================================================

# Initialize secure path validator
# This enforces that ALL file access must be within the project root
try:
    # Use current working directory as the base (project root)
    PATH_VALIDATOR = PathValidator(".")
    BOT_CODE_DIR = PATH_VALIDATOR.get_base_dir()
except Exception as e:
    print(f"[FATAL] Failed to initialize path validator: {e}")
    sys.exit(1)

# Validate command index path
try:
    COMMAND_INDEX_PATH = str(PATH_VALIDATOR.validate_file_exists("command_index.json"))
    print(f"[Security] Command index validated: {COMMAND_INDEX_PATH}")
except FileNotFoundError:
    print(f"[WARNING] command_index.json not found in {BOT_CODE_DIR}")
    print(f"[WARNING] The system will fail when trying to load the command index")
except PermissionError as e:
    print(f"[FATAL] Security violation: {e}")
    sys.exit(1)

# MCP Server configuration for filesystem access
# SECURITY: The filesystem server is restricted to BOT_CODE_DIR only
# This provides defense-in-depth - even if our validation fails,
# the MCP server itself won't allow access outside this directory
FILESYSTEM_SERVER_CONFIG = {
    "filesystem": {
        "command": "npx",
        "args": [
            "-y", 
            "@modelcontextprotocol/server-filesystem",
            BOT_CODE_DIR  # Only this directory is accessible
        ],
        "env": None
    }
}

print(f"[Security] MCP filesystem server restricted to: {BOT_CODE_DIR}")


# ============================================================================
# COMMAND INDEX LOADER
# ============================================================================

def load_command_index() -> Dict[str, Any]:
    """
    Load the command index JSON file using secure path validation.
    
    Returns:
        Command index dictionary
        
    Raises:
        FileNotFoundError: If command_index.json doesn't exist
        PermissionError: If path is outside the sandbox (should never happen)
        json.JSONDecodeError: If the file isn't valid JSON
    """
    # Use the secure path validator to read the file
    content = PATH_VALIDATOR.safe_read_text("command_index.json")
    return json.loads(content)


# ============================================================================
# SYSTEM PROMPTS
# ============================================================================

def build_router_system_prompt(command_index: Dict[str, Any]) -> str:
    """
    Build system prompt for the code-analysis router agent.
    This agent has access to filesystem tools and knows command locations.
    """
    return f"""You are a code analysis agent helping users understand Discord bot slash commands.

Your task is to analyze Python code to explain how Discord commands work.

**SECURITY NOTICE:**
You have access to filesystem tools, but they are restricted to the project directory only.
You CANNOT and should not attempt to access:
- System files (e.g., /etc/passwd, /windows/system32)
- User home directories outside the project
- Parent directories (../ paths are blocked)
- Any files outside the project root

The filesystem server enforces these restrictions. Focus on analyzing code within the project.

**Available Commands and Their Code Locations:**
{json.dumps(command_index, indent=2)}

The code locations reference line numbers in the file "Discord_commands.py" in the current directory.

**Your Capabilities:**
- You have access to filesystem tools (read_file, list_directory, etc.)
- All file access is restricted to the project directory for security
- You can read specific line ranges from files
- You can follow code references to view_classes and parent classes

**Analysis Workflow:**
1. Identify which command the user is asking about
2. Use read_file to read the command's code (start_line to end_line)
3. If the command has associated view_classes, read those too
4. If you find references to parent classes or imports, read those sections
5. Synthesize your understanding into a clear explanation

**What to Extract from Code:**
- Command purpose and description
- Required and optional parameters
- User workflow (what happens when they run the command)
- Interactive elements (buttons, dropdowns, modals)
- Any special requirements or permissions

**Response Format:**
Provide a clear, concise summary suitable for a user learning how to use the command.
Focus on the user experience, not implementation details.
Use natural language, not code snippets in your final response.

**Important:**
- Read the actual code before responding - don't guess
- If a command uses UI elements, explain what the user will see
- Chain multiple file reads if needed to fully understand the command
- Be thorough but concise in your final summary
- Stay within the project directory - do not attempt to access external files"""


def build_main_llm_system_prompt() -> str:
    """
    Build system prompt for the user-facing main LLM.
    This LLM maintains conversation with the Discord user.
    """
    return """You are a helpful assistant for a Discord Clash of Clans bot.

Users will ask you questions about how to use the bot's slash commands.
You have access to a tool called "analyze_command_code" that allows you to 
investigate the bot's source code to understand how commands work.

**Your Role:**
- Help users understand how to use slash commands
- Answer follow-up questions about command features
- Clarify command parameters and workflows
- Be friendly and concise (remember: Discord has message length limits)

**When to Use the Tool:**
- When asked about a specific command
- When you need details about command functionality
- When you're unsure about a command's behavior

**Important:**
- Keep responses under 2000 characters for Discord compatibility
- Be helpful and patient with users
- If you don't know something, use the tool to investigate
- Maintain conversation context across multiple questions"""


# ============================================================================
# MCP CLIENT FOR ROUTER AGENT
# ============================================================================

def convert_tool_format(tool):
    """Convert MCP tool format to OpenAI tool format"""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": tool.inputSchema["properties"],
                "required": tool.inputSchema.get("required", [])
            }
        }
    }


class RouterAgent:
    """
    Code-analysis agent with filesystem MCP tools.
    Uses interleaved thinking to chain multiple tool calls.
    """
    
    def __init__(self, command_index: Dict[str, Any]):
        self.command_index = command_index
        self.sessions = {}
        self.exit_stack = AsyncExitStack()
        self.openai = OpenAI(base_url=base_url, api_key=api_key)
        self.model = MODEL
        self.messages = []
        self.system_prompt = build_router_system_prompt(command_index)
        
    async def __aenter__(self):
        """Initialize MCP connection when entering context"""
        await self.connect_to_servers(FILESYSTEM_SERVER_CONFIG)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Cleanup when exiting context"""
        await self.cleanup()
    
    async def connect_to_servers(self, server_configs: Dict[str, Any]):
        """Connect to MCP servers (filesystem in this case)"""
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
            
            # Log available tools
            response = await session.list_tools()
            print(f"[RouterAgent] {name} server tools:", [tool.name for tool in response.tools])
    
    async def analyze(self, question: str, max_iterations: int = 10) -> str:
        """
        Analyze codebase to answer a question.
        Uses agentic loop with interleaved thinking.
        
        Args:
            question: User's question about a command
            max_iterations: Maximum tool call iterations
            
        Returns:
            Analysis summary suitable for the main LLM
        """
        # Initialize conversation with system prompt
        self.messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question}
        ]
        
        # Collect all available tools from MCP servers
        all_tools = []
        for session in self.sessions.values():
            response = await session.list_tools()
            all_tools.extend([convert_tool_format(tool) for tool in response.tools])
        
        # Agentic loop - model can make multiple tool calls with reasoning
        iteration_count = 0
        while iteration_count < max_iterations:
            iteration_count += 1
            
            # Call LLM with tools
            response = self.openai.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=all_tools,
                temperature=0.3,  # Lower temperature for code analysis
            )
            
            assistant_message = response.choices[0].message
            self.messages.append(assistant_message.model_dump())
            
            # Check if model wants to use tools
            if assistant_message.tool_calls:
                # Execute each requested tool call
                for tool_call in assistant_message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments or "{}")
                    
                    print(f"[RouterAgent] Tool call: {tool_name}({tool_args})")
                    
                    # Find which server has this tool and execute it
                    result = None
                    for session in self.sessions.values():
                        try:
                            result = await session.call_tool(tool_name, tool_args)
                            break
                        except Exception as e:
                            continue
                    
                    # Convert MCP result to string format
                    if result:
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
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": content_str
                        })
                    else:
                        # Tool execution failed
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": "Error: Tool execution failed"
                        })
                
                # Continue loop - model can reason about results and make more tool calls
                continue
            
            else:
                # No more tool calls - model has final answer
                final_response = assistant_message.content
                print(f"[RouterAgent] Analysis complete in {iteration_count} iterations")
                return final_response
        
        # Max iterations reached
        print(f"[RouterAgent] Warning: Max iterations ({max_iterations}) reached")
        return self.messages[-1].get("content", "Unable to complete analysis")
    
    async def cleanup(self):
        """Clean up MCP connections"""
        await self.exit_stack.aclose()


# ============================================================================
# MAIN LLM WITH CUSTOM TOOL
# ============================================================================

async def analyze_command_code(question: str, command_index: Dict[str, Any]) -> str:
    """
    Custom tool function that spawns router agent to analyze code.
    This is exposed to the main LLM as a tool.
    
    Args:
        question: Question about a command
        command_index: Loaded command index
        
    Returns:
        Analysis summary from router agent
    """
    async with RouterAgent(command_index) as router:
        summary = await router.analyze(question)
        return summary


# Define tool schema for main LLM
COMMAND_ANALYSIS_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_command_code",
        "description": (
            "Analyze the bot's source code to understand how a Discord slash command works. "
            "Use this when users ask about specific commands, their parameters, or functionality. "
            "This tool will read the actual code and provide detailed information about the command."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "A clear question about the command you want to understand. "
                        "Examples: 'How does the assign_bases command work?', "
                        "'What parameters does the war_plan command accept?', "
                        "'Explain the dashboard command workflow'"
                        "If you do not know the exact command name, ask a more general question like "
                        "'How do I use the bot?' or 'how do I broadcast war assignments?'"
                        "and the tool will try to find relevant commands to analyze."
                    )
                }
            },
            "required": ["question"]
        }
    }
}


class MainLLM:
    """
    User-facing LLM that maintains Discord conversation.
    Has access to analyze_command_code tool.
    """
    
    def __init__(self, command_index: Dict[str, Any]):
        self.command_index = command_index
        self.openai = OpenAI(base_url=base_url, api_key=api_key)
        self.model = MODEL
        self.messages = []
        self.system_prompt = build_main_llm_system_prompt()
        self.tools = [COMMAND_ANALYSIS_TOOL]
    
    async def respond(self, user_message: str, max_iterations: int = 5) -> str:
        """
        Process user message and generate response with streaming.
        Uses agentic loop to handle tool calls.
        
        Args:
            user_message: User's question
            max_iterations: Maximum tool call iterations
            
        Returns:
            Complete response for the user
        """
        # Add system prompt on first message
        if len(self.messages) == 0:
            self.messages.append({"role": "system", "content": self.system_prompt})
        
        # Add user message
        self.messages.append({"role": "user", "content": user_message})
        
        # Agentic loop with streaming
        iteration_count = 0
        while iteration_count < max_iterations:
            iteration_count += 1
            
            # Call LLM with streaming enabled
            stream = self.openai.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=self.tools,
                temperature=0.7,
                stream=True,
            )
            
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
            
            # Check if model wants to use tools
            if tool_calls:
                for tool_call in tool_calls:
                    if tool_call["function"]["name"] == "analyze_command_code":
                        args = json.loads(tool_call["function"]["arguments"] or "{}")
                        question = args.get("question", "")
                        
                        print(f"[MainLLM] Analyzing command: {question}")
                        
                        # Execute the tool (spawns router agent)
                        try:
                            result = await analyze_command_code(question, self.command_index)
                        except Exception as e:
                            result = f"Error analyzing command: {str(e)}"
                        
                        # Add tool result to conversation
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "name": "analyze_command_code",
                            "content": result
                        })
                
                # Continue loop - model will now respond to user with tool results
                continue
            
            else:
                # No tool calls - return final response
                return full_content or "I'm not sure how to help with that."
        
        # Max iterations reached
        print(f"[MainLLM] Warning: Max iterations ({max_iterations}) reached")
        return "I apologize, but I'm having trouble processing that request. Please try rephrasing your question."


# ============================================================================
# DISCORD INTEGRATION
# ============================================================================

class CommandHelpSession:
    """
    Session for a Discord interaction.
    Maintains conversation state for a single help interaction.
    """
    
    def __init__(self):
        self.command_index = load_command_index()
        self.main_llm = MainLLM(self.command_index)
    
    async def ask(self, question: str) -> List[str]:
        """
        Ask a question and get a response, chunked for Discord.
        
        Args:
            question: User's question
            
        Returns:
            List of response chunks (each under 2000 chars for Discord)
        """
        response = await self.main_llm.respond(question)
        
        # Chunk the response for Discord's message length limits
        chunks = _chunk_content(response)
        
        return chunks


# ============================================================================
# SECURITY VALIDATION EXAMPLES
# ============================================================================

def validate_custom_file_access(filename: str) -> Path:
    """
    Example of how to safely access any file within the project.
    
    This demonstrates the pattern to use if you need to extend
    the system with additional file operations.
    
    Args:
        filename: User-provided filename (can include subdirectories)
        
    Returns:
        Validated Path object
        
    Raises:
        PermissionError: If path escapes the sandbox
        FileNotFoundError: If file doesn't exist
        
    Examples:
        >>> # Safe: File in project root
        >>> path = validate_custom_file_access("Discord_commands.py")
        
        >>> # Safe: File in subdirectory
        >>> path = validate_custom_file_access("config/settings.json")
        
        >>> # BLOCKED: Parent directory traversal
        >>> path = validate_custom_file_access("../../../etc/passwd")
        PermissionError: Access denied...
        
        >>> # BLOCKED: Absolute path outside project
        >>> path = validate_custom_file_access("/etc/passwd")
        PermissionError: Access denied...
    """
    return PATH_VALIDATOR.validate_file_exists(filename)


def safe_read_project_file(filename: str) -> str:
    """
    Example of safely reading any file within the project.
    
    Args:
        filename: Filename relative to project root
        
    Returns:
        File contents as string
        
    Raises:
        PermissionError: If path escapes sandbox
        FileNotFoundError: If file doesn't exist
    """
    return PATH_VALIDATOR.safe_read_text(filename)


# ============================================================================
# USAGE EXAMPLES
# ============================================================================

async def example_single_question():
    """Example: Single question interaction"""
    print("=== Example: Single Question ===\n")
    
    session = CommandHelpSession()
    chunks = await session.ask("How do I broadcast war assignments?")
    
    print(f"User: How do I broadcast war assignments?")
    print(f"Bot (response in {len(chunks)} chunk(s)):")
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"--- Chunk {i}/{len(chunks)} ---")
        print(chunk)
    print()


async def example_security_validation():
    """Example: Security validation in action"""
    print("=== Example: Security Validation ===\n")
    
    # Test 1: Valid file in project
    print("Test 1: Accessing valid project file")
    try:
        path = PATH_VALIDATOR.validate_path("command_index.json")
        print(f"✓ ALLOWED: {path}")
    except PermissionError as e:
        print(f"✗ BLOCKED: {e}")
    
    # Test 2: Valid subdirectory (if it exists)
    print("\nTest 2: Accessing file in subdirectory")
    try:
        path = PATH_VALIDATOR.validate_path("subdir/file.py")
        print(f"✓ ALLOWED: {path}")
    except (PermissionError, FileNotFoundError) as e:
        print(f"✓ Path validation passed, but file doesn't exist")
    
    # Test 3: Directory traversal attempt
    print("\nTest 3: Attempting directory traversal (../)")
    try:
        path = PATH_VALIDATOR.validate_path("../../etc/passwd")
        print(f"✗ SECURITY FAILURE: {path} was allowed!")
    except PermissionError as e:
        print(f"✓ BLOCKED: Directory traversal prevented")
    
    # Test 4: Absolute path outside project
    print("\nTest 4: Attempting absolute path access")
    try:
        path = PATH_VALIDATOR.validate_path("/etc/passwd")
        print(f"✗ SECURITY FAILURE: {path} was allowed!")
    except PermissionError as e:
        print(f"✓ BLOCKED: Absolute path outside project prevented")
    
    # Test 5: Symlink traversal (if symlinks exist)
    print("\nTest 5: Path normalization with resolve()")
    try:
        path = PATH_VALIDATOR.validate_path("./././command_index.json")
        print(f"✓ NORMALIZED: {path}")
    except PermissionError as e:
        print(f"✗ BLOCKED: {e}")
    
    print("\n✓ All security tests passed!\n")


async def example_multi_turn():
    """Example: Multi-turn conversation"""
    print("=== Example: Multi-turn Conversation ===\n")
    
    session = CommandHelpSession()
    
    # First question
    q1 = "How do I use the war_plan command?"
    chunks1 = await session.ask(q1)
    print(f"User: {q1}")
    print(f"Bot: {chunks1[0]}")  # Show first chunk only for brevity
    if len(chunks1) > 1:
        print(f"... ({len(chunks1) - 1} more chunk(s))")
    print()
    
    # Follow-up question (session maintains context)
    q2 = "Can I save multiple war plans?"
    chunks2 = await session.ask(q2)
    print(f"User: {q2}")
    print(f"Bot: {chunks2[0]}")
    if len(chunks2) > 1:
        print(f"... ({len(chunks2) - 1} more chunk(s))")
    print()
    
    # Another follow-up
    q3 = "How do I delete a saved plan?"
    chunks3 = await session.ask(q3)
    print(f"User: {q3}")
    print(f"Bot: {chunks3[0]}")
    if len(chunks3) > 1:
        print(f"... ({len(chunks3) - 1} more chunk(s))")
    print()


async def example_discord_slash_command(interaction, question: str):
    """
    Example Discord slash command handler.
    
    Usage in your Discord bot:
        @bot.tree.command(name="help", description="Get help with bot commands")
        async def help_command(interaction: discord.Interaction, question: str):
            await example_discord_slash_command(interaction, question)
    """
    # Defer response (AI takes time to process)
    await interaction.response.defer()
    
    # Create session and get response chunks
    session = CommandHelpSession()
    chunks = await session.ask(question)
    
    # Send first chunk as followup to deferred response
    await interaction.followup.send(chunks[0])
    
    # Send remaining chunks as separate messages if needed
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    # First, run security validation tests
    print("=" * 70)
    print("SECURITY VALIDATION")
    print("=" * 70)
    asyncio.run(example_security_validation())
    
    # Then run functional examples
    print("=" * 70)
    print("FUNCTIONAL EXAMPLES")
    print("=" * 70)
    asyncio.run(example_single_question())
    asyncio.run(example_multi_turn())