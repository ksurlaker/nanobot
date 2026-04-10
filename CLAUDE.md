# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**nanobot** is an ultra-lightweight personal AI assistant framework (~3,500 lines of core code) that supports multiple chat platforms (Telegram, Discord, WhatsApp, Slack, Feishu, DingTalk, Email, QQ), multi-provider LLM support, persistent memory, scheduled tasks, and background subagent execution.

## Development Commands

### Setup and Installation
```bash
# Install from source (editable)
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"

# Initialize config and workspace
nanobot onboard
```

### Testing
```bash
# Run tests
pytest

# Run specific test file
pytest tests/test_tool_validation.py

# Run async tests (uses pytest-asyncio)
pytest tests/test_email_channel.py

# Count core agent lines (excludes channels/, cli/, providers/)
bash core_agent_lines.sh
```

### Code Quality
```bash
# Run linter
ruff check .

# Auto-fix linting issues
ruff check --fix .

# Format code
ruff format .
```

### Running the Agent
```bash
# Interactive chat mode
nanobot agent

# Single message mode
nanobot agent -m "Your message here"

# With logs enabled
nanobot agent --logs

# Start gateway (for chat platforms)
nanobot gateway

# View status
nanobot status
```

### Cron Management
```bash
# List scheduled jobs
nanobot cron list

# Add a job
nanobot cron add --name "daily" --message "Good morning" --cron "0 9 * * *"
nanobot cron add --name "hourly" --message "Check status" --every 3600

# Remove a job
nanobot cron remove <job_id>
```

## Architecture

### Core Agent Loop (`nanobot/agent/loop.py`)

The **AgentLoop** is the heart of nanobot:
- Consumes messages from the message bus (`MessageBus`)
- Builds context from conversation history, memory, and skills (`ContextBuilder`)
- Calls LLM provider with available tools
- Implements agentic loop: execute tool → add result to history → call LLM again, until completion
- Saves conversation to session manager
- Publishes responses back to message bus

**Key point**: The agent loop processes messages asynchronously and can handle multiple concurrent conversations across different channels/sessions.

### Message Flow Architecture

```
Chat Platform → Channel → MessageBus (inbound) → AgentLoop → MessageBus (outbound) → Channel → Chat Platform
```

1. **Channels** (`nanobot/channels/`) implement `BaseChannel` and convert platform-specific messages to/from `InboundMessage`/`OutboundMessage`
2. **MessageBus** (`nanobot/bus/queue.py`) decouples channels from the agent core using async queues
3. **ChannelManager** (`nanobot/channels/manager.py`) orchestrates all enabled channels and routes outbound messages

### Provider System (`nanobot/providers/`)

Two-layer architecture:
1. **ProviderSpec Registry** (`registry.py`): Metadata about each provider (keywords, model prefixes, env vars, gateway detection)
2. **LiteLLMProvider** (`litellm_provider.py`): Uses LiteLLM library for actual API calls

**Adding a New Provider**: Only requires 2 steps:
1. Add `ProviderSpec` entry to `PROVIDERS` tuple in `registry.py`
2. Add field to `ProvidersConfig` class in `config/schema.py`

Environment variables, model prefixing, and gateway detection work automatically.

**Gateway Detection** (priority order):
1. Explicit `provider_name` in config
2. API key prefix (e.g., `sk-or-` → OpenRouter)
3. API base URL keyword (e.g., "aihubmix")

### Tool System (`nanobot/agent/tools/`)

Extensible tool registry with OpenAI-compatible schemas:
- Tools extend `Tool` base class (`base.py`)
- `ToolRegistry` (`registry.py`) validates parameters and executes tools
- Built-in tools: file operations (read/write/edit/list), shell execution, web search/fetch, messaging, spawning subagents, cron scheduling

**Important**: Some tools (message, spawn, cron) need current channel/chat context, which is set before each agent turn via `update_context()`.

### Skills System (`nanobot/agent/skills.py`)

Progressive loading strategy:
- **Always-loaded skills**: Full content in system prompt (`always: true` in frontmatter)
- **Available skills**: Only summary shown; agent reads full content on demand
- Skills are markdown files at `~/.nanobot/workspace/skills/{skill-name}/SKILL.md`
- Frontmatter supports metadata: `description`, `always`, `requires` (bins/env)

Skills teach the agent how to use tools or perform complex tasks. They're the primary way to extend agent capabilities.

### Session Management (`nanobot/session/manager.py`)

- Sessions stored as JSONL at `~/.nanobot/sessions/{channel_chat_id}.jsonl`
- Session key format: `"{channel}:{chat_id}"`
- In-memory cache with lazy loading from disk
- Maintains recent message history (last 50 by default) for LLM context

### Memory System (`nanobot/agent/memory.py`)

Two-tier persistent memory:
1. **Long-term memory**: `~/.nanobot/workspace/memory/MEMORY.md` - permanent facts
2. **Daily notes**: `~/.nanobot/workspace/memory/YYYY-MM-DD.md` - time-scoped observations

Memory is included in system prompt and writable by agent via `write_file` tool.

### Cron/Scheduled Tasks (`nanobot/cron/service.py`)

Async job scheduler with three modes:
- `"at"`: One-shot at specific timestamp (ms)
- `"every"`: Repeating interval (ms)
- `"cron"`: Cron expression (e.g., "0 9 * * *")

Jobs execute through agent loop and can optionally deliver responses back to originating channel/chat.

### Subagent System (`nanobot/agent/subagent.py`)

Background task execution:
- Main agent calls `spawn_tool.spawn(task, label, origin_channel, origin_chat_id)`
- Subagent runs in asyncio task with focused system prompt (no memory/history)
- Limited tool subset (no message/spawn/cron to prevent recursion)
- Results published as system messages back to main agent
- Main agent summarizes and sends response to user

### Context Builder (`nanobot/agent/context.py`)

Assembles complete prompt from multiple sources (in order):
1. Identity (agent name, time, workspace location)
2. Bootstrap files (AGENTS.md, SOUL.md, USER.md, TOOLS.md, IDENTITY.md)
3. Memory (long-term + today's notes)
4. Always-loaded skills (full content)
5. Skills summary (XML listing of available skills)
6. Conversation history
7. Current user message (with optional base64 images for multimodal)

## Configuration

Config file: `~/.nanobot/config.json`

Key sections:
- `providers`: API keys and base URLs for LLM providers
- `agents.defaults`: Default model, max iterations, workspace path
- `channels`: Enable/configure chat platforms (Telegram, Discord, etc.)
- `tools`: Web search API key, exec timeout, workspace restrictions

**Security**: Set `tools.restrictToWorkspace: true` to sandbox agent to workspace directory only.

## Directory Structure

```
nanobot/
├── agent/          # Core agent logic
│   ├── loop.py     # Main agent loop (LLM ↔ tool execution)
│   ├── context.py  # Prompt assembly
│   ├── memory.py   # Persistent memory
│   ├── skills.py   # Skills loader
│   ├── subagent.py # Background task execution
│   └── tools/      # Built-in tools (file, shell, web, message, spawn, cron)
├── bus/            # Message routing (inbound/outbound queues)
├── channels/       # Chat platform integrations (Telegram, Discord, etc.)
├── config/         # Configuration schema and loader
├── cron/           # Scheduled tasks service
├── heartbeat/      # Proactive wake-up service
├── providers/      # LLM provider registry and LiteLLM wrapper
├── session/        # Conversation session management
├── skills/         # Bundled skills
├── cli/            # Typer-based CLI commands
└── utils/          # Helper utilities
```

## Key Design Principles

1. **Async-first**: All operations use `asyncio` for concurrent handling
2. **Decoupled**: Message bus separates channels from agent core
3. **Extensible**: Tool registry, skill loader, provider registry support plugins
4. **Lightweight**: ~3,500 lines of core code (excluding channels/cli/providers)
5. **Persistent**: Sessions, memory, cron jobs saved to disk
6. **Provider-agnostic**: Works with any LLM via LiteLLM + registry
7. **Multi-channel**: Single agent handles multiple chat platforms simultaneously

## Common Patterns

### Adding a New Tool

1. Create tool class extending `Tool` in `nanobot/agent/tools/`
2. Implement required properties: `name`, `description`, `parameters` (JSON Schema)
3. Implement `async execute(**kwargs)` method
4. Register in `ToolRegistry` initialization (typically in `loop.py`)

### Adding a New Channel

1. Create channel class extending `BaseChannel` in `nanobot/channels/`
2. Implement `start()`, `stop()`, `send()` methods
3. Add configuration to `ChannelsConfig` in `config/schema.py`
4. Register in `ChannelManager._initialize_channels()` in `channels/manager.py`

### Adding a New Provider

1. Add `ProviderSpec` to `PROVIDERS` in `providers/registry.py`
2. Add field to `ProvidersConfig` in `config/schema.py`

That's it! Environment setup and model routing work automatically.

## Testing Notes

- Tests use `pytest` with `pytest-asyncio` for async tests
- Test files in `tests/` directory
- Run `pytest` from root directory
- Email channel tests mock IMAP/SMTP for isolated testing

## Important Files to Check

When making changes, check these files for potential impacts:
- `nanobot/config/schema.py` - Configuration schema (Pydantic models)
- `nanobot/agent/loop.py` - Core agent processing logic
- `nanobot/providers/registry.py` - Provider metadata registry
- `nanobot/channels/manager.py` - Channel orchestration
- `pyproject.toml` - Dependencies and build configuration
