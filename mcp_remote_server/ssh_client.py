from __future__ import annotations

import base64
import json
import logging
import re
import shlex
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import paramiko

from .config import SSHConfig

logger = logging.getLogger(__name__)

_DENYLIST_PATTERNS = [
    r"\brm\s+-rf\s+/\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bpoweroff\b",
    r":\s*\(\)\s*\{\s*:\|:\s*&\s*\};\s*:",
    r"\bchmod\s+-R\s+777\s+/\b",
    r"\bchown\s+-R\s+/\b",
]

_BLOCKED = [re.compile(p, re.IGNORECASE) for p in _DENYLIST_PATTERNS]


@dataclass(slots=True)
class CommandResult:
    ok: bool
    status: str
    timeout: bool
    timeout_seconds: int
    stdout: str
    stderr: str
    returncode: int | None
    cmd: str | None = None
    script_summary: str | None = None
    stdout_partial: str | None = None
    stderr_partial: str | None = None
    next_actions: list[str] | None = None
    message_for_user: str | None = None

    def to_dict(self) -> dict:
        payload = {
            "ok": self.ok,
            "status": self.status,
            "timeout": self.timeout,
            "timeout_seconds": self.timeout_seconds,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        if self.cmd is not None:
            payload["cmd"] = self.cmd
        if self.script_summary is not None:
            payload["script_summary"] = self.script_summary
        if self.stdout_partial is not None:
            payload["stdout_partial"] = self.stdout_partial
        if self.stderr_partial is not None:
            payload["stderr_partial"] = self.stderr_partial
        if self.next_actions is not None:
            payload["next_actions"] = self.next_actions
        if self.message_for_user is not None:
            payload["message_for_user"] = self.message_for_user
        return payload


class PersistentSSH:
    def __init__(self, config: SSHConfig, audit_log_path: str = "mcp_ssh_audit.log") -> None:
        self.config = config
        self.audit_log_path = Path(audit_log_path)
        self._client: paramiko.SSHClient | None = None
        self._transport: paramiko.Transport | None = None
        self._lock = threading.Lock()
        self._connect_count = 0

    def connect(self) -> None:
        if self._transport and self._transport.is_active():
            return

        self.close()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.config.host,
            port=self.config.port,
            username=self.config.username,
            key_filename=self.config.key_filename,
            password=self.config.password,
            passphrase=self.config.passphrase,
            timeout=self.config.connect_timeout,
            auth_timeout=self.config.auth_timeout,
            banner_timeout=self.config.banner_timeout,
        )
        transport = client.get_transport()
        if transport is None:
            client.close()
            raise RuntimeError("SSH transport is unavailable after connect")

        transport.set_keepalive(self.config.keepalive_interval)
        self._client = client
        self._transport = transport
        self._connect_count += 1
        logger.info("SSH connected, connect_count=%d", self._connect_count)

    def ensure_connected(self) -> None:
        if self._transport is None or not self._transport.is_active():
            self.connect()

    def close(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                logger.exception("Error closing transport")
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                logger.exception("Error closing SSH client")
        self._transport = None
        self._client = None

    def run_command(self, cmd: str, timeout: int = 30) -> dict:
        with self._lock:
            if self._is_denied(cmd):
                result = CommandResult(
                    ok=False,
                    status="denied",
                    timeout=False,
                    timeout_seconds=timeout,
                    stdout="",
                    stderr="Command blocked by safety denylist.",
                    returncode=None,
                    cmd=cmd,
                )
                self._log_audit("ssh_exec", cmd, result)
                return result.to_dict()

            try:
                self.ensure_connected()
                result = self._exec_channel(cmd=cmd, timeout=timeout, tool_name="ssh_exec")
                return result.to_dict()
            except Exception as exc:
                logger.exception("run_command failed")
                self.close()
                result = CommandResult(
                    ok=False,
                    status="error",
                    timeout=False,
                    timeout_seconds=timeout,
                    stdout="",
                    stderr=f"SSH command error: {exc}",
                    returncode=None,
                    cmd=cmd,
                )
                self._log_audit("ssh_exec", cmd, result)
                return result.to_dict()

    def run_script(self, script: str, timeout: int = 300) -> dict:
        with self._lock:
            if self._is_denied(script):
                result = CommandResult(
                    ok=False,
                    status="denied",
                    timeout=False,
                    timeout_seconds=timeout,
                    stdout="",
                    stderr="Script blocked by safety denylist.",
                    returncode=None,
                    script_summary=self._script_summary(script),
                )
                self._log_audit("ssh_exec_script", self._script_summary(script), result)
                return result.to_dict()

            wrapped = f"bash -lc {shlex.quote(script)}"
            try:
                self.ensure_connected()
                result = self._exec_channel(
                    cmd=wrapped,
                    timeout=timeout,
                    tool_name="ssh_exec_script",
                    script_summary=self._script_summary(script),
                )
                return result.to_dict()
            except Exception as exc:
                logger.exception("run_script failed")
                self.close()
                result = CommandResult(
                    ok=False,
                    status="error",
                    timeout=False,
                    timeout_seconds=timeout,
                    stdout="",
                    stderr=f"SSH script error: {exc}",
                    returncode=None,
                    script_summary=self._script_summary(script),
                )
                self._log_audit("ssh_exec_script", self._script_summary(script), result)
                return result.to_dict()

    def _exec_channel(
        self,
        cmd: str,
        timeout: int,
        tool_name: str,
        script_summary: str | None = None,
    ) -> CommandResult:
        transport = self._transport
        if transport is None:
            raise RuntimeError("Transport is not available")

        chan = transport.open_session()
        chan.exec_command(cmd)

        start = time.monotonic()
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []

        try:
            while True:
                while chan.recv_ready():
                    out_chunks.append(chan.recv(65536))
                while chan.recv_stderr_ready():
                    err_chunks.append(chan.recv_stderr(65536))

                elapsed = time.monotonic() - start
                if elapsed > timeout:
                    stdout_partial = b"".join(out_chunks).decode("utf-8", errors="replace")
                    stderr_partial = b"".join(err_chunks).decode("utf-8", errors="replace")
                    chan.close()
                    result = CommandResult(
                        ok=False,
                        status="timeout",
                        timeout=True,
                        timeout_seconds=timeout,
                        stdout="",
                        stderr="",
                        returncode=None,
                        cmd=cmd if script_summary is None else None,
                        script_summary=script_summary,
                        stdout_partial=stdout_partial,
                        stderr_partial=stderr_partial,
                        next_actions=[
                            "retry_with_longer_timeout",
                            "ask_user_for_more_info",
                            "stop",
                        ],
                        message_for_user=(
                            "The remote command timed out before completion. "
                            "Do you want me to rerun it with a longer timeout, "
                            "provide more information before retrying, or stop?"
                        ),
                    )
                    self._log_audit(tool_name, cmd if script_summary is None else script_summary, result)
                    return result

                if chan.exit_status_ready():
                    break

                time.sleep(0.02)

            while chan.recv_ready():
                out_chunks.append(chan.recv(65536))
            while chan.recv_stderr_ready():
                err_chunks.append(chan.recv_stderr(65536))

            rc = chan.recv_exit_status()
            stdout = b"".join(out_chunks).decode("utf-8", errors="replace")
            stderr = b"".join(err_chunks).decode("utf-8", errors="replace")
            ok = rc == 0
            result = CommandResult(
                ok=ok,
                status="ok" if ok else "failed",
                timeout=False,
                timeout_seconds=timeout,
                stdout=stdout,
                stderr=stderr,
                returncode=rc,
                cmd=cmd if script_summary is None else None,
                script_summary=script_summary,
            )
            self._log_audit(tool_name, cmd if script_summary is None else script_summary, result)
            return result
        finally:
            try:
                chan.close()
            except Exception:
                logger.exception("failed to close channel")

    def _log_audit(self, tool_name: str, cmd_summary: str, result: CommandResult) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "tool": tool_name,
            "summary": cmd_summary[:2000],
            "returncode": result.returncode,
            "timeout": result.timeout,
            "stdout_len": len(result.stdout or result.stdout_partial or ""),
            "stderr_len": len(result.stderr or result.stderr_partial or ""),
            "status": result.status,
        }
        with self.audit_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _script_summary(script: str, max_lines: int = 8) -> str:
        lines = [ln.strip() for ln in script.strip().splitlines() if ln.strip()]
        summary = " ; ".join(lines[:max_lines])
        if len(lines) > max_lines:
            summary += " ; ..."
        return summary[:2000]

    @staticmethod
    def _is_denied(text: str) -> bool:
        lowered = text.lower()
        if re.search(r"\bsudo\b", lowered):
            return True
        return any(rx.search(text) for rx in _BLOCKED)


def encode_script(script: str) -> str:
    """Helper for systems that want safe script transport (not required by server tools)."""
    return base64.b64encode(script.encode("utf-8")).decode("ascii")
