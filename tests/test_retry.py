"""Run on the container: python3 -m unittest discover -s tests"""
import unittest
from unittest import mock

import requests

from jobs import retry


def response(status, url, text=""):
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = text.encode()
    return r


class ScrubTests(unittest.TestCase):
    def test_removes_key_like_parameters(self):
        for raw in ("GET /api/v1/points?api_key=SUPERSECRET123&start_at=5", "url: http://h/x?token=abcdef123456",
                    "...?API_KEY=UPPER99&x=1", "password=hunter22 tail", "secret=abc123def"):
            out = retry.scrub(raw)
            for leaked in ("SUPERSECRET123", "abcdef123456", "UPPER99", "hunter22", "abc123def"):
                self.assertNotIn(leaked, out)
            self.assertIn("***", out)

    def test_leaves_ordinary_text_alone(self):
        self.assertEqual(retry.scrub("HTTP 400 from https://immich.example: Bad Request"), "HTTP 400 from https://immich.example: Bad Request")


class CallWithRetryTests(unittest.TestCase):
    def test_4xx_becomes_a_clear_rejection_without_the_key(self):
        body = "<html><head><title>400 The plain HTTP request was sent to HTTPS port</title></head></html>"
        err = requests.HTTPError("400", response=response(400, "http://h:3000/api/v1/points?api_key=TOPSECRET&x=1", body))
        with self.assertRaises(retry.ServiceRejected) as ctx:
            retry.call_with_retry(mock.Mock(side_effect=err), max_attempts=3, base_delay=0)
        msg = str(ctx.exception)
        self.assertIn("400", msg)
        self.assertIn("plain HTTP request was sent to HTTPS port", msg)
        self.assertNotIn("TOPSECRET", msg)
        self.assertNotIn("api_key", msg)
        self.assertIsInstance(ctx.exception, retry.ServiceUnavailable)     # existing callers still catch it

    def test_4xx_is_not_retried(self):
        fn = mock.Mock(side_effect=requests.HTTPError("403", response=response(403, "http://h/x", "nope")))
        with self.assertRaises(retry.ServiceRejected):
            retry.call_with_retry(fn, max_attempts=5, base_delay=0)
        self.assertEqual(fn.call_count, 1)

    def test_5xx_and_connection_errors_are_retried_then_scrubbed(self):
        fn = mock.Mock(side_effect=requests.ConnectionError("Max retries exceeded with url: /api/v1/points?api_key=LEAKME99&a=1"))
        with self.assertRaises(retry.ServiceUnavailable) as ctx:
            retry.call_with_retry(fn, max_attempts=3, base_delay=0)
        self.assertEqual(fn.call_count, 3)
        self.assertNotIn("LEAKME99", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, retry.ServiceRejected)

    def test_success_returns_the_value(self):
        self.assertEqual(retry.call_with_retry(lambda: 42), 42)


if __name__ == "__main__":
    unittest.main()
