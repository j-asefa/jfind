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
    observations = []

    class ObservedJev(jfind.Jev):
        def evaluate(self, query, content):
            p = super().evaluate(query, content)
            observations.append(p)
            return p

    client = ObservedJev(os.environ.get("typesafe_api_key"), max_requests=6)
    # Each synthetic request is below 4,000 conservatively estimated input tokens;
    # six attempts reserve less than 24,000 input tokens for the entire check.
    results = []
    for name, expected in CASES:
        path = ROOT / "fixtures" / name
        if len(path.read_bytes()) + len(QUERY.encode()) + len(jfind.INSTRUCTION.encode()) + 2048 >= 4000:
            raise RuntimeError("fixture grew beyond the predeclared live allowance")
        out, err = io.BytesIO(), io.StringIO()
        count = len(observations)
        started = time.monotonic()
        with patch.object(jfind, "Jev", return_value=client):
            code = jfind.main([QUERY, str(path)], stdin=io.BytesIO(), stdout=out, stderr=err)
        expected_bytes = os.fsencode(path) + b"\n" if expected else b""
        results.append({"fixture": name, "expected_match": expected,
                        "observed_match": bool(out.getvalue()), "exit_code": code,
                        "probability": observations[-1] if len(observations) > count else None,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "passed": code == 0 and out.getvalue() == expected_bytes,
                        "diagnostic": err.getvalue().strip()})
    report = {"model": jfind.MODEL, "query": QUERY, "maximum_attempts": 6,
              "attempts": client.attempts, "input_tokens": client.input_tokens,
              "output_tokens": client.output_tokens, "results": results}
    print(json.dumps(report, indent=2))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
