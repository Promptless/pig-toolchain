"""Opt-in, paid model smoke test of synthetic constraints; never reads local sessions.

Run: uv run python scripts/evaluate_token_efficiency.py --model MODEL_ID
Runs four requests, each containing the same 12 independent tasks. This is a
constraint smoke test, not a quality benchmark or native plugin acceptance test.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from promptless_instruction_hub.token_efficiency import CONCISE_INSTRUCTION, compact_markdown


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="explicit Claude model ID for reproducibility")
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/token-efficiency/cases.json"
    cases = json.loads(fixture.read_text())
    any_failures = False
    with tempfile.TemporaryDirectory(prefix="pig-token-eval-") as directory:
        for mode in ("baseline", "compact", "concise", "both"):
            inputs = [
                {
                    "id": case["id"],
                    "prompt": compact_markdown(case["prompt"]) if mode in ("compact", "both") else case["prompt"],
                }
                for case in cases
            ]
            prompt = "Answer each independent task. Return only a JSON object mapping each id to its answer.\n"
            prompt += json.dumps(inputs)
            system = "Follow task instructions exactly."
            if mode in ("concise", "both"):
                system += "\n" + CONCISE_INSTRUCTION
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--model",
                    args.model,
                    "--safe-mode",
                    "--tools",
                    "",
                    "--disable-slash-commands",
                    "--setting-sources",
                    "",
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                    "--system-prompt",
                    system,
                    "--output-format",
                    "json",
                    "--no-session-persistence",
                    "--max-budget-usd",
                    "2",
                ],
                input=prompt,
                text=True,
                capture_output=True,
                cwd=directory,
                timeout=180,
                check=True,
            )
            data = json.loads(result.stdout)
            if isinstance(data, list):
                data = next(event for event in reversed(data) if event.get("type") == "result")
            if data.get("is_error"):
                raise RuntimeError(f"{mode}: Claude returned an error result")
            answer = data["result"].strip()
            if answer.startswith("```"):
                answer = "\n".join(answer.splitlines()[1:-1])
            answers = json.loads(answer)
            failures = [case["id"] for case in cases if answers.get(case["id"]) != case["expected"]]
            any_failures |= bool(failures)
            print(
                json.dumps(
                    {
                        "mode": mode,
                        "passed": len(cases) - len(failures),
                        "total": len(cases),
                        "failed_ids": failures,
                        "requested_model": args.model,
                        "models": sorted(data.get("modelUsage", {})),
                        "usage": data.get("usage"),
                        "cost_usd": data.get("total_cost_usd"),
                    }
                ),
                flush=True,
            )
    if any_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
