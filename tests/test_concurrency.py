"""Concurrency, cancellation and persistent-connection checks; loopback only."""
import asyncio
import concurrent.futures
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_jfind import answer, jfind


@contextlib.contextmanager
def server(respond):
    class State:
        def __init__(self):
            self.lock = threading.Lock()
            self.seen = []
            self.active = self.peak = 0
            self.closed = threading.Event()

    state = State()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with state.lock:
                state.seen.append((body, self.client_address[1]))
                state.active += 1
                state.peak = max(state.peak, state.active)
            try:
                code, headers, data = respond(self, body, state)
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when the client cancels a request.
            finally:
                with state.lock:
                    state.active -= 1

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        def shutdown_request(self, request):
            super().shutdown_request(request)
            state.closed.set()

        def handle_error(self, *args):
            pass  # A cancelled persistent connection can reset its next read.

    httpd = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        with patch.object(jfind, "BASE_URL", f"http://127.0.0.1:{httpd.server_port}"):
            yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


OK = (200, {}, json.dumps(answer()).encode())


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jfind-concurrency-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {"typesafe_api_key": "synthetic-key"})
        env.start()
        self.addCleanup(env.stop)

    def files(self, count):
        paths = [self.root / f"{i}.txt" for i in range(count)]
        for i, path in enumerate(paths):
            path.write_text(str(i))
        return paths

    def scan(self, paths, *options, stdin=None, stdout=None):
        out = io.BytesIO() if stdout is None else stdout
        err = io.StringIO()
        code = jfind.main(list(options) + ["q"] + list(map(str, paths)),
                          stdin=io.BytesIO() if stdin is None else stdin,
                          stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_four_workers_overlap_reuse_connections_and_bound_ordered_results(self):
        paths = self.files(12)
        first_wave = threading.Barrier(4)
        release_first, others_ready = threading.Event(), threading.Event()
        ready = []

        def respond(handler, body, state):
            i = int(body["state"]["content"])
            if i < 4:
                first_wave.wait(timeout=3)
                if i == 0:
                    release_first.wait(timeout=3)
                else:
                    with state.lock:
                        ready.append(i)
                        if len(ready) == 3:
                            others_ready.set()
            return OK

        out = io.BytesIO()
        with server(respond) as state, concurrent.futures.ThreadPoolExecutor(1) as runner:
            future = runner.submit(self.scan, paths, stdout=out)
            try:
                self.assertTrue(others_ready.wait(3), "default four requests did not overlap")
                self.assertEqual(out.getvalue(), b"", "later results overtook the first file")
                self.assertEqual(len(state.seen), 4, "slow first result allowed unbounded read-ahead")
            finally:
                release_first.set()
            code, output, errors = future.result(timeout=3)
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, b"".join(os.fsencode(p) + b"\n" for p in paths))
        self.assertEqual(state.peak, 4)
        self.assertEqual(len({port for _, port in state.seen}), 4)

    def test_jobs_one_reuses_a_connection_and_preserves_duplicates(self):
        paths = self.files(2)
        paths = paths + paths
        with server(lambda *args: OK) as state:
            code, output, errors = self.scan(paths, "--jobs", "1", "-0")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(output, b"".join(os.fsencode(p) + b"\0" for p in paths))
        self.assertEqual(len(state.seen), 4)
        self.assertEqual(len({port for _, port in state.seen}), 1)

    def test_request_budget_is_shared_with_retries(self):
        paths = self.files(8)
        first_wave = threading.Barrier(2)
        occurrences = {}

        def respond(handler, body, state):
            content = body["state"]["content"]
            with state.lock:
                occurrences[content] = occurrences.get(content, 0) + 1
                first = occurrences[content] == 1
            if first:
                first_wave.wait(timeout=3)
                return 429, {"Retry-After": "0"}, b"{}"
            return OK

        with server(respond) as state:
            code, _, errors = self.scan(paths, "--jobs", "2", "--max-requests", "3")
        self.assertEqual(code, 1)
        self.assertIn("request limit reached", errors)
        self.assertLessEqual(len(state.seen), 3)
        self.assertEqual(set(occurrences), {"0", "1"})

    def test_authentication_failure_interrupts_an_earlier_request_and_stops_dispatch(self):
        paths = self.files(8)
        first_started, release_first = threading.Event(), threading.Event()

        def respond(handler, body, state):
            if body["state"]["content"] == "0":
                first_started.set()
                release_first.wait(5)
                return OK
            first_started.wait(3)
            return 401, {}, b"private remote detail"

        with server(respond) as state, concurrent.futures.ThreadPoolExecutor(1) as runner:
            future = runner.submit(self.scan, paths, "--jobs", "2")
            try:
                code, output, errors = future.result(timeout=3)
            finally:
                release_first.set()
        self.assertEqual((code, output), (1, b""))
        self.assertIn("authentication failed", errors)
        self.assertNotIn("private remote detail", errors)
        self.assertEqual(len(state.seen), 2)

    def test_streaming_output_and_cancellation_do_not_wait_for_stdin_eof(self):
        paths = self.files(1)
        printed, closed = threading.Event(), threading.Event()

        class Output(io.BytesIO):
            def write(self, data):
                count = super().write(data)
                printed.set()
                return count

        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as source, server(lambda *args: OK) as state:
            with patch.object(jfind, "pipe_closed", side_effect=lambda _: closed.is_set()):
                with concurrent.futures.ThreadPoolExecutor(1) as runner:
                    future = runner.submit(self.scan, [], stdin=source, stdout=Output())
                    try:
                        os.write(writer, os.fsencode(paths[0]) + b"\n")
                        self.assertTrue(printed.wait(3), "output waited for more input/EOF")
                        closed.set()
                        code, output, errors = future.result(timeout=3)
                    finally:
                        os.close(writer)
        self.assertEqual((code, output, errors), (0, os.fsencode(paths[0]) + b"\n", ""))
        self.assertEqual(len(state.seen), 1)
        self.assertFalse(any(t.name == "jfind-input" for t in threading.enumerate()))

    def test_cancellation_interrupts_retry_after(self):
        paths = self.files(1)
        attempted, closed = threading.Event(), threading.Event()

        def respond(*args):
            attempted.set()
            return 429, {"Retry-After": "20"}, b"{}"

        with server(respond) as state, patch.object(jfind, "pipe_closed", side_effect=lambda _: closed.is_set()):
            with concurrent.futures.ThreadPoolExecutor(1) as runner:
                future = runner.submit(self.scan, paths)
                self.assertTrue(attempted.wait(3))
                closed.set()
                code, output, errors = future.result(timeout=3)
        self.assertEqual((code, output, errors), (0, b"", ""))
        self.assertEqual(len(state.seen), 1)

    def test_stale_keepalive_connection_reconnects(self):
        def respond(handler, body, state):
            if len(state.seen) == 1:
                handler.close_connection = True
            return OK

        async def run(state):
            async with contextlib.aclosing(jfind.Jev("synthetic-key", 3)) as client:
                self.assertEqual(await client.evaluate("q", "first"), 0.95)
                self.assertTrue(await asyncio.to_thread(state.closed.wait, 3))
                self.assertEqual(await client.evaluate("q", "second"), 0.95)
            # The pool may detect the closed socket before sending, avoiding a retry.
            self.assertIn(client.attempts, (2, 3))

        with server(respond) as state:
            asyncio.run(run(state))
        self.assertEqual(len(state.seen), 2)
        self.assertEqual(len({port for _, port in state.seen}), 2)

    def test_invalid_response_does_not_poison_later_requests(self):
        def respond(handler, body, state):
            return (200, {}, b"x" * 70000) if len(state.seen) == 1 else OK

        async def run():
            async with contextlib.aclosing(jfind.Jev("synthetic-key", 3)) as client:
                with self.assertRaisesRegex(jfind.Failure, "invalid TypeSafe response"):
                    await client.evaluate("q", "first")
                self.assertEqual(await client.evaluate("q", "second"), 0.95)
            self.assertEqual(client.attempts, 2)

        with server(respond) as state:
            asyncio.run(run())
        self.assertEqual(len(state.seen), 2)

    def test_file_timeout_is_reported_without_stalling_later_results(self):
        paths = self.files(2)
        read_text = jfind.read_text

        def read(path):
            if path == os.fsencode(paths[0]):
                raise TimeoutError("synthetic file I/O timeout")
            return read_text(path)

        with server(lambda *args: OK), patch.object(jfind, "read_text", side_effect=read):
            with concurrent.futures.ThreadPoolExecutor(1) as runner:
                result = runner.submit(self.scan, paths).result(timeout=3)
        code, output, errors = result
        self.assertEqual((code, output), (1, os.fsencode(paths[1]) + b"\n"))
        self.assertIn("cannot read file safely", errors)

    def test_keyboard_interrupt_closes_active_workers(self):
        paths = self.files(2)
        other_started, release_other = threading.Event(), threading.Event()

        def respond(handler, body, state):
            if body["state"]["content"] == "0":
                other_started.wait(3)
            else:
                other_started.set()
                release_other.wait(5)
            return OK

        class Output(io.BytesIO):
            def write(self, data):
                raise KeyboardInterrupt()

        with server(respond), concurrent.futures.ThreadPoolExecutor(1) as runner:
            future = runner.submit(self.scan, paths, stdout=Output())
            try:
                with self.assertRaises(KeyboardInterrupt):
                    future.result(timeout=3)
            finally:
                release_other.set()

    def test_stats_report_usage_across_workers_and_retries(self):
        paths = self.files(4)

        def respond(handler, body, state):
            with state.lock:
                if not getattr(state, "retried", False):
                    state.retried = True
                    return 429, {"Retry-After": "0"}, b"{}"
            return OK

        with server(respond) as state:
            code, output, errors = self.scan(paths, "--stats", "--max-requests", "5")
        self.assertEqual(code, 0)
        self.assertEqual(len(output.splitlines()), 4)
        self.assertTrue(errors.startswith("jfind: stats "))
        self.assertEqual(json.loads(errors.removeprefix("jfind: stats ")), {
            "http_attempts": 5, "evaluated_files": 4, "input_tokens": 168, "output_tokens": 8,
        })
        self.assertEqual(len(state.seen), 5)


if __name__ == "__main__":
    unittest.main()
