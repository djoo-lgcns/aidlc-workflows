#!/usr/bin/env python3
"""Run AIDLC evaluation through a CLI adapter.

Usage:
    # List available adapters
    python run_cli_evaluation.py --list

    # Run evaluation through kiro-cli
    python run_cli_evaluation.py --cli kiro-cli \
        --vision test_cases/sci-calc/vision.md \
        --golden test_cases/sci-calc/golden-aidlc-docs

    # Check prerequisites for a CLI tool
    python run_cli_evaluation.py --cli kiro-cli --check-only

    # Override rules ref (branch/tag/commit)
    python run_cli_evaluation.py --cli claude-code --rules-ref v0.2.0

    # Use local rules directory instead of git clone
    python run_cli_evaluation.py --cli claude-code --rules-path /path/to/rules
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = REPO_ROOT / "packages"

# Add cli-harness to path
sys.path.insert(0, str(PACKAGES / "cli-harness" / "src"))

from cli_harness.registry import get_adapter, list_adapters  # noqa: E402
from cli_harness.orchestrator import run_cli_evaluation  # noqa: E402

_SLUG_MAX_LEN = 80


def _rules_slug(
    rules_source: str,
    rules_repo: str,
    rules_ref: str,
    rules_local_path: str | None,
) -> str:
    """Derive a filesystem-safe slug from the AIDLC rules configuration.

    Mirrors packages/execution/src/aidlc_runner/runner.py:_rules_slug().
    """
    if rules_source == "local" and rules_local_path:
        raw = f"local_{Path(rules_local_path).name}"
    else:
        path = urlparse(rules_repo).path.rstrip("/")
        repo_name = Path(path).stem  # strips .git suffix
        raw = f"{repo_name}_{rules_ref}"
    slug = raw.replace(" ", "-")
    slug = re.sub(r"[^a-zA-Z0-9._-]", "", slug)
    return slug[:_SLUG_MAX_LEN]


def _default_output_dir(cli_name: str, slug: str) -> Path:
    """Generate a timestamped output directory matching the normal run pattern.

    Format: runs/{timestamp}-{rules_slug}-{cli_name}
    Example: runs/20260227T160245-aidlc-workflows_main-kiro-cli
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return REPO_ROOT / "runs" / f"{ts}-{slug}-{cli_name.lower()}"


def _setup_rules(
    output_dir: Path,
    *,
    rules_source: str = "git",
    rules_repo: str = "https://github.com/awslabs/aidlc-workflows.git",
    rules_ref: str = "main",
    rules_local_path: str | None = None,
) -> Path:
    """Download or copy AIDLC rules into the output directory.

    Mirrors the pattern from packages/execution/src/aidlc_runner/runner.py:setup_rules().
    """
    rules_dest = output_dir / "aidlc-rules"

    if rules_source == "local" and rules_local_path:
        local_path = Path(rules_local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"Local rules path not found: {local_path}")
        shutil.copytree(local_path / "aidlc-rules", rules_dest)
    else:
        # Git clone (shallow, single branch)
        print(f"  Cloning AIDLC rules from {rules_repo} (ref: {rules_ref})...")
        # nosec B603, B607 - Git clone of trusted AIDLC rules repository
        # nosemgrep: dangerous-subprocess-use-audit
        result = subprocess.run(
            [
                "git", "clone",
                "--branch", rules_ref,
                "--depth", "1",
                rules_repo,
                str(rules_dest / "_repo"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to clone AIDLC rules repo:\n{result.stderr}")

        # Move aidlc-rules content up from _repo/aidlc-rules/ to rules_dest/
        repo_rules = rules_dest / "_repo" / "aidlc-rules"
        if repo_rules.exists():
            for item in repo_rules.iterdir():
                shutil.move(str(item), str(rules_dest / item.name))

        # Clean up the full repo clone (force-remove read-only git pack files)
        def _force_remove_readonly(func, path, _exc_info):
            os.chmod(path, stat.S_IWRITE)
            func(path)

        # onexc was added in Python 3.12; fall back to onerror on older versions
        if sys.version_info >= (3, 12):
            shutil.rmtree(rules_dest / "_repo", onexc=_force_remove_readonly)
        else:
            shutil.rmtree(rules_dest / "_repo", onerror=_force_remove_readonly)

    return rules_dest


def _setup_dist_from_rules(
    output_dir: Path,
    *,
    rules_source: str,
    rules_repo: str,
    rules_ref: str,
    rules_local_path: str | None,
    dist_subpath: str = "dist/kiro/.kiro",
) -> Path | None:
    """Extract the Kiro distribution (`dist/kiro/.kiro`) from the SAME rules ref
    that `_setup_rules` cloned.

    This ties the Kiro skills/tools/agents that get installed into the workspace
    to the exact same commit as the `aidlc-rules/` tree, preserving A/B provenance.

    Returns the path to the extracted ``.kiro/`` directory, or ``None`` if
    ``dist_subpath`` does not exist in the rules ref (caller should fall back).
    """
    dest_root = output_dir / "kiro-dist"
    dest_kiro = dest_root / ".kiro"

    if rules_source == "local" and rules_local_path:
        local_path = Path(rules_local_path)
        src = local_path / dist_subpath
        if not src.is_dir():
            print(
                f"  [warn] Local rules path {local_path} has no {dist_subpath}; "
                "cannot derive dist from rules ref"
            )
            return None
        if dest_root.exists():
            shutil.rmtree(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest_kiro)
        print(f"  Extracted {dist_subpath} from local rules path → {dest_kiro}")
        return dest_kiro

    # Git-based rules_source: sparse-checkout just the dist subtree from the same ref.
    # We do a separate clone here rather than piggy-backing on _setup_rules, because
    # _setup_rules removes everything outside `aidlc-rules/`.
    tmp_repo = dest_root / "_repo"
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    print(
        f"  Cloning {dist_subpath} from {rules_repo} (ref: {rules_ref}) "
        "to derive Kiro dist from the same commit as rules..."
    )
    # nosec B603, B607 - Sparse-checkout of a trusted AIDLC rules repository
    # nosemgrep: dangerous-subprocess-use-audit
    clone_res = subprocess.run(
        [
            "git", "clone",
            "--branch", rules_ref,
            "--depth", "1",
            "--filter=blob:none",
            "--sparse",
            "--no-checkout",
            rules_repo,
            str(tmp_repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if clone_res.returncode != 0:
        print(
            f"  [warn] Sparse clone for dist failed (returncode={clone_res.returncode}):"
            f"\n{clone_res.stderr}"
        )
        shutil.rmtree(dest_root, ignore_errors=True)
        return None

    # Configure sparse-checkout and materialize just the subpath.
    for cmd in (
        ["git", "-C", str(tmp_repo), "sparse-checkout", "init", "--cone"],
        ["git", "-C", str(tmp_repo), "sparse-checkout", "set", dist_subpath],
        ["git", "-C", str(tmp_repo), "checkout", rules_ref],
    ):
        # nosec B603, B607 - trusted git commands with validated args
        # nosemgrep: dangerous-subprocess-use-audit
        step = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if step.returncode != 0:
            print(
                f"  [warn] Command {' '.join(cmd)} failed:\n{step.stderr}"
            )
            shutil.rmtree(dest_root, ignore_errors=True)
            return None

    src = tmp_repo / dist_subpath
    if not src.is_dir():
        print(
            f"  [warn] Rules ref {rules_ref} does not contain {dist_subpath}; "
            "cannot derive dist from rules ref"
        )
        shutil.rmtree(dest_root, ignore_errors=True)
        return None

    shutil.copytree(src, dest_kiro)

    def _force_remove_readonly(func, path, _exc_info):
        os.chmod(path, stat.S_IWRITE)
        func(path)

    if sys.version_info >= (3, 12):
        shutil.rmtree(tmp_repo, onexc=_force_remove_readonly)
    else:
        shutil.rmtree(tmp_repo, onerror=_force_remove_readonly)

    print(f"  Extracted {dist_subpath} from {rules_ref} → {dest_kiro}")
    return dest_kiro


def _preflight_aws_credentials(profile: str | None, region: str | None) -> None:
    """Fail-closed check that AWS credentials work before any billable run.

    Skipped automatically when both the human-analog and scorer components
    are configured to use a non-Bedrock backend (e.g. ``kiro-cli``); AWS
    credentials are irrelevant in that case.  Can also be force-skipped by
    setting ``AIDLC_EVAL_SKIP_AWS_PREFLIGHT=1``.
    """
    if os.environ.get("AIDLC_EVAL_SKIP_AWS_PREFLIGHT") == "1":
        print("  [preflight] AWS sts check skipped (AIDLC_EVAL_SKIP_AWS_PREFLIGHT=1)")
        return

    # If every component that would call an LLM is on a non-Bedrock backend,
    # the sts check is not required — the run will not touch AWS.
    try:
        # ``shared`` is provided by the packages/shared workspace member.
        from shared.llm import resolve_backend  # type: ignore
    except ImportError:
        resolve_backend = None  # type: ignore[assignment]
    if resolve_backend is not None:
        try:
            human_backend = resolve_backend(component="human")
            scorer_backend = resolve_backend(component="scorer")
        except ValueError as exc:
            print(f"  [preflight] backend config error: {exc}", file=sys.stderr)
            sys.exit(3)
        if human_backend != "bedrock" and scorer_backend != "bedrock":
            print(
                "  [preflight] AWS sts check skipped: "
                f"human_backend={human_backend}, scorer_backend={scorer_backend} "
                "(no Bedrock calls will be made)"
            )
            return

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - boto3 is a hard dep
        print(f"  [preflight] boto3 not available for AWS preflight: {exc}", file=sys.stderr)
        sys.exit(3)

    session_kwargs: dict = {}
    if profile:
        session_kwargs["profile_name"] = profile
    session = boto3.Session(**session_kwargs)
    client_kwargs: dict = {"service_name": "sts"}
    if region:
        client_kwargs["region_name"] = region
    try:
        sts = session.client(**client_kwargs)
        ident = sts.get_caller_identity()
    except (BotoCoreError, ClientError) as exc:
        print(
            "  [preflight] aws sts get-caller-identity FAILED: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        print(
            "  [preflight] Refusing to start: Bedrock human-analog calls would fail "
            "and the evaluator would silently fall back to an approval loop, "
            "producing no meaningful A/B data.  Fix AWS credentials and retry, or "
            "set AIDLC_EVAL_SKIP_AWS_PREFLIGHT=1 to override at your own risk. "
            "Alternatively set AIDLC_EVAL_LLM_BACKEND=codex-cli to run without "
            "Bedrock at all (uses the Codex CLI's own auth).",
            file=sys.stderr,
        )
        sys.exit(3)
    print(
        "  [preflight] AWS sts get-caller-identity OK: "
        f"account={ident.get('Account')} arn={ident.get('Arn')}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run_cli_evaluation",
        description="Run AIDLC evaluation through a CLI AI assistant",
    )
    parser.add_argument(
        "--cli", type=str,
        help="CLI adapter name (e.g., kiro-cli)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List available CLI adapters and exit",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Only check CLI prerequisites, don't run evaluation",
    )
    parser.add_argument(
        "--config", type=Path,
        default=REPO_ROOT / "config" / "default.yaml",
        help="Path to YAML config file (default: config/default.yaml)",
    )
    parser.add_argument("--vision", type=Path, default=REPO_ROOT / "test_cases" / "sci-calc-v2" / "vision.md")
    parser.add_argument("--tech-env", type=Path, default=REPO_ROOT / "test_cases" / "sci-calc-v2" / "tech-env.md")
    parser.add_argument("--golden", type=Path, default=REPO_ROOT / "test_cases" / "sci-calc-v2" / "golden-aidlc-docs")
    parser.add_argument("--openapi", type=Path, default=REPO_ROOT / "test_cases" / "sci-calc-v2" / "openapi.yaml")
    parser.add_argument("--baseline", type=Path, default=REPO_ROOT / "test_cases" / "sci-calc-v2" / "golden.yaml")
    parser.add_argument(
        "--rules-ref", default=None,
        help="Git ref (branch/tag/commit) for AIDLC rules (overrides config value)",
    )
    parser.add_argument(
        "--rules-path", type=Path, default=None,
        help="Path to local AIDLC rules directory (overrides git clone)",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--profile", default=None, help="AWS profile (default: from config YAML)")
    parser.add_argument("--region", default=None, help="AWS region (default: from config YAML)")
    parser.add_argument("--scorer-model", default=None, help="Bedrock model for scoring (default: from config YAML)")
    parser.add_argument("--model", default=None, help="Model to use with the CLI adapter (e.g., claude-sonnet-4)")
    parser.add_argument(
        "--kiro-dist", type=Path, default=None,
        help=(
            "Path to the .kiro/ distribution directory for v2 agentic execution "
            "(e.g. dist/kiro/.kiro). When set, the kiro adapter copies this into "
            "the workspace so Kiro picks up skills, agents, hooks, and protocols "
            "natively and invokes /skill aidlc-orchestrator. "
            "Defaults to dist/kiro/.kiro relative to the repo root if it exists."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging output",
    )

    args = parser.parse_args()

    if args.verbose:
        import logging
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        )

    if args.list:
        print("Available CLI adapters:")
        for name in list_adapters():
            try:
                adapter = get_adapter(name)
                ok, msg = adapter.check_prerequisites()
                status = "ready" if ok else "not ready"
                print(f"  {name:15s}  [{status}] {msg}")
            except Exception as e:
                print(f"  {name:15s}  [error] {e}")
        sys.exit(0)

    if not args.cli:
        parser.error("--cli is required (use --list to see available adapters)")

    adapter = get_adapter(args.cli)
    adapter.verbose = args.verbose

    if args.check_only:
        ok, msg = adapter.check_prerequisites()
        print(f"{adapter.name}: {'OK' if ok else 'FAIL'} — {msg}")
        sys.exit(0 if ok else 1)

    # ── Resolve defaults from config YAML when not provided on CLI ──────
    cfg_data: dict = {}
    if args.config and args.config.exists():
        with open(args.config, encoding="utf-8") as f:
            cfg_data = yaml.safe_load(f) or {}

    if args.profile is None:
        args.profile = cfg_data.get("aws", {}).get("profile")
    if args.region is None:
        args.region = cfg_data.get("aws", {}).get("region")
    if args.scorer_model is None:
        args.scorer_model = (
            cfg_data.get("models", {}).get("scorer", {}).get("model_id")
        )
        if args.scorer_model is None:
            parser.error(
                "--scorer-model is required (or set models.scorer.model_id in config YAML)"
            )

    # ── Resolve AIDLC rules config ────────────────────────────────────────
    aidlc_cfg = cfg_data.get("aidlc", {})
    rules_source = aidlc_cfg.get("rules_source", "git")
    rules_repo = aidlc_cfg.get("rules_repo", "https://github.com/awslabs/aidlc-workflows.git")
    rules_ref = args.rules_ref or aidlc_cfg.get("rules_ref", "main")

    if args.rules_path:
        rules_source = "local"
        rules_local_path = str(Path(args.rules_path).resolve())
    else:
        rules_local_path = aidlc_cfg.get("rules_local_path")

    # Resolve all paths relative to cwd so they work from any directory
    vision_path = Path(args.vision).resolve()
    tech_env_path = Path(args.tech_env).resolve()
    golden_docs = Path(args.golden).resolve()
    openapi_path = Path(args.openapi).resolve()
    baseline_path = Path(args.baseline).resolve()
    slug = _rules_slug(rules_source, rules_repo, rules_ref, rules_local_path)
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else _default_output_dir(args.cli, slug)
    )

    # ── Setup AIDLC rules (git clone or local copy) ─────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)

    # Preflight AWS credentials before any billable run (Bedrock human analog would
    # otherwise silently fall back to a canned approval loop).
    _preflight_aws_credentials(args.profile, args.region)

    rules_path = _setup_rules(
        output_dir,
        rules_source=rules_source,
        rules_repo=rules_repo,
        rules_ref=rules_ref,
        rules_local_path=rules_local_path,
    )

    # Resolve kiro_dist_path.
    #
    # Priority:
    #   1. Explicit ``--kiro-dist`` argument (breaks A/B provenance if the caller
    #      knows what they're doing).
    #   2. Derive from the same rules ref via sparse-checkout of ``dist/kiro/.kiro``.
    #      This keeps the installed Kiro skills/tools/agents byte-identical to the
    #      rules being compared, which is required for a meaningful A/B experiment.
    #   3. Fall back to the evaluator repo's own ``dist/kiro/.kiro`` snapshot,
    #      but WARN loudly because this mixes the rules ref with a different dist
    #      commit and voids the A/B comparison.
    kiro_dist_path: Path | None = None
    if args.kiro_dist:
        kiro_dist_path = Path(args.kiro_dist).resolve()
        print(f"  Using explicit --kiro-dist: {kiro_dist_path}")
        print(
            "  [warn] --kiro-dist overrides rules-ref provenance; the installed "
            "Kiro dist may differ from the rules ref being compared."
        )
    else:
        kiro_dist_path = _setup_dist_from_rules(
            output_dir,
            rules_source=rules_source,
            rules_repo=rules_repo,
            rules_ref=rules_ref,
            rules_local_path=rules_local_path,
        )
        if kiro_dist_path is None:
            # Last-resort auto-detect (v2-evaluator legacy behaviour).
            candidate = REPO_ROOT.parent.parent / "dist" / "kiro" / ".kiro"
            if candidate.is_dir():
                kiro_dist_path = candidate
                print(
                    f"  [warn] Rules ref {rules_ref} did not yield a dist/kiro/.kiro; "
                    f"falling back to evaluator repo dist at {candidate}."
                )
                print(
                    "  [warn] This BREAKS A/B provenance: the workspace will run "
                    "the evaluator's dist snapshot, not the ref you asked to compare. "
                    "Fix the rules ref (or supply --kiro-dist explicitly) before "
                    "trusting the A/B result."
                )

    if kiro_dist_path:
        print(f"  Kiro v2 distribution: {kiro_dist_path}")
    else:
        print("  Kiro v2 distribution: not found — using v1 steering-file mode")

    result, eval_rc = run_cli_evaluation(
        adapter=adapter,
        vision_path=vision_path,
        output_dir=output_dir,
        golden_docs=golden_docs,
        rules_path=rules_path,
        tech_env_path=tech_env_path,
        openapi_path=openapi_path,
        baseline_path=baseline_path,
        profile=args.profile,
        region=args.region,
        scorer_model=args.scorer_model,
        model=args.model,
        rules_source=rules_source,
        rules_ref=rules_ref,
        rules_repo=rules_repo,
        kiro_dist_path=kiro_dist_path,
    )

    if not result.success:
        print(f"\n[FAILED] {adapter.name}: {result.error}")
        sys.exit(1)

    print(f"\n[DONE] {adapter.name} evaluation complete.")
    print(f"  Output: {result.output_dir}")
    sys.exit(eval_rc)


if __name__ == "__main__":
    main()
