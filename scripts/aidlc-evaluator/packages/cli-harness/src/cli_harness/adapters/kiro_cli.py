"""Kiro CLI adapter — drives AIDLC workflows via kiro-cli subprocess.

Uses ``kiro-cli chat --no-interactive`` for fully headless execution.  Tool
authorization depends on the agent engine (v3 requires an explicit whitelist
via ``--trust-tools``; the v1 legacy path uses ``--trust-all-tools``).

## v2 agentic execution (default when kiro_dist_path is set)

When ``AdapterConfig.kiro_dist_path`` points to the ``.kiro/`` distribution
directory (e.g. ``dist/kiro/.kiro``), the adapter:

1. Copies the entire ``.kiro/`` tree into the workspace root so Kiro picks up
   skills, agents, hooks, and protocols natively.
2. Runs ``kiro-cli chat --agent-engine v3 --agent aidlc`` so the aidlc agent
   and its skills load into the v3 agent engine.  The v3 engine registers
   agent-scoped skills as slash commands, which is the mechanism upstream
   ``.kiro/agents/aidlc.json`` describes ("run /aidlc to start or resume a
   workflow"). The v2 engine (default in older Kiro CLI builds) does NOT
   expose skills as slash commands, so the invocation would fail there.
3. Sends ``/aidlc\\n\\n<vision content>`` as the initial prompt, invoking
   the top-level aidlc skill directly.
4. Because the v3 engine rejects ``--trust-all-tools``, the adapter passes an
   explicit whitelist matching the aidlc agent's declared ``tools`` field:
   ``--trust-tools=fs_read,fs_write,execute_bash,todo_list,thinking,subagent``.
5. Corporate CA bundles set in the environment (``SSL_CERT_FILE`` for Python
   / uv, ``NODE_EXTRA_CA_CERTS`` for the Node-based kiro-cli v3 runtime)
   are inherited by subprocesses via the parent process's environment.
6. Detects completion by checking for an ``intent-*/state/intent-state.md`` file
   containing ``status: complete``.

The process-check-hook.json in ``.kiro/hooks/`` fires automatically after every
``invokeSubAgent`` call, enforcing ``process_checker.js`` without any evaluator
intervention.

## v1 legacy execution (when kiro_dist_path is not set)

Falls back to the original steering-file mechanism: concatenates all rule
``.md`` files into ``.kiro/steering/aidlc-rules.md`` and sends a monolithic
AIDLC executor prompt.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from cli_harness.adapter import AdapterConfig, AdapterResult, CLIAdapter
from cli_harness.human_analog import generate_human_response
from cli_harness.normalizer import normalize_output
from cli_harness.prompt_template import render_prompt, render_v2_prompt

logger = logging.getLogger(__name__)

_KIRO_CLI = "kiro-cli"

# Matches ANSI escape sequences: CSI sequences (\x1b[...X), OSC sequences (\x1b]...\x07),
# and simple two-byte escapes (\x1b followed by one char).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b.")

_DONE_SIGNALS = re.compile(
    r"(\b(complete|completed|finished|done|no more phases|no remaining|nothing left|"
    r"all phases|all stages|all steps|no next phase|workflow ended|workflow complete|"
    r"intent is complete|intent is finished|nothing to run|no pending)\b|^🏁$|^✅$)",
    re.IGNORECASE | re.MULTILINE,
)

_APPROVAL_SIGNALS = re.compile(
    r"(━{5,}|Proposed Workflow for Approval|for approval|awaiting approval|"
    r"please approve|ready to proceed)",
    re.IGNORECASE,
)

# Numbered option lists — Kiro presents them as
#   1. First option
#   2. Second option — extra description
# on separate lines.  We treat "two or more lines of the form ``\d+\.\s+``" as
# an option-selection prompt so the turn classifier does not misread the trailing
# option text as a "done" signal.
_OPTION_LINE_RE = re.compile(r"^\s*\d+\.\s+\S", re.MULTILINE)


def _has_option_list(text: str) -> bool:
    """Return True when ``text`` contains at least two numbered option lines."""
    return len(_OPTION_LINE_RE.findall(text)) >= 2


_ACTIVE_SIGNALS = re.compile(
    r"(using tool:|I'll create|I will run|Reading file|Writing file|"
    r"Layer \d|Step \d|proceeding to|✅|→)",
    re.IGNORECASE,
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def _classify_turn_output(raw_output: str) -> str:
    """Classify what Kiro said at the end of a turn.

    Returns one of:
      'approval_needed' — Kiro presented work and is waiting for human approval
      'done'            — Kiro says the workflow is complete, nothing left to do
      'continue'        — Kiro is actively working or waiting for a nudge
    """
    text = _strip_ansi(raw_output)
    response_lines = []
    for line in text.splitlines():
        if line.startswith("> "):
            response_lines = [line[2:]]
        elif response_lines:
            response_lines.append(line)
    response = "\n".join(response_lines).strip()

    if _ACTIVE_SIGNALS.search(response):
        return "continue"
    # Numbered option lists are approval-style prompts, not completion signals,
    # even if the option labels happen to contain words like "done" or "nothing".
    if _has_option_list(text) or _has_option_list(response):
        return "approval_needed"
    if _APPROVAL_SIGNALS.search(text):
        return "approval_needed"
    if _DONE_SIGNALS.search(response) and len(response) < 500:
        return "done"
    return "continue"


def _check_intent_state_complete(aidlc_docs_dir) -> bool:
    """Return True if intent-state.md indicates the full workflow is done.

    Requires both:
    - All table rows at terminal states (complete or approved)
    - At least one construction-phase skill (code-generation) is complete
      so that bootstrap-only runs don't trigger a false positive.
    """
    if aidlc_docs_dir is None:
        return False
    for state_file in aidlc_docs_dir.rglob("intent-state.md"):
        content = state_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("status:") and "complete" in stripped.lower():
                return True
        table_rows = [
            l for l in content.splitlines()
            if l.startswith("| ") and not l.startswith("| Skill") and not l.startswith("| ---")
        ]
        terminal = {"complete", "approved"}
        has_construction = any(
            "code-generation" in row.lower() or "build-and-test" in row.lower()
            for row in table_rows
        )
        if has_construction and table_rows and all(
            any(t in cell.lower() for t in terminal)
            for row in table_rows
            for cell in row.split("|")[3:4]
        ):
            return True
    return False


def _log(msg: str) -> None:
    """Print a progress message to stderr."""
    print(f"  [kiro-cli] {msg}", file=sys.stderr, flush=True)


def _find_aidlc_docs(workspace: Path) -> Path | None:
    """Find the AIDLC output directory anywhere under workspace.

    v1 layouts put artifacts under ``workspace/aidlc-docs/``; v2 layouts put
    them under ``workspace/aidlc/spaces/<space>/intents/<slug>/`` with the
    top-level directory being ``aidlc/``.  We accept either.

    Search order:
      1. ``workspace/aidlc-docs/`` (v1 direct)
      2. ``workspace/aidlc/`` (v2 direct)
      3. one level deep for either name (covers legacy nested layouts)

    Returns the first match, or None if not found.
    """
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


class KiroCLIAdapter(CLIAdapter):
    """Adapter for kiro-cli.

    Uses ``kiro-cli chat --no-interactive`` for headless
    execution via subprocess.
    """

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    @property
    def name(self) -> str:
        return "kiro-cli"

    def check_prerequisites(self) -> tuple[bool, str]:
        """Verify that ``kiro-cli`` is on PATH."""
        if not shutil.which(_KIRO_CLI):
            return False, (
                f"'{_KIRO_CLI}' not found in PATH. "
                "Install the Kiro CLI first (https://kiro.dev)."
            )
        return True, f"Kiro CLI ('{_KIRO_CLI}') found"

    def run(self, config: AdapterConfig) -> AdapterResult:
        """Execute the full AIDLC workflow through kiro-cli.

        Runs directly in ``<output_dir>/workspace/`` — no temp dir or copy step.
        """
        ok, msg = self.check_prerequisites()
        if not ok:
            return AdapterResult(
                success=False,
                output_dir=config.output_dir,
                error=f"Prerequisites not met: {msg}",
            )

        start_time = time.monotonic()

        # Work directly in the final output location
        config.output_dir.mkdir(parents=True, exist_ok=True)
        workspace = config.output_dir / "workspace"
        workspace.mkdir(exist_ok=True)
        _log(f"Workspace: {workspace}")

        try:
            # Copy input documents
            shutil.copy2(config.vision_path, workspace / "vision.md")
            _log(f"Copied vision: {config.vision_path}")
            if config.tech_env_path and config.tech_env_path.is_file():
                shutil.copy2(config.tech_env_path, workspace / "tech-env.md")
                _log(f"Copied tech-env: {config.tech_env_path}")

            is_v2 = config.kiro_dist_path is not None and config.kiro_dist_path.is_dir()

            if is_v2:
                # v2: copy the full .kiro/ distribution so Kiro picks up skills,
                # agents, hooks, and protocol files natively.
                kiro_dest = workspace / ".kiro"
                if kiro_dest.exists():
                    shutil.rmtree(kiro_dest)
                shutil.copytree(config.kiro_dist_path, kiro_dest)
                _log(f"Installed .kiro/ distribution from {config.kiro_dist_path}")

                # Record a provenance manifest so the A/B comparison can verify
                # that the installed skills/tools actually came from the rules ref
                # (rather than the evaluator's own dist snapshot).
                try:
                    import hashlib
                    manifest_lines = [
                        f"source: {config.kiro_dist_path}",
                        "",
                        "# sha256  path (relative to installed .kiro/)",
                    ]
                    for f in sorted(kiro_dest.rglob("*")):
                        if f.is_file():
                            digest = hashlib.sha256(f.read_bytes()).hexdigest()
                            rel = f.relative_to(kiro_dest)
                            manifest_lines.append(f"{digest}  {rel}")
                    manifest_path = config.output_dir / "kiro-dist-manifest.txt"
                    manifest_path.write_text(
                        "\n".join(manifest_lines) + "\n", encoding="utf-8"
                    )
                    _log(f"Wrote dist provenance manifest → {manifest_path}")
                except Exception as _prov_exc:  # pragma: no cover - non-fatal
                    _log(f"[warn] failed to write dist provenance manifest: {_prov_exc}")

                # Verify the ``aidlc`` skill and matching agent are actually
                # present in the installed distribution.  Without both, the v3
                # engine cannot register ``/aidlc`` as a slash command.
                if not (kiro_dest / "skills" / "aidlc" / "SKILL.md").is_file():
                    _log(
                        "[warn] installed .kiro/ has no top-level 'aidlc' skill "
                        "(skills/aidlc/SKILL.md); '/aidlc' invocation will fail. "
                        "This usually means the rules ref pre-dates the v2 aidlc "
                        "skill or --kiro-dist points at an old snapshot."
                    )
                if not (kiro_dest / "agents" / "aidlc.json").is_file():
                    _log(
                        "[warn] installed .kiro/ has no 'aidlc' agent "
                        "(agents/aidlc.json); we cannot pass --agent aidlc.  "
                        "Without the agent context the v3 engine will not expose "
                        "the aidlc skill as a slash command."
                    )

                # Build v2 prompt: /aidlc + vision content (top-level aidlc skill)
                vision_content = config.vision_path.read_text(encoding="utf-8")
                prompt = config.prompt_template or render_v2_prompt(vision_content)
                _log("Using v2 agentic execution (engine=v3, agent=aidlc, /aidlc slash)")
                # Surface any corporate-CA env vars that will be inherited, so
                # TLS issues (e.g. Node's kiro-cli v3 runtime not trusting the
                # corporate CA) are diagnosable from the run log.
                for _env_var in ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"):
                    if os.environ.get(_env_var):
                        _log(f"  env {_env_var}={os.environ[_env_var]}")
            else:
                # v1 legacy: inject rules as a single steering file
                steering_dir = workspace / ".kiro" / "steering"
                steering_dir.mkdir(parents=True, exist_ok=True)

                rules_path = config.rules_path
                if rules_path.is_dir():
                    parts = []
                    for rule_file in sorted(rules_path.rglob("*.md")):
                        parts.append(rule_file.read_text(encoding="utf-8"))
                    rules_content = "\n\n".join(parts)
                else:
                    rules_content = rules_path.read_text(encoding="utf-8")

                (steering_dir / "aidlc-rules.md").write_text(
                    rules_content, encoding="utf-8"
                )
                _log(f"Injected AIDLC rules ({len(rules_content)} chars) via steering file")
                prompt = config.prompt_template or render_prompt()
                _log("Using v1 legacy execution (steering file)")

            # Base command flags
            base_flags = [
                "--no-interactive",
            ]
            if is_v2:
                # v3 agent engine registers agent-scoped skills as slash
                # commands (upstream aidlc.json contract).  It rejects the
                # ``--trust-all-tools`` shortcut, so we hand it an explicit
                # whitelist that matches the aidlc agent's ``tools`` field.
                base_flags += [
                    "--agent-engine", "v3",
                    "--trust-tools=fs_read,fs_write,execute_bash,todo_list,thinking,subagent",
                ]
                if (workspace / ".kiro" / "agents" / "aidlc.json").is_file():
                    base_flags += ["--agent", "aidlc"]
            else:
                # v1 legacy: default engine, trust every tool for the monolithic
                # steering-file prompt.
                base_flags += ["--trust-all-tools"]
            if config.model:
                base_flags += ["--model", config.model]

            # Run kiro-cli in a loop, letting the AI response drive stop/continue logic.
            # With --no-interactive, kiro-cli exits after each response; we resume
            # based on what Kiro said rather than blindly sending the same approval.
            log_path = config.output_dir / "kiro-session.log"
            _log(f"Session log: {log_path}")

            turn = 0
            max_turns = 100  # safety cap — AI response drives stopping, not this number
            total_rc = 0
            next_prompt = prompt  # updated each turn based on classification

            with open(log_path, "w", encoding="utf-8") as log_file:
                while turn < max_turns:
                    turn += 1

                    if turn == 1:
                        cmd = [_KIRO_CLI, "chat"] + base_flags + [next_prompt]
                        _log(f"Turn {turn}: initial prompt ({len(next_prompt)} chars)")
                    else:
                        cmd = [_KIRO_CLI, "chat"] + base_flags + ["--resume", next_prompt]
                        _log(f"Turn {turn}: {next_prompt!r}")

                    log_file.write(f"\n{'='*60}\nTURN {turn}\n{'='*60}\n")
                    log_file.flush()

                    # nosec B603 - Executing user's Kiro CLI with validated configuration
                    # nosemgrep: dangerous-subprocess-use-audit
                    process = subprocess.Popen(
                        cmd,
                        cwd=str(workspace),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )

                    turn_output_lines: list[str] = []
                    for line in process.stdout:
                        log_file.write(_strip_ansi(line))
                        log_file.flush()
                        turn_output_lines.append(line)
                        if self.verbose:
                            sys.stderr.write(line)
                            sys.stderr.flush()

                    remaining = config.timeout_seconds - (time.monotonic() - start_time)
                    if remaining <= 0:
                        process.kill()
                        _log(f"Timeout reached at turn {turn}")
                        break
                    process.wait(timeout=max(remaining, 10))
                    total_rc = process.returncode
                    turn_output = "".join(turn_output_lines)

                    _log(f"Turn {turn} exited with code {process.returncode}")

                    # Classify what Kiro said to decide next action
                    turn_classification = _classify_turn_output(turn_output)

                    aidlc_docs_dir = _find_aidlc_docs(workspace)
                    file_count = sum(1 for _ in aidlc_docs_dir.rglob("*") if _.is_file()) if aidlc_docs_dir else 0

                    if is_v2:
                        state_complete = _check_intent_state_complete(aidlc_docs_dir)
                        _log(f"  aidlc-docs: {file_count} files, intent-state={'complete' if state_complete else 'in-progress'}, turn={turn_classification}")

                        if state_complete or turn_classification == "done":
                            _log("Workflow complete — stopping")
                            break
                        else:
                            next_prompt = generate_human_response(
                                turn_output=turn_output,
                                vision_path=config.vision_path,
                                tech_env_path=config.tech_env_path,
                                aws_profile=config.aws_profile,
                                aws_region=config.aws_region,
                                model_id=config.scorer_model,
                            )
                            _log(f"  human analog: {next_prompt[:80]!r}")
                    else:
                        has_construction = (
                            aidlc_docs_dir is not None
                            and (aidlc_docs_dir / "construction").is_dir()
                            and any((aidlc_docs_dir / "construction").rglob("*.md"))
                        )
                        _log(f"  aidlc-docs: {file_count} files, construction={'yes' if has_construction else 'no'}, turn={turn_classification}")

                        if has_construction or turn_classification == "done":
                            _log("Workflow complete — stopping")
                            break
                        else:
                            next_prompt = generate_human_response(
                                turn_output=turn_output,
                                vision_path=config.vision_path,
                                tech_env_path=config.tech_env_path,
                                aws_profile=config.aws_profile,
                                aws_region=config.aws_region,
                                model_id=config.scorer_model,
                            )
                            _log(f"  human analog: {next_prompt[:80]!r}")

                    elapsed = time.monotonic() - start_time
                    if elapsed >= config.timeout_seconds:
                        _log("Timeout reached")
                        break

            elapsed_seconds = time.monotonic() - start_time
            _log(f"Completed {turn} turn(s) in {elapsed_seconds:.0f}s")

            # List workspace contents for debugging
            _log("Workspace contents:")
            for item in sorted(workspace.iterdir()):
                _log(f"  {item.name}/") if item.is_dir() else _log(f"  {item.name}")

            # Move aidlc-docs to output_dir/ — search anywhere under workspace
            # (v2 places it at org-ai-kb/aidlc-docs/, v1 at aidlc-docs/)
            src_docs = _find_aidlc_docs(workspace)
            dst_docs = config.output_dir / "aidlc-docs"
            if src_docs is not None:
                if dst_docs.exists():
                    shutil.rmtree(dst_docs)
                shutil.move(str(src_docs), str(dst_docs))

            # Write run-meta.yaml and run-metrics.yaml
            # Kiro CLI does not expose token usage; pass turn count
            # so downstream reports show "data unavailable" rather than
            # silently reporting zeros that look like infinite efficiency.
            normalize_output(
                source_dir=workspace,
                output_dir=config.output_dir,
                adapter_name=self.name,
                elapsed_seconds=elapsed_seconds,
                token_usage={
                    "num_turns": turn,
                    "model": config.model or "",
                },
            )

            has_docs = dst_docs.is_dir() and any(dst_docs.iterdir())

            if total_rc == 0 and has_docs:
                return AdapterResult(
                    success=True,
                    output_dir=config.output_dir,
                    aidlc_docs_dir=dst_docs,
                    workspace_dir=workspace,
                    elapsed_seconds=elapsed_seconds,
                )

            error_detail = (
                f"kiro-cli completed {turn} turn(s), "
                "no aidlc-docs/ output was produced."
                if not has_docs
                else f"kiro-cli completed {turn} turn(s) "
                "but aidlc-docs/ may be incomplete."
            )
            return AdapterResult(
                success=has_docs,
                output_dir=config.output_dir,
                aidlc_docs_dir=dst_docs if has_docs else None,
                workspace_dir=workspace,
                error=error_detail if not has_docs else None,
                elapsed_seconds=elapsed_seconds,
            )

        except subprocess.TimeoutExpired:
            elapsed_seconds = time.monotonic() - start_time
            process.kill()
            _log(f"Timeout after {elapsed_seconds:.0f}s — killed process")
            return AdapterResult(
                success=False,
                output_dir=config.output_dir,
                workspace_dir=workspace,
                error=f"kiro-cli timed out after {config.timeout_seconds}s",
                elapsed_seconds=elapsed_seconds,
            )

        except Exception as exc:
            elapsed_seconds = time.monotonic() - start_time
            logger.exception("kiro-cli adapter run failed")
            return AdapterResult(
                success=False,
                output_dir=config.output_dir,
                workspace_dir=workspace,
                error=f"kiro-cli adapter error: {exc}",
                elapsed_seconds=elapsed_seconds,
            )
