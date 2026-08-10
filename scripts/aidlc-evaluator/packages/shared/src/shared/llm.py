"""Backend-neutral LLM invocation shim for evaluator components.

Callers pass a system prompt and user message; the shim routes the request to
either Amazon Bedrock (default, via boto3) or the Codex CLI (via subprocess).
The Codex CLI path uses the user's logged-in Codex account so it does NOT
require valid AWS credentials — this is useful for A/B evaluation runs when
Bedrock is unavailable but ``codex`` is authenticated.

Backend selection priority (first non-empty wins):

  1. ``backend`` argument passed to :func:`invoke_llm` / :func:`resolve_backend`
  2. component-specific env var, e.g. ``AIDLC_EVAL_HUMAN_BACKEND``,
     ``AIDLC_EVAL_SCORER_BACKEND`` (component name is normalised: uppercase,
     ``-``/``.`` → ``_``)
  3. global env var ``AIDLC_EVAL_LLM_BACKEND``
  4. default ``"bedrock"``

Codex CLI is invoked as ``codex exec --ephemeral --skip-git-repo-check
--sandbox read-only --ignore-user-config --ignore-rules --color never
--output-last-message <tmp>``.  ``--ephemeral`` ensures every call is an
independent session, so parallel invocations do not clobber each other's state
and there is no persistent context leakage between requests.

``codex`` is resolved in this order:
  1. env var ``AIDLC_EVAL_CODEX_BIN`` (absolute path)
  2. ``shutil.which("codex")`` (rare: codex on PATH)
  3. ``$HOME/.nvm/versions/node/v20.*/bin/codex`` (Node 20 nvm install)
  4. ``bash -lc 'source ~/.nvm/nvm.sh && nvm which 20'`` to derive the bin dir

Failures raise :class:`RuntimeError`; the caller decides whether to fall back
to a deterministic response (e.g. heuristic scorer or canned approval).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

Backend = Literal["bedrock", "codex-cli"]

_VALID_BACKENDS: tuple[str, ...] = ("bedrock", "codex-cli")

# Codex CLI writes structured session info to stdout; the actual last assistant
# message is what we want.  ``--output-last-message`` gives us that verbatim,
# so we don't have to parse stdout at all.


@dataclass(frozen=True)
class LlmRequest:
    """A backend-neutral single-turn LLM request.

    Bedrock-only fields are ignored by the codex-cli backend and vice versa;
    supplying them is safe.
    """

    system: str
    user: str
    max_tokens: int = 512
    temperature: float = 0.0
    # codex-cli only
    codex_model: str | None = None
    codex_sandbox: str = "read-only"  # 'read-only' | 'workspace-write' | 'danger-full-access'
    # bedrock only
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    aws_profile: str | None = None
    aws_region: str | None = None
    # both
    timeout_seconds: int = 300


def _normalise_backend(value: str | None) -> str:
    return (value or "").strip().lower()


def resolve_backend(
    *,
    explicit: str | None = None,
    component: str | None = None,
) -> Backend:
    """Resolve which backend to use for a given caller.

    See module docstring for priority order.

    Raises:
        ValueError: if any of the resolved values is set to something other
            than ``"bedrock"`` or ``"codex-cli"``.
    """
    candidates: list[tuple[str, str]] = []
    if explicit is not None:
        candidates.append(("explicit", _normalise_backend(explicit)))
    if component:
        env_var = f"AIDLC_EVAL_{component.upper().replace('-', '_').replace('.', '_')}_BACKEND"
        candidates.append((env_var, _normalise_backend(os.environ.get(env_var))))
    candidates.append((
        "AIDLC_EVAL_LLM_BACKEND",
        _normalise_backend(os.environ.get("AIDLC_EVAL_LLM_BACKEND")),
    ))

    for source, value in candidates:
        if not value:
            continue
        if value not in _VALID_BACKENDS:
            raise ValueError(
                f"Unsupported LLM backend {value!r} from {source}; "
                f"expected one of {_VALID_BACKENDS!r}"
            )
        return value  # type: ignore[return-value]
    return "bedrock"


def is_codex_cli_backend(*, component: str | None = None) -> bool:
    """Return True when the resolved backend for ``component`` is ``codex-cli``."""
    return resolve_backend(component=component) == "codex-cli"


def invoke_llm(
    req: LlmRequest,
    *,
    backend: str | None = None,
    component: str | None = None,
) -> str:
    """Invoke an LLM with the given request; return the assistant text.

    Raises:
        RuntimeError: on any transport, auth or parsing failure.  Callers may
            catch this to fall back to a deterministic response.
    """
    resolved: Backend = resolve_backend(explicit=backend, component=component)
    logger.debug(
        "invoke_llm backend=%s component=%s max_tokens=%d timeout=%ds",
        resolved,
        component,
        req.max_tokens,
        req.timeout_seconds,
    )
    if resolved == "codex-cli":
        return _invoke_codex_cli(req)
    return _invoke_bedrock(req)


# ---------------------------------------------------------------------------
# codex-cli backend
# ---------------------------------------------------------------------------

_CODEX_BIN_CACHE: str | None = None


def _resolve_codex_bin() -> str:
    """Locate the ``codex`` binary once and memoise the result.

    Search order matches the module docstring.  Raises RuntimeError if not
    found; callers should surface this as a preflight failure rather than
    silently degrading.
    """
    global _CODEX_BIN_CACHE
    if _CODEX_BIN_CACHE:
        return _CODEX_BIN_CACHE

    # 1. Explicit env override
    override = os.environ.get("AIDLC_EVAL_CODEX_BIN")
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            _CODEX_BIN_CACHE = str(p)
            return _CODEX_BIN_CACHE
        raise RuntimeError(
            f"AIDLC_EVAL_CODEX_BIN={override!r} does not exist"
        )

    # 2. PATH
    on_path = shutil.which("codex")
    if on_path:
        _CODEX_BIN_CACHE = on_path
        return _CODEX_BIN_CACHE

    # 3. Direct nvm layout glob (fast, no shell)
    home = os.environ.get("HOME", "")
    if home:
        # v20.*: prefer the highest-versioned node 20 install if multiple exist.
        candidates = sorted(
            glob.glob(f"{home}/.nvm/versions/node/v20.*/bin/codex"),
            reverse=True,
        )
        for c in candidates:
            if Path(c).exists():
                _CODEX_BIN_CACHE = c
                return _CODEX_BIN_CACHE

    # 4. Fall back to asking nvm directly
    try:
        # nosec B603 - bash + nvm are trusted local dev tools
        # nosemgrep: dangerous-subprocess-use-audit
        proc = subprocess.run(
            ["bash", "-lc", "source ~/.nvm/nvm.sh 2>/dev/null && nvm which 20 2>/dev/null"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        node_path = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        if node_path and Path(node_path).is_file():
            codex_candidate = Path(node_path).parent / "codex"
            if codex_candidate.exists():
                _CODEX_BIN_CACHE = str(codex_candidate)
                return _CODEX_BIN_CACHE
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    raise RuntimeError(
        "codex CLI not found. Set AIDLC_EVAL_CODEX_BIN to the absolute path, "
        "put 'codex' on PATH, or install via 'nvm install 20 && npm i -g @openai/codex'."
    )


def _invoke_codex_cli(req: LlmRequest) -> str:
    binary = _resolve_codex_bin()

    # Codex only accepts a single [PROMPT]; fold system into the same string.
    combined = (req.system or "").strip()
    if req.user:
        if combined:
            combined = f"{combined}\n\n---\n\n{req.user.strip()}"
        else:
            combined = req.user.strip()
    if not combined:
        raise RuntimeError("LlmRequest has empty system and user; nothing to send")

    # Use a temp file for the last-message capture so we get a clean, ANSI-free
    # copy of the assistant reply regardless of stdout noise (MCP transport
    # warnings, banners, etc.).
    tmpdir = tempfile.mkdtemp(prefix="codex-reply-")
    last_msg_path = os.path.join(tmpdir, "last_message.txt")

    args = [
        binary,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        req.codex_sandbox,
        "--ignore-user-config",
        "--ignore-rules",
        "--color",
        "never",
        "--output-last-message",
        last_msg_path,
    ]
    if req.codex_model:
        args += ["--model", req.codex_model]
    args.append(combined)

    try:
        # nosec B603 - Executing codex, resolved via _resolve_codex_bin above.
        # nosemgrep: dangerous-subprocess-use-audit
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=req.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(
            f"codex exec timed out after {req.timeout_seconds}s"
        ) from exc

    reply = ""
    if os.path.isfile(last_msg_path):
        try:
            reply = Path(last_msg_path).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    shutil.rmtree(tmpdir, ignore_errors=True)

    if result.returncode != 0:
        raise RuntimeError(
            "codex exec failed "
            f"(rc={result.returncode}, stderr={result.stderr.strip()[:500]!r})"
        )

    if not reply:
        raise RuntimeError(
            "codex exec returned empty last-message. "
            f"Raw stdout tail={result.stdout[-300:]!r}"
        )
    return reply


# ---------------------------------------------------------------------------
# bedrock backend
# ---------------------------------------------------------------------------

def _invoke_bedrock(req: LlmRequest) -> str:
    try:
        import boto3  # noqa: WPS433 - runtime optional dep
    except ImportError as exc:
        raise RuntimeError("boto3 required for bedrock backend") from exc

    session_kwargs: dict = {}
    if req.aws_profile:
        session_kwargs["profile_name"] = req.aws_profile
    session = boto3.Session(**session_kwargs)

    client_kwargs: dict = {"service_name": "bedrock-runtime"}
    if req.aws_region:
        client_kwargs["region_name"] = req.aws_region
    client = session.client(**client_kwargs)

    body_dict: dict = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "messages": [{"role": "user", "content": req.user}],
    }
    if req.system:
        body_dict["system"] = req.system

    try:
        response = client.invoke_model(
            modelId=req.bedrock_model_id,
            body=json.dumps(body_dict),
        )
        payload = json.loads(response["body"].read())
    except Exception as exc:  # boto3 raises a variety of exception types
        raise RuntimeError(f"bedrock invoke_model failed: {exc}") from exc

    try:
        return payload["content"][0]["text"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"unexpected bedrock response shape: keys={list(payload)[:5]}"
        ) from exc


# ---------------------------------------------------------------------------
# Backwards-compat re-exports (older code paths import re from this module)
# ---------------------------------------------------------------------------

__all__ = (
    "Backend",
    "LlmRequest",
    "invoke_llm",
    "is_codex_cli_backend",
    "resolve_backend",
)


# Deliberately kept unused so linters know `re` isn't spurious in edits below.
_ = re
