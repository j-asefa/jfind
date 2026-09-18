"""Response limits must apply while reading, before the SDK buffers or decodes."""
import asyncio
import contextlib
import gzip
import json
import unittest

import httpx2

from test_jfind import answer, jfind


LIMIT = 64 * 1024


def padded_answer(size):
    value = {**answer(), "padding": ""}
    value["padding"] = "x" * (size - len(json.dumps(value).encode()))
    return json.dumps(value).encode()


class TrackedStream(httpx2.AsyncByteStream):
    def __init__(self, body, chunk_size=1024):
        self.body = body
        self.chunk_size = chunk_size
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for start in range(0, len(self.body), self.chunk_size):
            chunk = self.body[start:start + self.chunk_size]
            self.consumed += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_success_and_error_bodies_stop_reading_and_allow_recovery(self):
        for status in (200, 422, 429, 500):
            for headers in ({}, {"Content-Length": "1"}):
                with self.subTest(status=status, headers=headers):
                    stream = TrackedStream(padded_answer(2 * 1024 * 1024))
                    responses = iter([
                        httpx2.Response(status, headers=headers, stream=stream),
                        httpx2.Response(200, json=answer()),
                    ])
                    transport = httpx2.MockTransport(lambda _: next(responses))
                    async with contextlib.aclosing(jfind.Jev("synthetic", 3, transport=transport)) as client:
                        with self.assertRaisesRegex(jfind.Failure, "invalid TypeSafe response"):
                            await client.evaluate("q", "first")
                        self.assertLessEqual(stream.consumed, LIMIT + stream.chunk_size)
                        self.assertTrue(stream.closed)
                        self.assertEqual(client.attempts, 1, "oversized bodies must not be retried")
                        self.assertEqual(client.budget.evaluated, 0)
                        self.assertEqual(client.budget.input_tokens, 0)
                        self.assertEqual(await client.evaluate("q", "second"), 0.95)
                        self.assertEqual(client.attempts, 2)

    async def test_response_size_boundary_for_streamed_and_buffered_bodies(self):
        for size in (LIMIT - 1, LIMIT, LIMIT + 1):
            for buffered in (False, True):
                with self.subTest(size=size, buffered=buffered):
                    body = padded_answer(size)
                    self.assertEqual(len(body), size)
                    stream = TrackedStream(body)
                    response = (httpx2.Response(200, content=body) if buffered
                                else httpx2.Response(200, stream=stream))
                    transport = httpx2.MockTransport(lambda _: response)
                    async with contextlib.aclosing(jfind.Jev("synthetic", 3, transport=transport)) as client:
                        if size <= LIMIT:
                            self.assertEqual(await client.evaluate("q", "text"), 0.95)
                            self.assertEqual(client.budget.evaluated, 1)
                        else:
                            with self.assertRaisesRegex(jfind.Failure, "invalid TypeSafe response"):
                                await client.evaluate("q", "text")
                            self.assertEqual(client.budget.evaluated, 0)
                        self.assertTrue(response.is_closed)
                        if not buffered:
                            self.assertTrue(stream.closed)

    async def test_unexpected_compression_is_rejected_before_reading(self):
        body = gzip.compress(padded_answer(2 * 1024 * 1024))
        self.assertLess(len(body), LIMIT)
        for status in (200, 500):
            with self.subTest(status=status):
                stream = TrackedStream(body)

                def respond(request):
                    self.assertEqual(request.headers["Accept-Encoding"], "identity")
                    return httpx2.Response(status, headers={"Content-Encoding": "gzip"}, stream=stream)

                async with contextlib.aclosing(jfind.Jev(
                        "synthetic", 3, transport=httpx2.MockTransport(respond))) as client:
                    with self.assertRaisesRegex(jfind.Failure, "invalid TypeSafe response"):
                        await client.evaluate("q", "text")
                    self.assertEqual((stream.consumed, client.budget.evaluated), (0, 0))
                    self.assertTrue(stream.closed)

    async def test_authentication_failure_stops_without_reading_the_body(self):
        for status in (401, 403):
            with self.subTest(status=status):
                stream = TrackedStream(padded_answer(2 * 1024 * 1024))
                transport = httpx2.MockTransport(lambda _: httpx2.Response(status, stream=stream))
                async with contextlib.aclosing(jfind.Jev("synthetic", 3, transport=transport)) as client:
                    with self.assertRaisesRegex(jfind.StopSearch, "authentication failed"):
                        await client.evaluate("q", "text")
                    self.assertEqual((stream.consumed, client.budget.evaluated), (0, 0))
                    self.assertEqual(client.attempts, 1)
                    self.assertTrue(stream.closed)

    async def test_cancelled_body_read_closes_the_stream(self):
        reading = asyncio.Event()

        class SlowStream(TrackedStream):
            async def __aiter__(self):
                yield b"{"
                reading.set()
                await asyncio.Event().wait()

        stream = SlowStream(b"")
        transport = httpx2.MockTransport(lambda _: httpx2.Response(200, stream=stream))
        async with contextlib.aclosing(jfind.Jev("synthetic", 3, transport=transport)) as client:
            task = asyncio.create_task(client.evaluate("q", "text"))
            try:
                await asyncio.wait_for(reading.wait(), 1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            self.assertTrue(stream.closed)
            self.assertEqual(client.budget.evaluated, 0)


if __name__ == "__main__":
    unittest.main()
