# Aegis Integration Plan for Nanobot

## Overview

Integrate [Aegis](../aegis/) — a deterministic policy enforcement framework — into nanobot to gate all tool calls with pre-call and post-execution security checks.

Aegis's **Standalone Client** is the right integration point — nanobot doesn't use LangChain/LangGraph. The hook goes into `ToolRegistry.execute()`, which is the single chokepoint for all tool calls.

```
InboundMessage → AgentLoop._run_agent_loop()
    → AegisEnforcer.check_pre_call()   ← NEW
    → ToolRegistry.execute()
    → AegisEnforcer.record_result()    ← NEW
    → AegisEnforcer.check_post_exec()  ← NEW
    → LLM gets result
```

Nanobot's `session_key` (e.g., `telegram:12345`) maps 1:1 to an Aegis session ID.

---

## Step 1 — Add aegis as a dependency

In `pyproject.toml`, under `dependencies`:
```toml
"aegis @ file:///path/to/aegis",
```

Or install directly:
```bash
pip install -e /path/to/aegis
```

---

## Step 2 — Config schema

Add to `nanobot/config/schema.py`:

```python
class AegisConfig(BaseModel):
    enabled: bool = False
    policy_directory: str = "~/.nanobot/aegis/policies"
    tag_rules_file: str | None = None
    memory_directory: str = "~/.nanobot/aegis/memory"
    audit_log: str | None = "~/.nanobot/aegis/audit.jsonl"
    # Optional: session declares purpose per-channel
    default_purpose: str | None = None
```

And add `aegis: AegisConfig = AegisConfig()` to the root config class.

---

## Step 3 — Create `nanobot/agent/security.py`

```python
"""Aegis policy enforcement for nanobot tool calls."""
from __future__ import annotations
from typing import TYPE_CHECKING
from loguru import logger

if TYPE_CHECKING:
    from nanobot.config.schema import AegisConfig


class AegisEnforcer:
    """Wraps AegisClient for nanobot's session/tool lifecycle."""

    def __init__(self, config: AegisConfig):
        from aegis import AegisClient
        from aegis.config import AegisConfig as _AegisConfig
        import os

        aegis_cfg = _AegisConfig(
            policy_directory=os.path.expanduser(config.policy_directory),
            tag_rules_file=os.path.expanduser(config.tag_rules_file) if config.tag_rules_file else None,
            memory_store_type="file",
            memory_directory=os.path.expanduser(config.memory_directory),
            audit={"enabled": bool(config.audit_log), "log_file": os.path.expanduser(config.audit_log or "")},
        )
        self.client = AegisClient(aegis_cfg)
        self.default_purpose = config.default_purpose
        self._sessions: dict[str, str] = {}  # nanobot session_key → aegis session_id

    def get_or_create_session(self, session_key: str) -> str:
        if session_key not in self._sessions:
            sid = self.client.start_session(
                declared_purpose=self.default_purpose or session_key,
            )
            self._sessions[session_key] = sid
            logger.debug("Aegis session started for {}: {}", session_key, sid)
        return self._sessions[session_key]

    def end_session(self, session_key: str) -> None:
        if sid := self._sessions.pop(session_key, None):
            self.client.end_session(sid)

    async def check_pre_call(self, session_key: str, tool_name: str, args: dict) -> str | None:
        """Returns denial message if blocked, else None."""
        from aegis.core.models import Verdict
        try:
            sid = self.get_or_create_session(session_key)
            result = self.client.check_pre_call(sid, tool_name, args)
            if result.final_verdict == Verdict.DENY:
                reason = result.blocking_reason or "Policy violation"
                logger.warning("Aegis blocked {}: {}", tool_name, reason)
                return f"Error: Tool call denied by security policy — {reason}"
        except Exception as e:
            logger.error("Aegis pre-call check failed (allowing): {}", e)
        return None

    async def record_and_check_post(self, session_key: str, tool_name: str, args: dict, result: str) -> str:
        """Records result tags and runs post-execution check. Returns (possibly redacted) result."""
        from aegis.core.models import Verdict
        try:
            sid = self.get_or_create_session(session_key)
            post = self.client.check_post_execution(sid, tool_name, args, result)
            if post.final_verdict == Verdict.DENY:
                reason = post.blocking_reason or "Policy violation"
                logger.warning("Aegis blocked post-exec for {}: {}", tool_name, reason)
                return f"[Result redacted by security policy: {reason}]"
        except Exception as e:
            logger.error("Aegis post-exec check failed (passing through): {}", e)
        return result
```

---

## Step 4 — Modify `ToolRegistry.execute()` with optional hooks

Add two optional async callbacks to `ToolRegistry` (`nanobot/agent/tools/registry.py`):

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._pre_call_hook = None   # async (name, params) -> str | None
        self._post_exec_hook = None  # async (name, params, result) -> str

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        _HINT = "\n\n[Analyze the error above and try a different approach.]"
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"
        try:
            params = tool.cast_params(params)
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT

            # Aegis pre-call
            if self._pre_call_hook:
                if denial := await self._pre_call_hook(name, params):
                    return denial

            result = await tool.execute(**params)

            # Aegis post-execution
            if self._post_exec_hook:
                result = await self._post_exec_hook(name, params, result)

            if isinstance(result, str) and result.startswith("Error"):
                return result + _HINT
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _HINT
```

---

## Step 5 — Wire it into `AgentLoop`

In `AgentLoop.__init__()` (`nanobot/agent/loop.py`), accept an optional aegis config and create the enforcer:

```python
# In __init__ signature:
aegis_config: AegisConfig | None = None,

# After self.tools = ToolRegistry():
if aegis_config and aegis_config.enabled:
    from nanobot.agent.security import AegisEnforcer
    self._aegis = AegisEnforcer(aegis_config)
else:
    self._aegis = None
```

In `_process_message()`, set the hooks before calling `_run_agent_loop()` and tear down on `/new`:

```python
# Before _run_agent_loop():
if self._aegis:
    _key = key  # nanobot session key, e.g. "telegram:12345"
    self.tools._pre_call_hook = lambda n, p: self._aegis.check_pre_call(_key, n, p)
    self.tools._post_exec_hook = lambda n, p, r: self._aegis.record_and_check_post(_key, n, p, r)

# On /new command, end the aegis session:
if self._aegis:
    self._aegis.end_session(key)
```

Also pass `aegis_config` through from the CLI entrypoint (`nanobot/cli/`) when constructing `AgentLoop`.

---

## Step 6 — Write policies for nanobot's tools

### `~/.nanobot/aegis/policies/shell_restriction.yaml`

```yaml
policy "restrict_shell_exec":
  description: "Deny dangerous shell commands"
  enforcement: [PRE_CALL]
  applies_to: ["exec_shell"]

rule "allow_trusted":
  ctx.session.purpose_tags.any_of(["admin", "trusted"])
  => ALLOW

rule "deny_dangerous":
  ctx.tool.args.command.contains("rm -rf") OR
  ctx.tool.args.command.contains("curl") AND NOT ctx.session.purpose_tags.any_of(["trusted"])
  => DENY
  reason: "Dangerous shell commands require trusted session"
```

### `~/.nanobot/aegis/policies/data_exfiltration.yaml`

```yaml
policy "prevent_data_exfil":
  description: "Block external messaging after sensitive file access"
  enforcement: [PRE_CALL]
  applies_to: ["send_message", "web_fetch"]

rule "block_after_sensitive_data":
  ctx.session.tags.any_of(["pii", "credentials", "secrets"]) AND
  NOT ctx.session.purpose_tags.any_of(["audit", "trusted"])
  => DENY
  reason: "Cannot send messages after accessing sensitive data"
```

### `~/.nanobot/aegis/tag_rules.yaml`

```yaml
rules:
  - name: "detect_env_file"
    tool_pattern: "read_file"
    arg_pattern: ".env"
    tags: ["credentials", "secrets"]

  - name: "detect_ssh_keys"
    result_pattern: "BEGIN.*PRIVATE KEY"
    tags: ["credentials", "pii"]

  - name: "detect_shell_output"
    tool_pattern: "exec_shell"
    tags: ["shell_exec"]
```

---

## Files Changed

| File | Change |
|------|--------|
| `pyproject.toml` | Add aegis dependency |
| `nanobot/config/schema.py` | Add `AegisConfig` model + field on root config |
| `nanobot/agent/tools/registry.py` | Add `_pre_call_hook` / `_post_exec_hook` |
| `nanobot/agent/security.py` | **New** — `AegisEnforcer` class |
| `nanobot/agent/loop.py` | Accept `aegis_config`, wire hooks per session |
| `nanobot/cli/` | Pass `aegis_config` when constructing `AgentLoop` |
| `~/.nanobot/aegis/policies/` | Policy YAML files |
| `~/.nanobot/aegis/tag_rules.yaml` | Tool output classification rules |

---

## Notes

- Integration is fully opt-in via `aegis.enabled: true` in `~/.nanobot/config.json`. Zero overhead when disabled.
- Aegis uses **deny-wins** semantics: any DENY blocks the tool call regardless of other policies.
- Tags accumulate monotonically within a session — once sensitive data is accessed, downstream tools are gated for the rest of the session.
- Policy evaluation errors default to ABSTAIN (allow) so a broken policy never brings down the agent.
