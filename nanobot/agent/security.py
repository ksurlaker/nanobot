"""Aegis policy enforcement for nanobot tool calls."""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from nanobot.config.schema import AegisConfig


def init_aegis_defaults(policy_directory: str, tag_rules_file: str | None) -> None:
    """Seed bundled default policies and tag rules into the aegis config dirs if absent."""
    from importlib.resources import files as pkg_files

    policy_dir = Path(policy_directory)
    policy_dir.mkdir(parents=True, exist_ok=True)

    try:
        bundled_policies = pkg_files("nanobot") / "aegis" / "policies"
        for item in bundled_policies.iterdir():
            if item.name.endswith(".yaml"):
                dest = policy_dir / item.name
                if not dest.exists():
                    dest.write_text(item.read_text(encoding="utf-8"), encoding="utf-8")
                    logger.debug("Aegis: seeded default policy {}", item.name)
    except Exception as e:
        logger.debug("Aegis: could not seed default policies: {}", e)

    if tag_rules_file:
        rules_path = Path(tag_rules_file)
        if not rules_path.exists():
            rules_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                bundled_rules = pkg_files("nanobot") / "aegis" / "tag_rules.yaml"
                rules_path.write_text(bundled_rules.read_text(encoding="utf-8"), encoding="utf-8")
                logger.debug("Aegis: seeded default tag rules to {}", rules_path)
            except Exception as e:
                logger.debug("Aegis: could not seed default tag rules: {}", e)


class AegisEnforcer:
    """Wraps AegisClient for nanobot's session/tool lifecycle."""

    def __init__(self, config: AegisConfig):
        from aegis import AegisClient
        from aegis.config import AegisConfig as _AegisConfig, AuditConfig

        policy_dir = os.path.expanduser(config.policy_directory)
        # Default tag_rules_file to ~/.nanobot/aegis/tag_rules.yaml if not set
        tag_rules = os.path.expanduser(
            config.tag_rules_file or str(Path(policy_dir).parent / "tag_rules.yaml")
        )

        init_aegis_defaults(policy_dir, tag_rules)

        audit_log = os.path.expanduser(config.audit_log) if config.audit_log else None
        aegis_cfg = _AegisConfig(
            policy_directory=policy_dir,
            tag_rules_file=tag_rules,
            memory_store_type="file",
            memory_directory=os.path.expanduser(config.memory_directory),
            audit=AuditConfig(enabled=bool(audit_log), log_file=audit_log),
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

            # Persist tags resolved at post-execution (from static rules + result patterns)
            # into the session's accumulated_tags so future pre-call checks can see them.
            resolved_tags = list(post.context_snapshot.get("current_tags", []))
            if resolved_tags:
                self.client.record_result(sid, tool_name, result, result_tags=resolved_tags)

            if post.final_verdict == Verdict.DENY:
                reason = post.blocking_reason or "Policy violation"
                logger.warning("Aegis blocked post-exec for {}: {}", tool_name, reason)
                return f"[Result redacted by security policy: {reason}]"
        except Exception as e:
            logger.error("Aegis post-exec check failed (passing through): {}", e)
        return result
