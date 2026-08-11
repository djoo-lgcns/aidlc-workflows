"""Kiro CLI v3 adapter via the Agent Client Protocol (ACP).

Kiro CLI v3's classic non-TUI ``chat --no-interactive`` execution model is not
supported by the v3 agent engine (documented as a Known gap in
https://kiro.dev/docs/cli/v3/).  Trying to drive it that way produces a hung
subprocess: the model finishes its response, the process becomes ``defunct``,
but stdout never sees EOF because the v3 acp-server keeps the pipe open, so a
naive ``for line in process.stdout`` reader blocks forever.

The supported programmatic entry point for v3 is
``kiro-cli acp`` — the Agent Client Protocol.  It speaks JSON-RPC 2.0 over
stdin/stdout and emits an explicit ``TurnEnd`` notification when the agent has
finished a turn.  This adapter uses that protocol:

* One long-lived ``kiro-cli acp`` process per adapter run (multi-turn).
* ``initialize`` + ``session/new`` at start.
* Each evaluator "turn" is a ``session/prompt`` request whose completion is
  the ACP ``TurnEnd`` notification (not the OS process exit).
* Slash commands (e.g. ``/aidlc``) can be sent either as raw text inside a
  prompt or via ``_kiro.dev/commands/execute``; we use the raw-text form so
  the input is byte-identical to what a human user would type into
  ``kiro-cli chat``.

Reference: https://kiro.dev/docs/cli/acp/
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

from cli_harness.adapter import AdapterConfig, AdapterResult, CLIAdapter
from cli_harness.human_analog import generate_human_response

logger = logging.getLogger(__name__)

_KIRO_CLI = "kiro-cli"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b.")

_OPTION_LINE_RE = re.compile(r"^\s*\d+\.\s+\S", re.MULTILINE)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _has_option_list(text: str) -> bool:
    return len(_OPTION_LINE_RE.findall(text)) >= 2


def _log(msg: str) -> None:
    print(f"  [kiro-acp] {msg}", file=sys.stderr, flush=True)


def _find_aidlc_docs(workspace: Path) -> Path | None:
    """Same v2/v1 discovery as :mod:`cli_harness.adapters.kiro_cli`."""
    for name in ("aidlc-docs", "aidlc"):
        direct = workspace / name
        if direct.is_dir():
            return direct
    for child in sorted(workspace.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            for name in ("aidlc-docs", "aidlc"):
                candidate = child / name
                if candidate.is_dir():
                    return candidate
    return None


def _check_intent_state_complete(aidlc_docs_dir: Path | None) -> bool:
    """Same v2-completion heuristic as kiro_cli.py."""
    if aidlc_docs_dir is None:
        return False
    for state_file in aidlc_docs_dir.rglob("*state.md"):
        try:
            content = state_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("status:") and "complete" in stripped.lower():
                return True
    return False


class ACPProtocolError(RuntimeError):
    """Raised for JSON-RPC transport or protocol violations."""


class KiroACPClient:
    """Minimal ACP JSON-RPC 2.0 client for ``kiro-cli acp``.

    The client owns a single ``kiro-cli acp`` subprocess.  It exposes request/
    response semantics for ``initialize``, ``session/new``, and
    ``session/prompt`` and a notification stream for the incremental agent
    updates that flow during a prompt (AgentMessageChunk, ToolCall,
    ToolCallUpdate, TurnEnd).

    Threading model
    ---------------
    A dedicated reader thread consumes stdout and dispatches each JSON message
    to either a pending-request future (matched by ``id``) or a callback that
    the caller registers for notifications.  This lets one call site issue a
    request and, in parallel, collect the notification stream that fires
    before the response arrives — which is the ACP norm.
    """

    def __init__(
        self,
        cwd: Path,
        *,
        agent: str | None = None,
        log_file: Path | None = None,
        env_extra: dict | None = None,
        stderr_log: Path | None = None,
    ) -> None:
        self._cwd = Path(cwd)
        self._agent = agent
        self._log_file = log_file
        self._stderr_log = stderr_log
        self._env_extra = env_extra or {}
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending: dict[int, Queue] = {}
        self._pending_lock = threading.Lock()
        self._notification_handler: Callable[[dict], None] | None = None
        # ACP is bidirectional JSON-RPC.  The agent can send *requests* to us
        # too (e.g. session/request_permission, terminal/create, terminal/wait_for_exit).
        # We look up a handler by method name; anything without a handler is
        # answered with JSON-RPC error -32601 so the agent doesn't hang.
        self._request_handlers: dict[str, Callable[[dict], Any]] = {}
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # subprocess lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        binary = shutil.which(_KIRO_CLI)
        if binary is None:
            raise ACPProtocolError(f"{_KIRO_CLI!r} not found on PATH")

        cmd = [binary, "acp"]
        if self._agent:
            cmd += ["--agent", self._agent]

        env = os.environ.copy()
        env.update(self._env_extra)

        _log(f"spawn: {' '.join(cmd)} (cwd={self._cwd})")

        # nosec B603 - Executing user's Kiro CLI with validated configuration
        # nosemgrep: dangerous-subprocess-use-audit
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self._cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        assert self._proc.stdout is not None
        assert self._proc.stdin is not None

        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="kiro-acp-reader", daemon=True
        )
        self._reader_thread.start()

        if self._stderr_log is not None:
            self._stderr_thread = threading.Thread(
                target=self._stderr_loop, name="kiro-acp-stderr", daemon=True
            )
            self._stderr_thread.start()

    def stop(self, *, kill_after: float = 3.0) -> int | None:
        if self._proc is None:
            return None
        self._stopped = True
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            return self._proc.wait(timeout=kill_after)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                return self._proc.wait(timeout=kill_after)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                return self._proc.wait(timeout=kill_after)

    # ------------------------------------------------------------------
    # stdout reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            if not line.strip():
                continue
            if self._log_file is not None:
                try:
                    with open(self._log_file, "a", encoding="utf-8") as fh:
                        fh.write(line)
                except OSError:
                    pass
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("acp stdout non-JSON line: %s", line[:200])
                continue
            self._dispatch(msg)

    def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            with open(self._stderr_log, "a", encoding="utf-8") as fh:  # type: ignore[arg-type]
                for line in self._proc.stderr:
                    fh.write(line)
        except OSError:
            pass

    def _dispatch(self, msg: dict) -> None:
        # response: has "id" and either "result" or "error", NO "method"
        if "id" in msg and "method" not in msg and ("result" in msg or "error" in msg):
            with self._pending_lock:
                q = self._pending.pop(msg["id"], None)
            if q is not None:
                q.put(msg)
            else:
                logger.debug("acp response for unknown id: %s", msg.get("id"))
            return

        # AGENT → CLIENT REQUEST: has both "id" and "method".
        # ACP is bidirectional JSON-RPC; the agent uses this to ask us to
        # execute shell commands (terminal/create, terminal/wait_for_exit, …)
        # and to request tool-use permission (session/request_permission).
        # Answering these is *mandatory* — the agent will block waiting for
        # our response, which is exactly what caused the original 45 s hang.
        if "id" in msg and "method" in msg:
            method = msg["method"]
            handler = self._request_handlers.get(method)
            try:
                if handler is None:
                    # Report method-not-found rather than silently ignoring;
                    # otherwise the agent hangs on the missing response.
                    logger.warning("acp: no handler for agent request %s", method)
                    self._send({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {
                            "code": -32601,
                            "message": f"Method not found: {method}",
                        },
                    })
                else:
                    result = handler(msg.get("params", {}) or {})
                    self._send({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": result,
                    })
            except Exception as exc:  # pragma: no cover - handler errors surface here
                logger.exception("acp: handler for %s raised", method)
                try:
                    self._send({
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {
                            "code": -32000,
                            "message": f"Handler for {method} raised: {exc}",
                        },
                    })
                except Exception:
                    pass
            return

        # NOTIFICATION: has "method" but no "id".
        if self._notification_handler is not None:
            try:
                self._notification_handler(msg)
            except Exception:  # pragma: no cover - handler errors must not kill reader
                logger.exception("notification handler raised")
        else:
            logger.debug("acp notification without handler: %s", msg.get("method"))

    # ------------------------------------------------------------------
    # request / notification / response
    # ------------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        data = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ACPProtocolError(f"kiro-cli acp stdin closed: {exc}") from exc

    def request(self, method: str, params: dict, *, timeout: float = 120.0) -> Any:
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
        q: Queue = Queue()
        with self._pending_lock:
            self._pending[req_id] = q
        self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        })
        try:
            resp = q.get(timeout=timeout)
        except Empty as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise ACPProtocolError(
                f"acp request {method} (id={req_id}) timed out after {timeout}s"
            ) from exc
        if "error" in resp:
            raise ACPProtocolError(
                f"acp request {method} failed: {resp['error']!r}"
            )
        return resp.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def set_notification_handler(self, handler: Callable[[dict], None]) -> None:
        self._notification_handler = handler

    def register_request_handler(
        self, method: str, handler: Callable[[dict], Any]
    ) -> None:
        """Register a handler for an agent→client JSON-RPC request.

        The handler receives the incoming ``params`` dict and must return a
        JSON-serialisable ``result`` (or raise, in which case a JSON-RPC
        error is sent back automatically).
        """
        self._request_handlers[method] = handler

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


# ---------------------------------------------------------------------------
# Default agent→client request handlers (permission + terminal)
# ---------------------------------------------------------------------------


def _select_allow_option(options: list[dict]) -> dict | None:
    """Pick an "allow"-flavoured option from a session/request_permission list.

    Preference order:
      1. ``kind == "allow_always"`` — sticky auto-allow, best for a headless run.
      2. ``kind == "allow_once"``   — permit this call only.
      3. Fallback: first option whose id/kind contains "allow".
    """
    if not isinstance(options, list):
        return None
    kinds = {"allow_always": 0, "allow_once": 1}
    ranked: list[tuple[int, dict]] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        kind = str(opt.get("kind") or "")
        if kind in kinds:
            ranked.append((kinds[kind], opt))
    if ranked:
        ranked.sort(key=lambda pair: pair[0])
        return ranked[0][1]
    for opt in options:
        if not isinstance(opt, dict):
            continue
        oid = str(opt.get("optionId") or "")
        kind = str(opt.get("kind") or "")
        if "allow" in oid.lower() or "allow" in kind.lower():
            return opt
    return None


class _TerminalManager:
    """Runs terminal/* requests on behalf of the ACP client.

    ACP's Terminal protocol lets the agent ask the client to execute shell
    commands: ``terminal/create`` starts a subprocess and returns a
    ``terminalId``; ``terminal/output`` / ``terminal/wait_for_exit`` /
    ``terminal/kill`` / ``terminal/release`` follow.  See
    https://agentclientprotocol.com/protocol/v1/terminals.

    We keep a per-adapter registry of live subprocesses keyed by terminalId
    and stream their stdout so ``terminal/output`` can return a snapshot at
    any time.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._counter = 0
        self._lock = threading.Lock()
        self._terminals: dict[str, dict[str, Any]] = {}

    def _new_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"term-{self._counter}"

    def create(self, params: dict) -> dict:
        command = params.get("command", "")
        args = params.get("args") or []
        cwd = params.get("cwd") or str(self._workspace)
        env_extra = params.get("env") if isinstance(params.get("env"), dict) else {}

        argv: list[str]
        if isinstance(command, list):
            argv = [str(x) for x in command] + [str(a) for a in args]
        else:
            argv = [str(command)] + [str(a) for a in args]

        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (env_extra or {}).items()})

        _log(f"terminal/create: {argv} cwd={cwd}")
        # nosec B603 - agent-driven shell execution inside its own workspace
        # nosemgrep: dangerous-subprocess-use-audit
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        term_id = self._new_id()
        buf_lock = threading.Lock()
        output_buf: list[str] = []

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                with buf_lock:
                    output_buf.append(line)

        threading.Thread(target=_reader, name=f"term-{term_id}", daemon=True).start()

        with self._lock:
            self._terminals[term_id] = {
                "proc": proc,
                "buf_lock": buf_lock,
                "output_buf": output_buf,
                "argv": argv,
                "cwd": cwd,
            }
        return {"terminalId": term_id}

    def output(self, params: dict) -> dict:
        term_id = params.get("terminalId")
        with self._lock:
            info = self._terminals.get(term_id)
        if info is None:
            raise KeyError(f"unknown terminalId: {term_id!r}")
        proc = info["proc"]
        with info["buf_lock"]:
            text = "".join(info["output_buf"])
        rc = proc.poll()
        result: dict[str, Any] = {"output": text, "truncated": False}
        if rc is not None:
            result["exitStatus"] = {"exitCode": rc, "signal": None}
        return result

    def wait_for_exit(self, params: dict) -> dict:
        term_id = params.get("terminalId")
        with self._lock:
            info = self._terminals.get(term_id)
        if info is None:
            raise KeyError(f"unknown terminalId: {term_id!r}")
        proc = info["proc"]
        rc = proc.wait()
        return {"exitCode": rc, "signal": None}

    def kill(self, params: dict) -> dict:
        term_id = params.get("terminalId")
        with self._lock:
            info = self._terminals.get(term_id)
        if info is None:
            return {}
        proc = info["proc"]
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # pragma: no cover
            pass
        return {}

    def release(self, params: dict) -> dict:
        term_id = params.get("terminalId")
        with self._lock:
            info = self._terminals.pop(term_id, None)
        if info is not None:
            proc = info["proc"]
            if proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except Exception:
                    pass
        return {}


def _register_default_request_handlers(
    client: KiroACPClient, workspace: Path
) -> _TerminalManager:
    """Wire up the ACP agent→client request handlers this adapter needs.

    Returns the :class:`_TerminalManager` in case the caller wants to inspect
    live terminals for diagnostics.
    """
    tm = _TerminalManager(workspace)

    def _permission(params: dict) -> dict:
        options = params.get("options") or []
        chosen = _select_allow_option(options)
        if chosen is None:
            # No allow-flavoured option?  Reject explicitly so the agent
            # gets a definite answer.
            reject = next(
                (
                    o
                    for o in options
                    if isinstance(o, dict) and "reject" in str(o.get("kind", "")).lower()
                ),
                None,
            )
            option_id = (reject or {}).get("optionId") or ""
            _log(
                "session/request_permission: no allow option; rejecting "
                f"(optionId={option_id!r})"
            )
            return {"outcome": {"outcome": "selected", "optionId": option_id}}
        title = (params.get("toolCall") or {}).get("title")
        _log(
            f"session/request_permission: auto-selecting {chosen.get('optionId')!r} "
            f"(kind={chosen.get('kind')!r}) for {title!r}"
        )
        # ACP result shape:
        #   { "outcome": { "outcome": "selected", "optionId": "<id>" } }
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}

    client.register_request_handler("session/request_permission", _permission)
    client.register_request_handler("terminal/create", tm.create)
    client.register_request_handler("terminal/output", tm.output)
    client.register_request_handler("terminal/wait_for_exit", tm.wait_for_exit)
    client.register_request_handler("terminal/kill", tm.kill)
    client.register_request_handler("terminal/release", tm.release)

    # ACP file-system requests (fs/read_text_file, fs/write_text_file).
    # We advertised support for these in clientCapabilities.fs, so we must
    # implement them if the agent calls them.
    def _fs_read(params: dict) -> dict:
        path = params.get("path")
        if not path:
            raise ValueError("fs/read_text_file requires 'path'")
        line = params.get("line")
        limit = params.get("limit")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if isinstance(line, int) or isinstance(limit, int):
            lines = text.splitlines(keepends=True)
            start = int(line) - 1 if isinstance(line, int) and line > 0 else 0
            end = start + int(limit) if isinstance(limit, int) else None
            text = "".join(lines[start:end])
        return {"content": text}

    def _fs_write(params: dict) -> dict:
        path = params.get("path")
        content = params.get("content", "")
        if not path:
            raise ValueError("fs/write_text_file requires 'path'")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return {}

    client.register_request_handler("fs/read_text_file", _fs_read)
    client.register_request_handler("fs/write_text_file", _fs_write)

    return tm


# ---------------------------------------------------------------------------
# Turn loop helper — collects notifications until TurnEnd
# ---------------------------------------------------------------------------


def _extract_text_from_content(content: Any) -> str:
    """Best-effort text extraction from a session-update content block."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "".join(parts)
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("text"), str):
            return content["text"]
    return ""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class KiroACPAdapter(CLIAdapter):
    """Drive an AIDLC workflow through ``kiro-cli acp``.

    The adapter mirrors the surface of :class:`KiroCLIAdapter` but uses ACP
    instead of ``chat --no-interactive`` so it works with the v3 agent engine.
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    @property
    def name(self) -> str:
        return "kiro-cli"

    def check_prerequisites(self) -> tuple[bool, str]:
        if not shutil.which(_KIRO_CLI):
            return False, f"'{_KIRO_CLI}' not found in PATH."
        return True, f"Kiro CLI ('{_KIRO_CLI}') found"

    def run(self, config: AdapterConfig) -> AdapterResult:  # noqa: C901 - workflow loop is intentionally verbose
        ok, msg = self.check_prerequisites()
        if not ok:
            return AdapterResult(success=False, output_dir=config.output_dir, error=msg)

        start_time = time.monotonic()

        config.output_dir.mkdir(parents=True, exist_ok=True)
        workspace = config.output_dir / "workspace"
        workspace.mkdir(exist_ok=True)
        _log(f"Workspace: {workspace}")

        try:
            shutil.copy2(config.vision_path, workspace / "vision.md")
            _log(f"Copied vision: {config.vision_path}")
            if config.tech_env_path and config.tech_env_path.is_file():
                shutil.copy2(config.tech_env_path, workspace / "tech-env.md")
                _log(f"Copied tech-env: {config.tech_env_path}")

            is_v2 = (
                config.kiro_dist_path is not None
                and config.kiro_dist_path.is_dir()
            )
            if not is_v2:
                return AdapterResult(
                    success=False,
                    output_dir=config.output_dir,
                    error=(
                        "KiroACPAdapter requires kiro_dist_path (v2 layout). "
                        "For v1 legacy rules use KiroCLIAdapter."
                    ),
                )

            # Install workspace .kiro/ distribution — same shape as kiro_cli.py.
            kiro_dest = workspace / ".kiro"
            if kiro_dest.exists():
                shutil.rmtree(kiro_dest)
            shutil.copytree(config.kiro_dist_path, kiro_dest)
            _log(f"Installed .kiro/ distribution from {config.kiro_dist_path}")

            # Same agent-JSON permissions patch as the v2 adapter, for
            # consistency; the ACP path still routes through Kiro's permission
            # system for tool calls.
            aidlc_agent = kiro_dest / "agents" / "aidlc.json"
            if aidlc_agent.is_file():
                try:
                    agent_data = json.loads(aidlc_agent.read_text(encoding="utf-8"))
                    agent_data.setdefault("permissions", {})["rules"] = [
                        {"capability": "all", "effect": "allow"}
                    ]
                    aidlc_agent.write_text(
                        json.dumps(agent_data, indent=2) + "\n", encoding="utf-8"
                    )
                    _log(
                        "Patched workspace agents/aidlc.json: permissions.rules "
                        "= [{capability: all, effect: allow}]"
                    )
                except Exception as _agent_exc:  # pragma: no cover - non-fatal
                    _log(f"[warn] failed to patch agents/aidlc.json: {_agent_exc}")

            # Verify aidlc skill + agent are installed.
            if not (kiro_dest / "skills" / "aidlc" / "SKILL.md").is_file():
                _log("[warn] installed .kiro/ has no top-level 'aidlc' skill")
            has_aidlc_agent = (kiro_dest / "agents" / "aidlc.json").is_file()

            # Provenance manifest — same as v2 adapter.
            try:
                import hashlib
                manifest_lines = [
                    f"source: {config.kiro_dist_path}",
                    "",
                    "# sha256  path (relative to installed .kiro/)",
                ]
                for f in sorted(kiro_dest.rglob("*")):
                    if f.is_file():
                        manifest_lines.append(
                            f"{hashlib.sha256(f.read_bytes()).hexdigest()}  "
                            f"{f.relative_to(kiro_dest)}"
                        )
                (config.output_dir / "kiro-dist-manifest.txt").write_text(
                    "\n".join(manifest_lines) + "\n", encoding="utf-8"
                )
                _log("Wrote dist provenance manifest")
            except Exception as _prov_exc:  # pragma: no cover - non-fatal
                _log(f"[warn] provenance manifest failed: {_prov_exc}")

            _log("Using ACP execution (kiro-cli acp)")
            for env_var in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"):
                if os.environ.get(env_var):
                    _log(f"  env {env_var}={os.environ[env_var]}")

            log_path = config.output_dir / "kiro-session.log"
            stderr_log = config.output_dir / "kiro-acp-stderr.log"

            client = KiroACPClient(
                cwd=workspace,
                agent="aidlc" if has_aidlc_agent else None,
                log_file=log_path,
                stderr_log=stderr_log,
            )
            client.start()

            # ACP is bidirectional: register handlers for the agent→client
            # requests we know Kiro will send during a v3 workflow.
            _register_default_request_handlers(client, workspace)

            try:
                # 1. initialize
                init_res = client.request(
                    "initialize",
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {
                            "fs": {"readTextFile": True, "writeTextFile": True},
                        },
                        "clientInfo": {
                            "name": "aidlc-evaluator",
                            "version": "0.1.0",
                        },
                    },
                    timeout=30.0,
                )
                _log(f"initialize OK: agent={init_res.get('agentInfo', {}).get('name')!r}")

                # 2. session/new  (may interleave with notifications)
                sess_res = client.request(
                    "session/new",
                    {"cwd": str(workspace), "mcpServers": []},
                    timeout=60.0,
                )
                session_id = sess_res["sessionId"]
                _log(f"session/new OK: sessionId={session_id}")

                # 3. Optional: switch mode if config.model is provided (session/set_model)
                if config.model:
                    try:
                        client.request(
                            "session/set_model",
                            {"sessionId": session_id, "modelId": config.model},
                            timeout=30.0,
                        )
                        _log(f"session/set_model: {config.model}")
                    except ACPProtocolError as exc:
                        _log(f"[warn] session/set_model failed (continuing): {exc}")

                # Prompt template: v2 uses "/aidlc\n\n<vision>", which ACP
                # forwards verbatim; the aidlc skill (registered as a slash
                # command in the loaded agent) picks it up.
                vision_content = config.vision_path.read_text(encoding="utf-8")
                initial_prompt = f"/aidlc\n\n{vision_content.strip()}\n"

                # Multi-turn loop.
                turn = 0
                next_prompt = initial_prompt
                turn_output = ""
                agent_docs_dir: Path | None = None
                while True:
                    turn += 1
                    # Timeout for the whole run.
                    elapsed = time.monotonic() - start_time
                    remaining = config.timeout_seconds - elapsed
                    if remaining <= 0:
                        _log(f"Timeout reached before turn {turn}")
                        break

                    _log(f"Turn {turn}: sending prompt ({len(next_prompt)} chars)")
                    turn_output, tool_calls, ended_naturally, stop_reason = self._run_turn(
                        client,
                        session_id,
                        next_prompt,
                        # Hard upper bound per turn is 1 h; the idle watchdog
                        # (default 120 s of silence) inside _run_turn catches
                        # true hangs long before this.  Long values here only
                        # affect legitimately busy sub-agent-crew turns
                        # observed on the v2 baseline.
                        timeout=min(remaining, 3600),
                    )

                    aidlc_docs_dir = _find_aidlc_docs(workspace)
                    file_count = (
                        sum(1 for _ in aidlc_docs_dir.rglob("*") if _.is_file())
                        if aidlc_docs_dir
                        else 0
                    )
                    state_complete = _check_intent_state_complete(aidlc_docs_dir)
                    _log(
                        f"Turn {turn} done: chars={len(turn_output)} "
                        f"tools={len(tool_calls)} aidlc-docs={file_count} "
                        f"state={'complete' if state_complete else 'in-progress'} "
                        f"stopReason={stop_reason!r} ended={ended_naturally}"
                    )
                    agent_docs_dir = aidlc_docs_dir

                    # Completion checks
                    if state_complete:
                        _log("Workflow marked complete via intent-state — stopping")
                        break
                    if not ended_naturally:
                        _log("session/prompt error/timeout — stopping")
                        break
                    if stop_reason in ("refusal", "cancelled", "max_tokens"):
                        _log(f"Non-normal stopReason={stop_reason!r} — stopping")
                        break

                    # Human analog / option-list default for the next turn.
                    if _has_option_list(turn_output):
                        # Default forward-progression is option 1.
                        next_prompt = "1"
                        _log("Detected option list — replying with '1'")
                    else:
                        try:
                            next_prompt = generate_human_response(
                                turn_output=turn_output,
                                vision_path=config.vision_path,
                                tech_env_path=config.tech_env_path,
                                aws_profile=config.aws_profile,
                                aws_region=config.aws_region,
                                model_id=config.scorer_model,
                            )
                        except Exception as exc:
                            _log(f"[warn] human_analog failed: {exc}")
                            next_prompt = "Approve & Continue."
                        _log(f"human_analog: {next_prompt!r}")

                    # Guard against runaway turns.
                    if turn >= 60:
                        _log("Reached turn cap (60); stopping")
                        break

            finally:
                _log("terminating kiro-cli acp process")
                rc = client.stop(kill_after=5.0)
                _log(f"kiro-cli acp exit={rc}")

            elapsed = time.monotonic() - start_time
            aidlc_docs_dir = _find_aidlc_docs(workspace)
            file_count = (
                sum(1 for _ in aidlc_docs_dir.rglob("*") if _.is_file())
                if aidlc_docs_dir
                else 0
            )
            if file_count == 0:
                return AdapterResult(
                    success=False,
                    output_dir=config.output_dir,
                    workspace_dir=workspace,
                    error=f"kiro-cli acp completed {turn} turn(s), no aidlc(-docs)/ output produced.",
                    elapsed_seconds=elapsed,
                )

            # Best-effort: relocate discovered aidlc(-docs) to the output root
            # under the canonical name so the downstream pipeline sees it.
            if aidlc_docs_dir is not None and aidlc_docs_dir.name != "aidlc-docs":
                dest = config.output_dir / "aidlc-docs"
                if not dest.exists():
                    try:
                        shutil.copytree(aidlc_docs_dir, dest)
                        _log(f"Copied {aidlc_docs_dir} → {dest} (canonical)")
                    except Exception as _copy_exc:  # pragma: no cover - non-fatal
                        _log(f"[warn] failed to copy aidlc-docs canonical: {_copy_exc}")
            elif aidlc_docs_dir is not None:
                dest = aidlc_docs_dir

            return AdapterResult(
                success=True,
                output_dir=config.output_dir,
                workspace_dir=workspace,
                aidlc_docs_dir=config.output_dir / "aidlc-docs" if (config.output_dir / "aidlc-docs").is_dir() else aidlc_docs_dir,
                elapsed_seconds=elapsed,
            )
        except Exception as exc:
            logger.exception("kiro-acp adapter run failed")
            return AdapterResult(
                success=False,
                output_dir=config.output_dir,
                error=f"kiro-acp adapter error: {exc}",
                elapsed_seconds=time.monotonic() - start_time,
            )

    # ------------------------------------------------------------------
    # per-turn execution
    # ------------------------------------------------------------------

    def _run_turn(
        self,
        client: KiroACPClient,
        session_id: str,
        prompt_text: str,
        *,
        timeout: float,
        idle_timeout: float = 120.0,
    ) -> tuple[str, list[dict], bool, str | None]:
        """Send ``prompt_text`` as a session/prompt and collect the turn.

        The ACP contract observed in kiro-cli 2.16.2:

          * ``session/prompt`` is a *blocking request* whose ``result`` doubles
            as the turn-boundary signal — the result carries a ``stopReason``
            (``end_turn``, ``refusal``, ``cancelled``, ``max_tokens``, …).
          * While the request is in flight, ``session/notification`` messages
            stream in with ``sessionUpdate`` of ``agent_message_chunk`` /
            ``tool_call`` / ``tool_call_update`` etc.  No separate ``TurnEnd``
            notification is emitted; the request result is the boundary.

        Two-part timeout policy (defensive against sub-agent crew workloads):

          * ``timeout`` — hard upper bound for the whole turn.  A long-running
            v2 upstream stage (e.g. approval-handoff / practices-discovery
            spawning a subagent crew) may legitimately need >15 minutes;
            callers pass a large value (e.g. 3600 s).
          * ``idle_timeout`` — cap on *silence*.  If we go this long with
            zero incoming notifications (no chunks, no tool events, no
            metadata), the agent is likely wedged rather than merely slow.
            A background watchdog trips the pending request with a synthetic
            error so we don't block for the full ``timeout``.

        Returns ``(agent_text, tool_events, ended_ok, stop_reason)``.
        """
        chunks: list[str] = []
        tool_events: list[dict] = []
        last_activity = [time.monotonic()]

        def handler(msg: dict) -> None:
            last_activity[0] = time.monotonic()
            method = msg.get("method", "")
            params = msg.get("params", {}) or {}
            update = params.get("update", {}) if isinstance(params, dict) else {}
            update_type = (
                update.get("sessionUpdate") if isinstance(update, dict) else None
            )

            if update_type == "agent_message_chunk":
                content = update.get("content") if isinstance(update, dict) else None
                text = _extract_text_from_content(content)
                if text:
                    chunks.append(text)
                return

            if update_type in ("tool_call", "tool_call_update"):
                tool_events.append({"kind": update_type, "raw": update})
                return

            # Kiro-specific extensions (commands/available, metadata, mcp/*, etc.)
            if method.startswith("_kiro.dev/"):
                return

            logger.debug("unhandled acp notification method=%s update=%s", method, update_type)

        client.set_notification_handler(handler)

        # Watchdog: unblocks the pending session/prompt with a synthetic
        # JSON-RPC error if no notification arrives for ``idle_timeout``
        # seconds.  This distinguishes a genuinely wedged agent from a slow
        # but active one — the latter keeps last_activity fresh through
        # streaming ``agent_thought_chunk`` events.
        watchdog_stop = threading.Event()
        watchdog_fired = [False]

        def _watchdog() -> None:
            while not watchdog_stop.wait(1.0):
                idle = time.monotonic() - last_activity[0]
                if idle <= idle_timeout:
                    continue
                # Trip pending requests for this client so the main-thread
                # request() call unblocks immediately.
                with client._pending_lock:  # type: ignore[attr-defined]
                    for req_id, q in list(client._pending.items()):  # type: ignore[attr-defined]
                        q.put({
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": -32001,
                                "message": (
                                    f"idle timeout {idle_timeout:.0f}s "
                                    f"(actual idle {idle:.0f}s)"
                                ),
                            },
                        })
                        client._pending.pop(req_id, None)  # type: ignore[attr-defined]
                watchdog_fired[0] = True
                _log(
                    f"[warn] session/prompt idle for {idle:.0f}s (> {idle_timeout:.0f}s) "
                    "— treating as hang"
                )
                return

        watchdog_thread = threading.Thread(
            target=_watchdog, name="kiro-acp-idle-watchdog", daemon=True,
        )
        watchdog_thread.start()

        stop_reason: str | None = None
        ended = False
        try:
            result = client.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt_text}],
                },
                timeout=timeout,
            )
            if isinstance(result, dict):
                stop_reason = result.get("stopReason")
            ended = True
        except ACPProtocolError as exc:
            _log(f"[warn] session/prompt error: {exc}")
            ended = False
        finally:
            watchdog_stop.set()

        client.set_notification_handler(None)
        text = "".join(chunks)
        # Surface idle-kill in stop_reason so the caller can distinguish it
        # from a hard max-timeout.
        if watchdog_fired[0] and stop_reason is None:
            stop_reason = "idle_timeout"
        return _strip_ansi(text), tool_events, ended, stop_reason
