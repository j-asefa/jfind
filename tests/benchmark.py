#!/usr/bin/env python3
"""Opt-in live CLI benchmark. Sends fixtures, or this repo's tracked files with --repo.

Each scan allows at most three HTTP attempts per input path in total. The key is inherited
through the environment; reports contain only timings, usage and relative paths.
"""
import argparse
import datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
QUERY = "Implements user sign-in by verifying a password and creating a session"
REPO_QUERY = "Implements bounded concurrency for API requests"
NAMES = ["session.py", "recipe.txt", "permissions.py", "injection.txt"] * 2
INPUT_USD_PER_MILLION = 0.042
PRICE_SOURCE = "https://docs.typesafe.ai/models"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, help="optional original sequential executable")
    parser.add_argument("--previous", type=Path, help="optional pre-SDK executable (four workers)")
    parser.add_argument("--repo", action="store_true", help="search tracked files in this repository")
    parser.add_argument("--rounds", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("typesafe_api_key"):
        parser.error("set typesafe_api_key")
    modes = [("sdk_jobs_1", ROOT / "jfind", ["--jobs", "1", "--stats"]),
             ("sdk_jobs_4", ROOT / "jfind", ["--jobs", "4", "--stats"])]
    if args.previous:
        modes.insert(0, ("previous_jobs_4", args.previous.resolve(), ["--jobs", "4"]))
    if args.baseline:
        modes.insert(0, ("baseline", args.baseline.resolve(), []))
    if args.repo:
        data = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        paths = [Path(os.fsdecode(p)) for p in data.split(b"\0") if p]
        query = REPO_QUERY
        expected = None  # Order/completeness check, not a semantic accuracy oracle.
    else:
        paths = [Path("fixtures") / name for name in NAMES]
        assert all(len((ROOT / path).read_bytes()) < 1000 for path in paths)
        query = QUERY
        expected = [str(path) for path in paths if path.name == "session.py"]
    if not paths:
        parser.error("no input files")
    data = b"".join(os.fsencode(path) + b"\0" for path in paths)
    report = {
        "date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
        "packages": {name: importlib.metadata.version(name) for name in ("typesafe-sdk", "httpx2")},
        "model": "jev-1.13.0", "query": query, "threshold": 0.5,
        "workload": "repository" if args.repo else "fixtures", "input_paths": len(paths),
        "manifest": [{"path": str(path), "bytes": (ROOT / path).stat().st_size,
                      "sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest()}
                     for path in dict.fromkeys(paths)],
        "executables_sha256": {label: hashlib.sha256(executable.read_bytes()).hexdigest()
                               for label, executable, _ in modes},
        "pricing": {"input_usd_per_million": INPUT_USD_PER_MILLION,
                    "output_usd_per_million": 0, "source": PRICE_SOURCE, "checked": "2026-09-18"},
        "maximum_http_attempts": args.rounds * len(modes) * len(paths) * 3, "runs": [],
    }

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    for round_id in range(args.rounds):
        start_index = round_id % len(modes)
        for label, executable, options in modes[start_index:] + modes[:start_index]:
            started = time.perf_counter()
            try:
                result = subprocess.run(
                    [sys.executable, str(executable), "-0", "--max-requests", str(len(paths) * 3)]
                    + options + [query], cwd=ROOT, input=data, capture_output=True, timeout=180,
                )
                elapsed = time.perf_counter() - started
                matched = [os.fsdecode(p) for p in result.stdout.split(b"\0") if p]
                diagnostic, usage = [], None
                for line in result.stderr.decode().splitlines():
                    if line.startswith("jfind: stats "):
                        usage = json.loads(line.removeprefix("jfind: stats "))
                    else:
                        diagnostic.append(line)
                ordered = matched == [str(p) for p in paths if str(p) in matched]
                record = {"round": round_id + 1, "mode": label, "elapsed_s": elapsed,
                          "exit_code": result.returncode,
                          "passed": result.returncode == 0 and not diagnostic and ordered
                                    and (expected is None or matched == expected)
                                    and ("--stats" not in options or usage is not None),
                          "matching_paths": matched, "diagnostic": "\n".join(diagnostic)}
                if usage is not None:
                    record["usage"] = usage
                    record["estimated_cost_usd"] = usage["input_tokens"] * INPUT_USD_PER_MILLION / 1_000_000
            except subprocess.TimeoutExpired:
                record = {"round": round_id + 1, "mode": label, "passed": False,
                          "diagnostic": "CLI exceeded the 180-second benchmark deadline"}
            report["runs"].append(record)
            save()
            print(json.dumps(record), flush=True)
            if not record["passed"]:
                return 1
    report["summary"] = {}
    for label, _, _ in modes:
        values = [r["elapsed_s"] for r in report["runs"] if r["mode"] == label]
        report["summary"][label] = {"median_s": statistics.median(values),
                                    "min_s": min(values), "max_s": max(values)}
    save()
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
