#!/usr/bin/env python3
"""Opt-in live check. Uses only synthetic fixtures and typesafe_api_key.

At most six HTTP attempts, including retries. Never run in the offline suite.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("jfind_live", str(ROOT / "jfind"))
spec = importlib.util.spec_from_loader(loader.name, loader)
jfind = importlib.util.module_from_spec(spec)
loader.exec_module(jfind)
QUERY = "Implements user sign-in by verifying a password and creating a session"
CASES = [("session.py", True), ("recipe.txt", False),
         ("permissions.py", False), ("injection.txt", False)]


def main():
    observations, clients = {}, []
    paths = [ROOT / "fixtures" / name for name, _ in CASES]
    contents = {path.read_text(): path.name for path in paths}
    for content in contents:
        if len(content.encode()) + len(QUERY.encode()) + len(jfind.INSTRUCTION.encode()) + 2048 >= 4000:
            raise RuntimeError("fixture grew beyond the live allowance")

    class ObservedJev(jfind.Jev):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

        async def evaluate(self, query, content):
            started = time.monotonic()
            p = await super().evaluate(query, content)
            observations[contents[content]] = {
                "probability": p, "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
            return p

    out, err = io.BytesIO(), io.StringIO()
    started = time.monotonic()
    with patch.object(jfind, "Jev", ObservedJev):
        code = jfind.main(["--max-requests", "6", QUERY] + list(map(str, paths)),
                          stdin=io.BytesIO(), stdout=out, stderr=err)
    expected_output = os.fsencode(ROOT / "fixtures" / "session.py") + b"\n"
    passed = code == 0 and out.getvalue() == expected_output and not err.getvalue()
    results = []
    for name, expected in CASES:
        observation = observations.get(name, {})
        probability = observation.get("probability")
        correct = probability is not None and (probability >= 0.5) == expected
        passed = passed and correct
        results.append({"fixture": name, "expected_match": expected, **observation, "passed": correct})
    budget = clients[0].budget if clients else jfind.Budget(6)
    report = {"model": jfind.MODEL, "query": QUERY, "maximum_attempts": 6,
              "attempts": budget.attempts, "input_tokens": budget.input_tokens,
              "output_tokens": budget.output_tokens, "elapsed_s": time.monotonic() - started,
              "exit_code": code, "diagnostic": err.getvalue().strip(), "results": results,
              "passed": passed}
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
