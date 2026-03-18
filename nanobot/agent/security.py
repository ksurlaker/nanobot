"""Aegis policy enforcement for nanobot tool calls."""
from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from nanobot.config.schema import AegisConfig


class AegisEnforcer:
    """Wraps AegisClient for nanobot's session/tool lifecycle."""

    def __init__(self, config: AegisConfig):
        import os

        from aegis import AegisClient
        from aegis.config import AegisConfig as _AegisConfig

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
        """Get existing aegis session for a nanobot session key, or start a new one."""
        if session_key not in self._sessions:
            sid = self.client.start_session(
                declared_purpose=self.default_purpose or session_key,
            )
            self._sessions[session_key] = sid
            logger.debug("Aegis session started for {}: {}", session_key, sid)
        return self._sessions[session_key]

    def end_session(self, session_key: str) -> None:
        """End the aegis session associated with a nanobot session key."""
        if sid := self._sessions.pop(session_key, None):
            self.client.end_session(sid)
            logger.debug("Aegis session ended for {}", session_key)

    async def check_pre_call(self, session_key: str, tool_name: str, args: dict) -> str | None:
        """Check policy before a tool executes. Returns a denial message if blocked, else None."""
        from aegis.core.models import Verdict

        try:
            sid = self.get_or_create_session(session_key)
            result = self.client.check_pre_call(sid, tool_name, args)
            if result.final_verdict == Verdict.DENY:
                reason = result.blocking_reason or "Policy violation"
                logger.warning("Aegis blocked pre-call for {}: {}", tool_name, reason)
                return f"Error: Tool call denied by security policy — {reason}"
        except Exception as e:
            logger.error("Aegis pre-call check failed (allowing): {}", e)
        return None

    async def record_and_check_post(
        self, session_key: str, tool_name: str, args: dict, result: str
    ) -> str:
        """Record tool result tags and run post-execution check. Returns (possibly redacted) result."""
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
