"""Offline checks: python3 -m unittest discover -s tests -v"""
import asyncio
import concurrent.futures
import contextlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx2

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("jfind", str(ROOT / "jfind"))
spec = importlib.util.spec_from_loader(loader.name, loader)
jfind = importlib.util.module_from_spec(spec)
loader.exec_module(jfind)


def answer(p=0.95):
    return {"model": jfind.MODEL, "answers": {"matches": {"type": "noul", "noul": p}},
            "usage": {"input_tokens": 42, "output_tokens": 2}}


@contextlib.contextmanager
def server(responses):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            code, headers, body = responses[min(len(seen) - 1, len(responses) - 1)]
            self.send_response(code)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        with patch.object(jfind, "BASE_URL", f"http://127.0.0.1:{httpd.server_port}"):
            yield seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jfind-minimal-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def file(self, name="source.txt", text="ordinary text"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_search(self, args, data=b"", probabilities=(0.95,)):
        fake = Mock(evaluate=AsyncMock(), aclose=AsyncMock())
        fake.evaluate.side_effect = probabilities
        output, errors = io.BytesIO(), io.StringIO()
        with patch.object(jfind, "Jev", return_value=fake):
            code = jfind.main(["--jobs", "1"] + args, stdin=io.BytesIO(data), stdout=output, stderr=errors)
        return code, output.getvalue(), errors.getvalue(), fake

    def test_explicit_root_ignores_stdin_and_prints_only_matches(self):
        a, b = self.file("a.txt"), self.file("b.txt")
        code, out, err, fake = self.run_search(["query", str(a), str(b)], b"/not/a/path\n", (0.99, 0.01))
        self.assertEqual((code, out, err), (0, os.fsencode(a) + b"\n", ""))
        self.assertEqual(fake.evaluate.call_count, 2)

    def test_recursive_directory_search(self):
        a = self.file("nested/a.txt")
        self.file(".hidden.txt")
        self.file("node_modules/package.txt")
        code, out, _, fake = self.run_search(["query", str(self.root)])
        self.assertEqual((code, out), (0, os.fsencode(a) + b"\n"))
        fake.evaluate.assert_called_once()

    def test_terminal_stdin_defaults_to_current_directory(self):
        source = io.BytesIO()
        source.isatty = lambda: True
        report = Mock()
        with patch.object(jfind.os, "lstat", side_effect=FileNotFoundError):
            self.assertEqual(list(jfind.candidates([], source, False, report)), [])
        report.assert_called_once_with(b".", "cannot read path metadata")

    def test_empty_pipe_and_no_matches_succeed_like_find(self):
        code, out, err, fake = self.run_search(["query"])
        self.assertEqual((code, out, err), (0, b"", ""))
        fake.evaluate.assert_not_called()
        code, out, err, _ = self.run_search(["query", str(self.file())], probabilities=(0.0,))
        self.assertEqual((code, out, err), (0, b"", ""))

    def test_nul_pipeline_preserves_unusual_path_bytes_and_order(self):
        names = [b"space tab\t'quote.txt", b"-dash.txt", b"line\nname.txt", "café.txt".encode()]
        if sys.platform != "darwin":  # APFS requires UTF-8 filenames.
            names.append(b"invalid-\xff.txt")
        paths = [os.fsencode(self.root) + b"/" + name for name in names]
        for path in paths:
            with open(path, "wb") as f:
                f.write(b"synthetic text")
        data = b"\0".join(paths)  # The last record intentionally has no delimiter.
        code, out, err, _ = self.run_search(["-0", "query"], data, (0.9,) * len(paths))
        self.assertEqual((code, out, err), (0, data + b"\0", ""))

    def test_newline_framing_keeps_spaces_and_duplicates(self):
        path = os.fsencode(self.file(" padded "))
        data = path + b"\n\n" + path
        code, out, _, fake = self.run_search(["query"], data, (0.9, 0.9))
        self.assertEqual((code, out), (0, path + b"\n" + path + b"\n"))
        self.assertEqual(fake.evaluate.call_count, 2)

    def test_input_yields_before_pipe_eof(self):
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as stream:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            records = jfind.path_records(stream, b"\n")
            future = pool.submit(next, records)
            try:
                os.write(writer, b"one.txt\n")
                self.assertEqual(future.result(timeout=1), b"one.txt")
            finally:
                os.close(writer)
                pool.shutdown()

    def test_binary_empty_credentials_symlinks_and_fifo_are_not_sent(self):
        paths = [self.file("empty.txt", ""), self.file("binary.txt", "a\0b"),
                 self.file("secrets.json"), self.file(".env"), self.file("a.key"),
                 self.file("ordinary.txt", "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret"),
                 self.root / "symlink", self.root / "pipe"]
        paths[-2].symlink_to(paths[0])
        os.mkfifo(paths[-1])
        code, out, err, fake = self.run_search(["query"] + list(map(str, paths)))
        self.assertEqual((code, out, err), (0, b"", ""))
        fake.evaluate.assert_not_called()
        link = self.root / "directory-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            jfind.open_regular(os.fsencode(link / "empty.txt"))

    def test_oversized_missing_and_unreadable_are_errors(self):
        large = self.file("large.txt", "x" * (jfind.MAX_FILE_BYTES + 1))
        code, out, err, fake = self.run_search(["query", str(large), str(self.root / "missing")])
        self.assertEqual((code, out), (1, b""))
        self.assertIn("24 KiB", err)
        self.assertIn("metadata", err)
        fake.evaluate.assert_not_called()
        path = self.file()
        with patch.object(jfind, "open_regular", side_effect=PermissionError):
            code, _, err, fake = self.run_search(["query", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)
        fake.evaluate.assert_not_called()

    def test_bom_is_removed_and_changed_files_fail(self):
        path = self.file(text="\ufefforiginal")
        self.assertEqual(jfind.read_text(os.fsencode(path)), "original")
        real_open = jfind.open_regular

        @contextlib.contextmanager
        def mutate_on_open(raw):
            with real_open(raw) as file:
                path.write_text("modified")
                yield file

        with patch.object(jfind, "open_regular", mutate_on_open), self.assertRaises(jfind.Failure):
            jfind.read_text(os.fsencode(path))

    def test_private_key_filter_does_not_skip_its_own_source(self):
        self.assertIsNotNone(jfind.read_text(os.fsencode(ROOT / "jfind")))
        fragments = 'Checks "-----BEGIN " and "PRIVATE KEY-----" as separate strings.'
        self.assertEqual(jfind.read_text(os.fsencode(self.file(text=fragments))), fragments)
        for kind in ("", "RSA ", "ENCRYPTED ", "OPENSSH "):
            marker = "-----BEGIN " + kind + "PRIVATE KEY-----"
            path = self.file(text='embedded_key = "' + marker + '\\nsynthetic"')
            self.assertIsNone(jfind.read_text(os.fsencode(path)))

    def test_threshold_boundary_and_invalid_arguments(self):
        a, b = self.file("a.txt"), self.file("b.txt")
        code, out, _, _ = self.run_search(["--threshold", "0.9", "q", str(a), str(b)], probabilities=(0.9, 0.899))
        self.assertEqual((code, out), (0, os.fsencode(a) + b"\n"))
        for args in (["q", "--threshold", "nan"], ["q", "--max-requests", "0"],
                     ["q", "--jobs", "0"], ["q", "--jobs", "33"], [" "]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                jfind.main(args)
            self.assertEqual(raised.exception.code, 2)

    def test_closed_output_does_not_dispatch_or_log_an_error(self):
        path = self.file()
        with patch.object(jfind, "pipe_closed", return_value=True):
            code, out, err, fake = self.run_search(["q", str(path)])
        self.assertEqual((code, out, err), (0, b"", ""))
        fake.evaluate.assert_not_called()
        output = Mock()
        output.fileno.side_effect = OSError
        output.isatty.return_value = False
        output.write.side_effect = BrokenPipeError
        fake = Mock(evaluate=AsyncMock(), aclose=AsyncMock())
        fake.evaluate.return_value = 1.0
        errors = io.StringIO()
        with patch.object(jfind, "Jev", return_value=fake), self.assertRaises(BrokenPipeError):
            jfind.main(["q", str(path)], stdout=output, stderr=errors)
        self.assertEqual(errors.getvalue(), "")

    def test_missing_key_is_actionable_and_help_needs_no_key(self):
        env = dict(os.environ)
        env.pop("typesafe_api_key", None)
        for arg in ("--help", "--version"):
            result = subprocess.run([sys.executable, str(ROOT / "jfind"), arg], cwd=self.root, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0)
        result = subprocess.run([sys.executable, str(ROOT / "jfind"), "q", str(self.file())], env=env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"set typesafe_api_key", result.stderr)

    def test_bad_path_input_is_bounded_and_never_becomes_content(self):
        code, _, err, fake = self.run_search(["q"], b"x" * (jfind.MAX_PATH_BYTES + 1))
        self.assertEqual(code, 1)
        self.assertIn("64 KiB", err)
        fake.evaluate.assert_not_called()
        code, _, err, fake = self.run_search(["q"], b"not\0a\0newline\0list")
        self.assertEqual(code, 1)
        self.assertIn("use -0", err)
        fake.evaluate.assert_not_called()


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_wire_contract_and_usage(self):
        with server([(200, {}, json.dumps(answer()).encode())]) as seen:
            async with contextlib.aclosing(jfind.Jev("synthetic-test-key", 3)) as client:
                self.assertEqual(await client.evaluate("implements sign-in", "verify a password"), 0.95)
        self.assertEqual(len(seen), 1)
        path, headers, body = seen[0]
        self.assertEqual(path, "/v1/systemone")
        self.assertEqual(headers["Authorization"], "Bearer synthetic-test-key")
        self.assertEqual(body["state"], {"content": "verify a password"})
        self.assertEqual(body["questions"]["matches"]["instructions"]["retrieval_criterion"], "implements sign-in")
        self.assertEqual((client.attempts, client.budget.input_tokens), (1, 42))

    async def test_authentication_and_redirects_are_not_retried(self):
        for code in (401, 403, 302, 422):
            with server([(code, {"Location": "/elsewhere"}, b"private echoed content")]) as seen:
                async with contextlib.aclosing(jfind.Jev("synthetic", 3)) as client:
                    with self.assertRaises(jfind.Failure) as raised:
                        await client.evaluate("q", "text")
            self.assertEqual(len(seen), 1)
            self.assertNotIn("private", str(raised.exception))
            self.assertNotIn("synthetic", str(raised.exception))

    async def test_retry_after_and_budget_count_attempts(self):
        responses = [(429, {"Retry-After": "0"}, b""), (200, {}, json.dumps(answer()).encode())]
        with server(responses) as seen:
            async with contextlib.aclosing(jfind.Jev("synthetic", 2)) as client:
                self.assertEqual(await client.evaluate("q", "text"), 0.95)
                self.assertEqual(client.attempts, 2)
                self.assertEqual(client.budget.evaluated, 1)
        self.assertEqual(len(seen), 2)
        with server(responses) as seen:
            async with contextlib.aclosing(jfind.Jev("synthetic", 1)) as client:
                with self.assertRaises(jfind.StopSearch):
                    await client.evaluate("q", "text")
        self.assertEqual(len(seen), 1)
        with server([(529, {"Retry-After": "60"}, b"")]) as seen:
            async with contextlib.aclosing(jfind.Jev("synthetic", 3)) as client:
                with self.assertRaises(jfind.Failure):
                    await client.evaluate("q", "text")
        self.assertEqual(len(seen), 1)

    async def test_malformed_probabilities_and_models_are_rejected(self):
        values = [answer(p) for p in (-0.1, 1.1, True, "0.9", float("nan"))]
        values += [{}, [], {**answer(), "model": "unexpected"}, {**answer(), "usage": {}}]
        values += [{**answer(), "usage": {"input_tokens": -1, "output_tokens": 1}},
                   {**answer(), "answers": {"other": answer()["answers"]["matches"]}}]
        for value in values:
            transport = httpx2.MockTransport(lambda request: httpx2.Response(200, content=json.dumps(value)))
            async with contextlib.aclosing(jfind.Jev("synthetic", 3, transport=transport)) as client:
                with self.assertRaises(jfind.Failure):
                    await client.evaluate("q", "text")
                self.assertEqual(client.budget.evaluated, 0)

    async def test_transport_retries_are_finite_and_redacted(self):
        requests = []

        def fail(request):
            requests.append(request)
            raise httpx2.ConnectError("synthetic secret echoed")

        async with contextlib.aclosing(jfind.Jev("synthetic", 10, transport=httpx2.MockTransport(fail))) as client:
            with self.assertRaises(jfind.Failure) as raised:
                await client.evaluate("q", "text")
        self.assertEqual(client.attempts, 3)
        self.assertNotIn("synthetic", str(raised.exception))
        self.assertTrue(all(request.extensions["timeout"]["read"] == 20 for request in requests))

    async def test_context_limit_fails_before_dispatch(self):
        transport = httpx2.MockTransport(lambda _: self.fail("oversized request was sent"))
        async with contextlib.aclosing(jfind.Jev("synthetic", 10, transport=transport)) as client:
            with self.assertRaises(jfind.Failure):
                await client.evaluate("q", "界" * 11_000)
        self.assertEqual(client.attempts, 0)

    async def test_cancelling_sdk_retry_wait_stops_further_attempts(self):
        attempted = asyncio.Event()

        def respond(request):
            attempted.set()
            return httpx2.Response(429, headers={"Retry-After": "20"}, json={})

        async with contextlib.aclosing(jfind.Jev("synthetic", 3, transport=httpx2.MockTransport(respond))) as client:
            task = asyncio.create_task(client.evaluate("q", "text"))
            await asyncio.wait_for(attempted.wait(), 1)
            await asyncio.sleep(0)
            self.assertFalse(task.done(), "the SDK should be waiting before retrying")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertEqual(client.attempts, 1)

    async def test_socks_proxy_environment_respects_no_proxy(self):
        for proxy_name, bypass_name in (("ALL_PROXY", "NO_PROXY"), ("all_proxy", "no_proxy")):
            with self.subTest(proxy_name=proxy_name):
                env = {proxy_name: "socks5://127.0.0.1:1", bypass_name: "127.0.0.1"}
                with patch.dict(os.environ, env, clear=True), server([
                        (200, {}, json.dumps(answer()).encode())]) as seen:
                    async with contextlib.aclosing(jfind.Jev("synthetic", 1)) as client:
                        self.assertEqual(await client.evaluate("q", "text"), 0.95)
                self.assertEqual(len(seen), 1)

    async def test_https_proxy_receives_no_origin_api_key(self):
        seen = []

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_CONNECT(self):
                seen.append((self.path, self.headers))
                self.send_error(502)

            def log_message(self, *args):
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        proxy = f"http://user:password@127.0.0.1:{httpd.server_port}"
        try:
            with patch.dict(os.environ, {"https_proxy": proxy, "HTTPS_PROXY": proxy,
                                         "all_proxy": "socks5://127.0.0.1:1",
                                         "ALL_PROXY": "socks5://127.0.0.1:1",
                                         "no_proxy": "", "NO_PROXY": ""}):
                async with contextlib.aclosing(jfind.Jev("synthetic-key", 1)) as client:
                    with self.assertRaises(jfind.RequestLimit):
                        await client.evaluate("q", "text")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()
        self.assertEqual(len(seen), 1)
        destination, headers = seen[0]
        self.assertEqual(destination, "api.typesafe.ai:443")
        self.assertEqual(headers["Proxy-Authorization"], "Basic dXNlcjpwYXNzd29yZA==")
        self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
