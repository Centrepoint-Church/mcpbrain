"""Eval graders for mcpbrain capability tests.

Ported from ops-brain evals/graders.py with adaptations:
- Logger: mcpbrain.evals
- CodeGrader._get_tool: falls back to mcpbrain.mcp_server (not src/mcp_server)
- ModelJudgeGrader: calls the subscription `claude` CLI (headless `-p`), NOT the
  Anthropic SDK / ANTHROPIC_API_KEY — mcpbrain runs on a Claude subscription.
  Skips cleanly when the CLI is unavailable.

Two grader types:
  CodeGrader       — deterministic assertion-based grading (verbatim from ops-brain)
  ModelJudgeGrader — LLM scores output against a rubric (pass@k); skips with no CLI
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

log = logging.getLogger("mcpbrain.evals")


@dataclass
class EvalResult:
    id: str
    passed: bool
    failures: list[str]
    output_sample: str
    judge_reasons: list[str] = field(default_factory=list)
    skipped: bool = False


def _check_assertion(output: str, assertion: dict, elapsed_ms: float | None = None) -> tuple[bool, str]:
    """Check a single assertion dict against a string output.

    Returns (passed, failure_reason). failure_reason is empty string if passed.
    elapsed_ms is the tool call duration in milliseconds, used for latency assertions.
    """
    for key, value in assertion.items():
        if key == "contains":
            if value.lower() not in output.lower():
                return False, f"expected output to contain '{value}'"
        elif key == "not_contains":
            if value.lower() in output.lower():
                return False, f"expected output NOT to contain '{value}'"
        elif key == "min_length":
            if len(output) < value:
                return False, f"expected length >= {value}, got {len(output)}"
        elif key == "max_length":
            if len(output) > value:
                return False, f"expected length <= {value}, got {len(output)}"
        elif key == "starts_with":
            if not output.startswith(value):
                return False, f"expected output to start with '{value}'"
        elif key == "regex":
            if not re.search(value, output):
                return False, f"expected output to match regex '{value}'"
        elif key == "section_present":
            pattern = rf"#+\s+{re.escape(value)}"
            if not re.search(pattern, output, re.IGNORECASE):
                return False, f"expected markdown heading matching '{value}'"
        elif key == "line_count_min":
            lines = [line for line in output.splitlines() if line.strip()]
            if len(lines) < value:
                return False, f"expected >= {value} non-empty lines, got {len(lines)}"
        elif key == "latency_ms_max":
            if elapsed_ms is None:
                return False, "latency_ms_max assertion requires timing data (not available in this context)"
            if elapsed_ms > value:
                return False, f"expected latency <= {value}ms, got {elapsed_ms:.0f}ms"
        elif key == "equals":
            if output.strip() != str(value).strip():
                return False, f"expected output '{value}', got '{output.strip()[:80]}'"
        elif key == "min_score":
            scores = re.findall(r"Score:\s*([\d.]+)", output)
            if not scores:
                return False, f"min_score: no Score: values found in output (threshold {value})"
            max_score = max(float(s) for s in scores)
            if max_score < value:
                return False, f"min_score: top score {max_score:.4f} < threshold {value}"
        else:
            return False, f"unknown assertion key '{key}'"
    return True, ""


class CodeGrader:
    """Deterministic grader that runs a tool function and checks assertions."""

    def __init__(self, tool_registry: dict | None = None):
        """tool_registry maps tool name -> callable.

        If None, falls back to looking up the tool in mcpbrain.mcp_server.
        Pass a registry in tests to avoid importing the full MCP server.
        """
        self._registry = tool_registry

    def _get_tool(self, name: str):
        if self._registry is not None:
            fn = self._registry.get(name)
            if fn is None:
                raise ValueError(f"Tool '{name}' not in registry")
            return fn

        import mcpbrain.mcp_server as mcp_server
        fn = getattr(mcp_server, name, None)
        if fn is None:
            raise ValueError(f"Tool '{name}' not found in mcpbrain.mcp_server")
        return fn

    def run(self, case: dict) -> EvalResult:
        import time
        tool_fn = self._get_tool(case["tool"])
        t0 = time.monotonic()
        try:
            output = tool_fn(**case.get("input", {}))
        except Exception as e:
            output = f"TOOL ERROR: {e}"
        elapsed_ms = (time.monotonic() - t0) * 1000

        failures = []
        for assertion in case.get("assertions", []):
            ok, reason = _check_assertion(str(output), assertion, elapsed_ms=elapsed_ms)
            if not ok:
                failures.append(reason)

        return EvalResult(
            id=case["id"],
            passed=len(failures) == 0,
            failures=failures,
            output_sample=str(output)[:500],
        )


def _claude_cli() -> str | None:
    """Locate the subscription `claude` CLI, or None if unavailable.

    mcpbrain runs on a Claude subscription (no ANTHROPIC_API_KEY) — LLM calls go
    through the `claude` CLI in headless `-p` mode, the same path the enrichment
    cadences use. Returns None (→ grader skips) when the CLI is absent.
    """
    try:
        from mcpbrain.config import find_claude
        return find_claude()
    except Exception:
        return None


_JUDGE_PROMPT = """\
You are evaluating whether an AI tool's output satisfies a quality rubric.

TOOL: {tool}
INPUT: {input}
OUTPUT:
{output}

RUBRIC:
{rubric}

Does the output satisfy ALL rubric criteria?
Respond with exactly one word on the first line: PASS or FAIL.
On the second line, explain your reasoning in one sentence.
"""


class ModelJudgeGrader:
    """LLM-based grader. Runs the tool once and judges k times.

    Passes if >= pass_threshold fraction of k judge runs return PASS.
    Default model is claude-haiku-4-5-20251001 for cost efficiency.

    Calls the subscription `claude` CLI (headless `-p`). Skips cleanly (SKIP
    result, passed=True) when the CLI is unavailable, rather than raising.
    """

    def __init__(self, model: str = "claude-haiku-4-5-20251001",
                 tool_registry: dict | None = None):
        self.model = model
        self._registry = tool_registry

    def _get_tool(self, name: str):
        if self._registry is not None:
            fn = self._registry.get(name)
            if fn is None:
                raise ValueError(f"Tool '{name}' not in registry")
            return fn
        import mcpbrain.mcp_server as mcp_server
        fn = getattr(mcp_server, name, None)
        if fn is None:
            raise ValueError(f"Tool '{name}' not found in mcpbrain.mcp_server")
        return fn

    def _judge(self, case: dict, output: str) -> tuple[str, str]:
        """Call the judge model once via the claude CLI. Returns (verdict, reason)."""
        import json
        import subprocess

        cli = _claude_cli()
        if not cli:
            return "SKIP", "claude CLI not found — LLM judge skipped"

        prompt = _JUDGE_PROMPT.format(
            tool=case.get("tool", ""),
            input=json.dumps(case.get("input", {})),
            output=output[:3000],
            rubric=case.get("rubric", ""),
        )
        try:
            # Headless print mode; prompt on stdin to avoid arg-length/escaping.
            # --model selects Haiku for cost; no tools needed for a text judge.
            proc = subprocess.run(
                [cli, "-p", "--model", self.model],
                input=prompt, capture_output=True, text=True, timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 — CLI missing/slow → skip, don't crash the suite
            return "SKIP", f"claude CLI invocation failed: {exc}"
        if proc.returncode != 0:
            return "SKIP", f"claude CLI exit {proc.returncode}: {(proc.stderr or '')[:200]}"

        text = (proc.stdout or "").strip()
        lines = text.splitlines()
        verdict = lines[0].strip().upper() if lines else "FAIL"
        reason = lines[1].strip() if len(lines) > 1 else text
        if verdict not in ("PASS", "FAIL"):
            verdict = "FAIL"
        return verdict, reason

    def run(self, case: dict) -> EvalResult:
        if not _claude_cli():
            log.info("ModelJudgeGrader: claude CLI unavailable — skipping %s", case.get("id"))
            return EvalResult(
                id=case.get("id", "unknown"),
                passed=True,
                failures=[],
                output_sample="",
                judge_reasons=["SKIP: claude CLI not found"],
                skipped=True,
            )

        tool_fn = self._get_tool(case["tool"])
        try:
            output = tool_fn(**case.get("input", {}))
        except Exception as e:
            output = f"TOOL ERROR: {e}"

        k = case.get("k", 3)
        threshold = case.get("pass_threshold", 1.0)
        passes = 0
        reasons = []

        for _ in range(k):
            verdict, reason = self._judge(case, str(output))
            if verdict == "PASS":
                passes += 1
            reasons.append(f"{verdict}: {reason}")

        passed = (passes / k) >= threshold
        failures = [] if passed else [
            f"{passes}/{k} judge runs passed (threshold {threshold:.0%})"
        ]

        return EvalResult(
            id=case.get("id", "unknown"),
            passed=passed,
            failures=failures,
            output_sample=str(output)[:500],
            judge_reasons=reasons,
        )
