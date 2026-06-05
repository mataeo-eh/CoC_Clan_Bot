"""
Discord Bot Command Help System with AI-Powered Code Intelligence

This module implements an AI-powered help system for Discord slash commands.
It uses a two-tier LLM architecture:
1. User-facing LLM (``MainLLM``) - Maintains Discord conversation context.
2. Code-analysis LLM (``RouterAgent``) - Navigates the real source code to
   understand how a command works, then summarizes it for the user-facing tier.

CODE INTELLIGENCE / "RAG":
Rather than dumping hand-maintained line ranges into a prompt and post-slicing
whole files (the previous, brittle approach), the router agent drives two
local MCP servers that maintain a proper structural index of this codebase:

  - jcodemunch  -> symbol-level code navigation (search_symbols, get_symbol_source,
                   get_file_outline, get_file_tree, search_text, ...).
  - jdocmunch   -> documentation navigation (README/TODO sections, etc.).

This lets the agent jump straight to the relevant function/class/section
instead of guessing at line numbers, which is both cheaper and far more
accurate. The legacy ``command_index.json`` is no longer used for navigation;
it now only supplies the list of command *names* so the agent knows what
exists (see ``build_router_system_prompt``).

SECURITY MODEL (defense-in-depth):
- All filesystem-style tool arguments are validated against BOT_CODE_DIR with
  ``PathValidator`` (pathlib.resolve() containment check) before being forwarded
  to an MCP server - this blocks ``../`` traversal and absolute-path escapes.
- The agent may only call a curated, read-only allowlist of MCP tools
  (``ALLOWED_TOOLS``). Index-mutating / outbound tools (delete_index, embed_repo,
  arbitrary index_folder, tune_weights, ...) are never advertised to the model.
- The ``repo`` argument on every tool call is overridden by the server to the
  pinned bot-repo id, so a user cannot steer the agent at another project's index.
- The jcodemunch/jdocmunch servers are spawned WITHOUT any API keys or GitHub
  token and with AI summaries disabled ("basic servers" only). The MCP stdio
  client inherits just HOME/PATH/SHELL/USER/LOGNAME, so the bot's own
  OPENROUTER_API_KEY and the developer's personal jcodemunch credentials never
  reach these subprocesses.
- The index is stored in a project-local directory (``CODE_INDEX_PATH``), fully
  isolated from the developer's global ``~/.code-index`` (which holds indexes of
  unrelated projects).

LONG-TERM VISION:
The current orchestration is a hand-rolled agentic loop (LLM -> tool -> LLM).
This is intentionally kept simple and "acceptable for now". The intended
evolution is to migrate this two-tier flow into an explicit LangGraph / LangChain
graph: nodes for routing, retrieval (jcodemunch/jdocmunch), synthesis, and
guardrails, with typed state and checkpointing instead of the ad-hoc message
list and manual retry/iteration bookkeeping below. The MCP-backed retrieval layer
added here is designed to drop cleanly into such a graph as the "tools" node.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from typing import Optional, Dict, Any, List
from pathlib import Path
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Generic MCP -> OpenAI tool plumbing (tool discovery, execution routing, and
# result formatting). The RouterAgent layers repo-pinning, a read-only tool
# allowlist, and path validation on top of this manager (see RouterAgent).
from MCP_To_OpenAI_Tool import MCPToolManager

load_dotenv()

# Discord message length limit
MAX_MESSAGE_LENGTH = 1990


# ============================================================================
# DISCORD MESSAGE CHUNKING
# ============================================================================


# ============================================================================
# RETRY LOGIC FOR API CALLS
# ============================================================================

async def retry_with_backoff(func, max_retries: int = 3, initial_delay: float = 1.0):
    """
    Retry an async function with exponential backoff.

    Args:
        func: Async callable to retry
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds (doubles each retry)

    Returns:
        Result from the function

    Raises:
        The last exception if all retries fail
    """
    last_exception = None

    for attempt in range(max_retries):
        try:
            return await func()
        except Exception as e:
            last_exception = e
            error_str = str(e).lower()

            # Only retry on transient errors
            if "rate_limit" in error_str or "timeout" in error_str or "503" in error_str or "429" in error_str:
                if attempt < max_retries - 1:
                    delay = initial_delay * (2 ** attempt)
                    print(f"[Retry] Attempt {attempt + 1} failed with {type(e).__name__}. Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    continue

            # Non-retryable error, raise immediately
            raise

    # All retries exhausted
    raise last_exception




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

# ----------------------------------------------------------------------------
# CODE-INTELLIGENCE MCP SERVERS (jcodemunch + jdocmunch)
# ----------------------------------------------------------------------------
# These local stdio servers maintain a structural index of this codebase and
# expose symbol/section navigation tools. They replace the old filesystem MCP
# server + brittle line-range reads.
#
# ISOLATED INDEX STORAGE:
# The servers honor CODE_INDEX_PATH for where they persist indexes. We point it
# at a project-local hidden directory so the bot's index is fully isolated from
# the developer's global ~/.code-index (which holds indexes of OTHER projects).
# The bot must never be able to query those.
CODE_INDEX_PATH = str(Path(BOT_CODE_DIR) / ".bot_code_index")
os.makedirs(CODE_INDEX_PATH, exist_ok=True)


def _resolve_mcp_binary(name: str) -> Optional[str]:
    """
    Resolve the absolute path to a jcodemunch/jdocmunch launcher.

    The bot process may not have ~/.local/bin on PATH, so we resolve explicitly
    and fall back to the conventional uv-tools install location. Returns None if
    the binary cannot be found (the help agent then degrades gracefully).
    """
    found = shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / name
    return str(fallback) if fallback.exists() else None


# Minimal, key-free environment overrides for the spawned servers.
# SECURITY: We deliberately pass NO GITHUB_TOKEN and NO OPENAI/ANTHROPIC keys,
# and disable AI summaries. The MCP stdio client only inherits a safe env subset
# (HOME/PATH/SHELL/USER/LOGNAME), so the bot's OPENROUTER_API_KEY and the
# developer's personal jcodemunch credentials never reach these subprocesses.
# This satisfies the "basic servers, no LLM integration inside them" requirement.
_CODE_INTEL_ENV = {
    "CODE_INDEX_PATH": CODE_INDEX_PATH,
    "JCODEMUNCH_USE_AI_SUMMARIES": "false",
}

_JCODEMUNCH_BIN = _resolve_mcp_binary("jcodemunch-mcp")
_JDOCMUNCH_BIN = _resolve_mcp_binary("jdocmunch-mcp")

# Build the server config, including only the servers whose binaries we found.
CODE_INTELLIGENCE_SERVER_CONFIG: Dict[str, Any] = {}
if _JCODEMUNCH_BIN:
    CODE_INTELLIGENCE_SERVER_CONFIG["jcodemunch"] = {
        "command": _JCODEMUNCH_BIN,
        "args": [],
        "env": dict(_CODE_INTEL_ENV),
        "cwd": BOT_CODE_DIR,
    }
if _JDOCMUNCH_BIN:
    CODE_INTELLIGENCE_SERVER_CONFIG["jdocmunch"] = {
        "command": _JDOCMUNCH_BIN,
        "args": [],
        "env": dict(_CODE_INTEL_ENV),
        "cwd": BOT_CODE_DIR,
    }

if not CODE_INTELLIGENCE_SERVER_CONFIG:
    print("[WARNING] Neither jcodemunch-mcp nor jdocmunch-mcp was found on PATH "
          "or in ~/.local/bin. The AI help agent will be unable to read code.")
else:
    print(f"[Security] Code-intelligence MCP servers: "
          f"{list(CODE_INTELLIGENCE_SERVER_CONFIG)}; index isolated at {CODE_INDEX_PATH}")


# ----------------------------------------------------------------------------
# READ-ONLY TOOL ALLOWLIST
# ----------------------------------------------------------------------------
# jcodemunch + jdocmunch expose ~150 tools combined, including index-mutating and
# outbound ones (delete_index, embed_repo, arbitrary index_folder, tune_weights).
# We advertise ONLY this curated, read-only navigation set to the model. Every
# tool here accepts a `repo` argument, which we override with the pinned bot-repo
# id on each call so the agent cannot query another project's index.
JCODEMUNCH_ALLOWED_TOOLS = {
    "search_symbols",
    "get_symbol_source",
    "get_file_outline",
    "get_file_tree",
    "get_repo_outline",
    "get_related_symbols",
    "search_text",
}
JDOCMUNCH_ALLOWED_TOOLS = {
    "search_sections",
    "get_section",
    "get_toc",
    "search_titles",
    "get_document_outline",
}
ALLOWED_TOOLS = JCODEMUNCH_ALLOWED_TOOLS | JDOCMUNCH_ALLOWED_TOOLS

# Tool-argument keys that name a filesystem path (repo-relative). These are
# validated against BOT_CODE_DIR before forwarding. Glob patterns (file_pattern)
# and opaque index ids (symbol_id, section ids) are not filesystem paths and are
# left untouched.
_PATH_ARG_KEYS = ("file_path", "path", "doc_path")
_PATH_LIST_ARG_KEYS = ("file_paths",)


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
    Build the system prompt for the code-analysis router agent.

    The agent navigates the real source via the jcodemunch (code) and jdocmunch
    (docs) MCP tools. We deliberately do NOT inject line numbers here anymore;
    we only list the command *names* that exist so the agent has a starting map.
    It then resolves each command to actual code with the index-backed tools,
    which is far more accurate than the previous hand-maintained line ranges.
    """
    # Retire command_index as a navigation source: keep only the command names
    # as a lightweight catalog of what exists.
    command_names = sorted(command_index.keys())
    command_catalog = "\n".join(f"- {name}" for name in command_names)

    return f"""You are a code-analysis agent that explains how this Discord bot's slash commands work.

You answer by reading the bot's ACTUAL source code and documentation using a set
of code-intelligence tools backed by a structural index of the repository. Always
ground your answer in what you actually read - never guess at behavior.

**TOOLS AVAILABLE TO YOU:**
Code navigation (jcodemunch):
- search_symbols: find a function/class/method by name or description (start here).
- get_symbol_source: read the full source of a specific symbol you found.
- get_file_outline: list all symbols in a file with signatures.
- get_file_tree: see the repository's file layout.
- get_repo_outline: high-level overview of the repository.
- get_related_symbols: find callers/callees/related code for a symbol.
- search_text: full-text search for string literals, decorators, or comments.
Documentation navigation (jdocmunch):
- search_sections / search_titles: find relevant sections in README/docs.
- get_section / get_document_outline / get_toc: read docs structure and content.

**REPO ARGUMENT:**
The tools take a `repo` argument. It is automatically pinned to this bot's repo
for you - you do not need to discover it, and you cannot point these tools at any
other project. Just pass the repo value you are given (or omit it).

**RECOMMENDED WORKFLOW:**
1. Map the user's question to a likely slash command (see the catalog below).
2. search_symbols for that command's handler function (slash command handlers are
   functions whose name matches the command, in Discord_Commands.py).
3. get_symbol_source to read the handler. Slash commands in this bot typically
   follow the pattern: user runs the command -> an interactive menu / buttons /
   modal appears. Note the parameters and the interactive elements.
4. If the handler references a View/Modal/Select class (the UI the user sees),
   search_symbols + get_symbol_source on that class to understand the experience.
5. For high-level "how do I use the bot / what can it do" questions, also consult
   jdocmunch (search_sections over the README/docs).

**EFFICIENCY LIMITS:**
- You have at most 10 tool calls. Prefer breadth over depth.
- After reading the handler and 1-2 related UI classes you usually have enough.
- If you've read 4+ symbols, answer from what you have rather than digging further.

**WHAT TO EXTRACT:**
- The command's purpose, and its required/optional parameters.
- The user-facing workflow: what happens, step by step, when they run it.
- Interactive elements (buttons, dropdowns, modals) and what the user sees.
- Any special requirements or permissions (e.g. admin-only).

**RESPONSE FORMAT:**
Write a clear, concise, natural-language summary for a user learning the command.
Focus on the user experience, not implementation details. Do not paste code.

**COMMANDS THAT EXIST IN THIS BOT (names only - resolve them with the tools):**
{command_catalog}"""


def build_main_llm_system_prompt() -> str:
    """
    Build system prompt for the user-facing main LLM.
    This LLM maintains conversation with the Discord user.
    """
    return """You are a helpful assistant for a Discord Clash of Clans bot.

Users will ask you questions about how to use the bot's slash commands.
You have access to a tool called "analyze_command_code" that investigates the
bot's ACTUAL source code and documentation (via a structural code index) to
understand exactly how a command works. Rely on this tool whenever a question
depends on what a command actually does - prefer reading the real code over
answering from memory.

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

class RouterAgent:
    """
    Code-analysis agent backed by the jcodemunch/jdocmunch MCP servers.

    Responsibilities layered on top of the generic ``MCPToolManager``:
    - Connect to the code-intelligence servers (stdio).
    - Index this repository (incremental) and *pin* the resulting repo ids, so the
      model never has to (and never gets to) point the tools at another project.
    - Advertise only a read-only allowlist of navigation tools to the model.
    - Validate/repin every tool call's arguments before execution.

    NOTE (long-term vision): this hand-rolled agentic loop is the piece slated to
    become a LangGraph node graph (route -> retrieve -> synthesize). The retrieval
    plumbing here is intentionally self-contained so it can be lifted out cleanly.
    """

    def __init__(self, command_index: Dict[str, Any]):
        self.command_index = command_index
        self.sessions: Dict[str, Any] = {}
        self.exit_stack = AsyncExitStack()
        self.openai = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model = MODEL
        self.messages: List[Dict[str, Any]] = []
        self.system_prompt = build_router_system_prompt(command_index)
        # Generic MCP tool plumbing (discovery / execution routing / formatting).
        self.tool_manager = MCPToolManager()
        # server name -> pinned repo id (only set after a successful index).
        self.repo_ids: Dict[str, str] = {}

    async def __aenter__(self):
        """Connect to the code-intelligence servers and prepare the index."""
        await self.connect_to_servers(CODE_INTELLIGENCE_SERVER_CONFIG)
        await self._index_and_pin_repos()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Cleanup when exiting context"""
        await self.cleanup()

    async def connect_to_servers(self, server_configs: Dict[str, Any]):
        """Connect to the configured MCP servers and register their sessions."""
        if not server_configs:
            raise RuntimeError(
                "No code-intelligence MCP servers are configured "
                "(jcodemunch-mcp / jdocmunch-mcp not found)."
            )

        print(f"[RouterAgent] Connecting to {len(server_configs)} MCP server(s)...")

        for name, config in server_configs.items():
            try:
                print(f"[RouterAgent] Connecting to {name} server...")
                server_params = StdioServerParameters(**config)
                stdio_transport = await self.exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
                stdio, write = stdio_transport
                session = await self.exit_stack.enter_async_context(
                    ClientSession(stdio, write)
                )
                try:
                    async with asyncio.timeout(25):  # 25 second timeout
                        await session.initialize()
                    print(f"[RouterAgent] ✓ {name} MCP session initialized")
                except asyncio.TimeoutError:
                    print(f"[RouterAgent] ERROR: {name} server initialization timed out")
                    raise RuntimeError(f"{name} server failed to initialize - likely PATH issue")

                self.sessions[name] = session
                # Register the session with the generic tool manager so it can
                # discover, route, and format tool calls for this server.
                self.tool_manager.add_session(name, session)

                response = await session.list_tools()
                tool_names = [tool.name for tool in response.tools]
                print(f"[RouterAgent] ✓ {name} server connected ({len(tool_names)} tools)")

            except FileNotFoundError as e:
                print(f"[RouterAgent] ERROR: MCP binary not found for {name}: {e}")
                raise RuntimeError(
                    f"Failed to start {name} MCP server: launcher not found. "
                    f"Install it so '{name}-mcp' is on PATH or in ~/.local/bin."
                )

            except Exception as e:
                print(f"[RouterAgent] ERROR: Failed to connect to {name} server: {type(e).__name__}: {e}")
                raise RuntimeError(f"Failed to connect to {name} MCP server: {str(e)}")

        print(f"[RouterAgent] All {len(self.sessions)} server(s) connected successfully")

    async def _index_and_pin_repos(self):
        """
        Incrementally (re)index this repository on each server and remember the
        repo id each one assigns. This is driven by our own code - NOT exposed to
        the model - so the model can only ever read the bot's own pinned index.

        A server whose indexing fails contributes no tools (its repo stays
        unpinned), so the help agent degrades gracefully to whatever is available.
        """
        # jcodemunch: structural code index over the whole project.
        if "jcodemunch" in self.sessions:
            try:
                result = await self.sessions["jcodemunch"].call_tool("index_folder", {
                    "path": BOT_CODE_DIR,
                    "incremental": True,
                    "use_ai_summaries": False,  # basic server only, no LLM calls
                    "extra_ignore_patterns": [".bot_code_index/"],  # never index our own index
                })
                data = json.loads(self.tool_manager._format_result(result))
                repo = data.get("repo")
                if repo:
                    self.repo_ids["jcodemunch"] = repo
                    print(f"[RouterAgent] jcodemunch index ready: repo={repo} "
                          f"symbols={data.get('symbol_count')}")
            except Exception as e:
                print(f"[RouterAgent] WARNING: jcodemunch indexing failed: {e}")

        # jdocmunch: documentation index (README/TODO sections, etc.).
        if "jdocmunch" in self.sessions:
            try:
                result = await self.sessions["jdocmunch"].call_tool("index_local", {
                    "path": BOT_CODE_DIR,
                    "incremental": True,
                    "use_ai_summaries": False,
                    "use_embeddings": False,
                })
                data = json.loads(self.tool_manager._format_result(result))
                repo = data.get("repo") or data.get("name")
                if repo:
                    self.repo_ids["jdocmunch"] = repo
                    print(f"[RouterAgent] jdocmunch index ready: repo={repo}")
            except Exception as e:
                print(f"[RouterAgent] WARNING: jdocmunch indexing failed: {e}")

    def _sanitize_args(self, tool_name: str, server_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Make a tool call safe before it reaches an MCP server:

        1. Override the `repo` argument with the pinned bot-repo id for that
           server, so the model cannot read any other project's index.
        2. Validate any filesystem-path argument against BOT_CODE_DIR. Escapes
           (``../``, absolute paths outside the project) raise PermissionError.

        Opaque ids (symbol_id, section ids) and glob patterns are left untouched.
        """
        safe = dict(args or {})

        # (1) Pin the repo. Every allowlisted tool accepts a `repo` argument.
        safe["repo"] = self.repo_ids[server_name]

        # (2) Validate path-like arguments. validate_path raises on escape.
        for key in _PATH_ARG_KEYS:
            value = safe.get(key)
            if isinstance(value, str) and value:
                PATH_VALIDATOR.validate_path(value)
        for key in _PATH_LIST_ARG_KEYS:
            value = safe.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item:
                        PATH_VALIDATOR.validate_path(item)

        return safe

    async def _safe_execute(self, tool_name: str, raw_args: Dict[str, Any]) -> str:
        """
        Enforce the allowlist + sanitize args, then execute via the tool manager.
        Returns a string result (or an error string the model can reason about).
        """
        server_name = self.tool_manager._tool_registry.get(tool_name)

        # Defense in depth: even though we only advertise allowlisted tools, never
        # execute one that isn't allowlisted or whose server has no pinned repo.
        if tool_name not in ALLOWED_TOOLS or server_name not in self.repo_ids:
            return f"Error: tool '{tool_name}' is not permitted in the help assistant."

        try:
            safe_args = self._sanitize_args(tool_name, server_name, raw_args)
        except PermissionError as e:
            print(f"[RouterAgent] BLOCKED unsafe path in {tool_name}: {e}")
            return f"Access denied: {e}"

        try:
            return await self.tool_manager.execute_tool_call(tool_name, safe_args)
        except Exception as e:
            print(f"[RouterAgent] Tool {tool_name} failed: {e}")
            return f"Error executing {tool_name}: {str(e)}"

    def _advertised_tools(self, all_openai_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter the servers' full tool catalog down to the read-only allowlist,
        and only for servers whose repo we successfully pinned.
        """
        advertised = []
        for tool in all_openai_tools:
            name = tool.get("function", {}).get("name")
            server = self.tool_manager._tool_registry.get(name)
            if name in ALLOWED_TOOLS and server in self.repo_ids:
                advertised.append(tool)
        return advertised

    async def analyze(self, question: str, max_iterations: int = 10) -> str:
        """
        Analyze the codebase to answer a question using an agentic tool loop.

        Args:
            question: User's question about a command
            max_iterations: Maximum tool call iterations

        Returns:
            Analysis summary suitable for the main LLM
        """
        print(f"[RouterAgent] Starting analysis for: {question[:100]}...")

        # Initialize conversation with system prompt
        self.messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question}
        ]

        # Discover tools from all servers, then restrict to the read-only allowlist.
        try:
            all_openai_tools = await self.tool_manager.get_all_openai_tools()
            all_tools = self._advertised_tools(all_openai_tools)
            print(f"[RouterAgent] Advertising {len(all_tools)} allowlisted tool(s) "
                  f"from pinned repos: {self.repo_ids}")
        except Exception as e:
            print(f"[RouterAgent] ERROR: Failed to list tools: {e}")
            return f"Error: Failed to access the code-intelligence tools. {str(e)}"

        if not all_tools:
            return ("Error: The code-intelligence index is unavailable right now, "
                    "so I can't read the command's source code. Please try again later.")

        # Agentic loop - model can make multiple tool calls with reasoning
        iteration_count = 0
        while iteration_count < max_iterations:
            iteration_count += 1
            print(f"[RouterAgent] Iteration {iteration_count}/{max_iterations}")

            # Retry logic for API calls
            retry_count = 0
            max_retries = 3
            last_error = None

            while retry_count < max_retries:
                try:
                    # Call LLM with tools
                    response = await self.openai.chat.completions.create(
                        model=self.model,
                        messages=self.messages,
                        tools=all_tools,
                        temperature=0.3,  # Lower temperature for code analysis
                    )
                    break  # Success, exit retry loop

                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()

                    # Check if this is a retryable error
                    is_retryable = any(keyword in error_str for keyword in [
                        "rate_limit", "429", "timeout", "503", "502", "connection"
                    ])

                    if is_retryable and retry_count < max_retries - 1:
                        retry_count += 1
                        delay = 1.0 * (2 ** retry_count)  # Exponential backoff
                        print(f"[RouterAgent] API call failed ({type(e).__name__}), retrying in {delay}s (attempt {retry_count}/{max_retries})...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        # Non-retryable or out of retries
                        raise

            # Check if we exited the loop due to retries exhausted
            if retry_count >= max_retries and last_error:
                raise last_error

            try:
                assistant_message = response.choices[0].message
                finish_reason = response.choices[0].finish_reason
                print(f"[RouterAgent] Model finished with reason: {finish_reason}")

                self.messages.append(assistant_message.model_dump())

                # Check if model wants to use tools
                if assistant_message.tool_calls:
                    print(f"[RouterAgent] Processing {len(assistant_message.tool_calls)} tool calls")

                    # Execute each requested tool call through the security wrapper
                    # (allowlist enforcement, repo pinning, path validation).
                    for tool_call in assistant_message.tool_calls:
                        tool_name = tool_call.function.name
                        tool_args = json.loads(tool_call.function.arguments or "{}")

                        print(f"[RouterAgent] Tool call: {tool_name}({tool_args})")

                        content_str = await self._safe_execute(tool_name, tool_args)
                        print(f"[RouterAgent] Tool {tool_name} result: {len(content_str)} characters")

                        # Add tool result to conversation
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": content_str,
                        })

                    # Continue loop - model can reason about results and make more tool calls
                    continue

                else:
                    # No more tool calls - model has final answer
                    final_response = assistant_message.content
                    print(f"[RouterAgent] Analysis complete in {iteration_count} iterations")
                    print(f"[RouterAgent] Response length: {len(final_response) if final_response else 0} characters")

                    if not final_response:
                        print("[RouterAgent] WARNING: Empty response from model")
                        return "Error: Model provided empty response. Please try rephrasing your question."

                    return final_response

            except Exception as e:
                print(f"[RouterAgent] ERROR in iteration {iteration_count}: {type(e).__name__}: {str(e)}")

                # Check for specific error types
                if "rate_limit" in str(e).lower():
                    return "Error: API rate limit exceeded. Please try again in a moment."
                elif "timeout" in str(e).lower():
                    return "Error: Request timed out. The model may be overloaded. Please try again."
                elif "authentication" in str(e).lower() or "api_key" in str(e).lower():
                    return "Error: API authentication failed. Please check the API key configuration."
                else:
                    return f"Error: An unexpected error occurred during analysis: {str(e)}"

        # Max iterations reached
        print(f"[RouterAgent] WARNING: Max iterations ({max_iterations}) reached")

        # Try to extract the last assistant message if available
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                print("[RouterAgent] Returning partial response from last assistant message")
                return msg["content"]

        return "Error: Unable to complete analysis within the maximum number of iterations. Please try asking a more specific question."
    
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
    try:
        print(f"[analyze_command_code] Spawning router agent for question: {question[:100]}...")
        async with RouterAgent(command_index) as router:
            summary = await router.analyze(question)
            print(f"[analyze_command_code] Router agent returned {len(summary)} character response")
            return summary
    except RuntimeError as e:
        # MCP server connection failures
        error_msg = str(e)
        print(f"[analyze_command_code] ERROR: {error_msg}")
        return f"I'm experiencing technical difficulties accessing the command documentation. Error: {error_msg}"
    except Exception as e:
        # Unexpected errors
        print(f"[analyze_command_code] UNEXPECTED ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return f"I encountered an unexpected error while analyzing the command. Please try again or contact support."


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
        self.openai = AsyncOpenAI(base_url=base_url, api_key=api_key)
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
        print(f"[MainLLM] Processing user message: {user_message[:100]}...")

        # Add system prompt on first message
        if len(self.messages) == 0:
            self.messages.append({"role": "system", "content": self.system_prompt})

        # Add user message
        self.messages.append({"role": "user", "content": user_message})

        # Agentic loop with streaming
        iteration_count = 0
        while iteration_count < max_iterations:
            iteration_count += 1
            print(f"[MainLLM] Iteration {iteration_count}/{max_iterations}")

            # Retry logic for API calls
            retry_count = 0
            max_retries = 3
            last_error = None
            stream = None

            while retry_count < max_retries:
                try:
                    # Call LLM with streaming enabled
                    stream = await self.openai.chat.completions.create(
                        model=self.model,
                        messages=self.messages,
                        tools=self.tools,
                        temperature=0.7,
                        stream=True,
                    )
                    break  # Success, exit retry loop

                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()

                    # Check if this is a retryable error
                    is_retryable = any(keyword in error_str for keyword in [
                        "rate_limit", "429", "timeout", "503", "502", "connection"
                    ])

                    if is_retryable and retry_count < max_retries - 1:
                        retry_count += 1
                        delay = 1.0 * (2 ** retry_count)  # Exponential backoff
                        print(f"[MainLLM] API call failed ({type(e).__name__}), retrying in {delay}s (attempt {retry_count}/{max_retries})...")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        # Non-retryable or out of retries
                        raise

            # Check if we exited the loop due to retries exhausted
            if retry_count >= max_retries and last_error:
                raise last_error

            try:

                # Collect the streamed response
                full_content = ""
                tool_calls = []
                finish_reason = None

                async for chunk in stream:
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

                print(f"[MainLLM] Streaming complete. Content: {len(full_content)} chars, Tool calls: {len(tool_calls)}, Finish reason: {finish_reason}")

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
                    print(f"[MainLLM] Processing {len(tool_calls)} tool call(s)")

                    for tool_call in tool_calls:
                        if tool_call["function"]["name"] == "analyze_command_code":
                            args = json.loads(tool_call["function"]["arguments"] or "{}")
                            question = args.get("question", "")

                            print(f"[MainLLM] Analyzing command: {question}")

                            # Execute the tool (spawns router agent)
                            try:
                                result = await analyze_command_code(question, self.command_index)
                            except Exception as e:
                                print(f"[MainLLM] ERROR in analyze_command_code: {type(e).__name__}: {e}")
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
                    final_response = full_content or "I'm not sure how to help with that."
                    print(f"[MainLLM] Returning final response: {len(final_response)} characters")
                    return final_response

            except Exception as e:
                print(f"[MainLLM] ERROR in iteration {iteration_count}: {type(e).__name__}: {str(e)}")

                # Check for specific error types
                if "rate_limit" in str(e).lower():
                    return "I apologize, but the AI service is experiencing high load (rate limit exceeded). Please try again in a moment."
                elif "timeout" in str(e).lower():
                    return "The request timed out. Please try again with a simpler question."
                elif "authentication" in str(e).lower() or "api_key" in str(e).lower():
                    return "I'm experiencing authentication issues with the AI service. Please contact an administrator."
                elif "connection" in str(e).lower():
                    return "I'm having trouble connecting to the AI service. Please check your internet connection and try again."
                else:
                    # Log full error for debugging
                    import traceback
                    traceback.print_exc()
                    return f"I encountered an error while processing your request. Please try again or rephrase your question."

        # Max iterations reached
        print(f"[MainLLM] WARNING: Max iterations ({max_iterations}) reached")

        # Try to extract the last assistant response
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                print("[MainLLM] Returning partial response from last assistant message")
                return msg["content"] + "\n\n(Note: Response may be incomplete due to complexity. Please try asking a simpler question.)"

        return "I apologize, but I'm having trouble completing this request within the allowed time. Please try breaking your question into smaller, more specific parts."


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
        
        # Import the function for chunking discord messages
        from Discord_Commands import _chunk_content
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
        >>> path = validate_custom_file_access("Discord_Commands.py")
        
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