"""Run: .venv/bin/python -m unittest test_gmail_retry -v"""
import json
import unittest
from unittest import mock

import httplib2
import requests
from googleapiclient.errors import HttpError

import fetch_article
import main


def http_error(status, reason=None):
    body = {'error': {'errors': [{'reason': reason}] if reason else []}}
    return HttpError(httplib2.Response({'status': status}), json.dumps(body).encode())


class Req:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def execute(self):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@mock.patch.object(main.time, 'sleep')
class GmailExecute(unittest.TestCase):
    def test_retries_quota_then_5xx_then_succeeds(self, sleep):
        req = Req(http_error(403, 'rateLimitExceeded'), http_error(503), 'ok')
        self.assertEqual(main.gmail_execute(req), 'ok')
        self.assertEqual(sleep.call_count, 2)

    def test_permission_403_fails_at_once(self, sleep):
        with self.assertRaises(HttpError):
            main.gmail_execute(Req(http_error(403, 'insufficientPermissions')))
        sleep.assert_not_called()

    def test_404_fails_at_once(self, sleep):
        with self.assertRaises(HttpError):
            main.gmail_execute(Req(http_error(404)))
        sleep.assert_not_called()

    def test_gives_up_after_attempts(self, sleep):
        req = Req(*[http_error(429)] * 5)
        with self.assertRaises(HttpError):
            main.gmail_execute(req)
        self.assertEqual(req.calls, 5)


class NetworkDown(unittest.TestCase):
    def test_dns_failure_raises_not_fallback(self):
        with mock.patch.object(fetch_article, 'fetch_article_text', side_effect=requests.ConnectionError('dns')):
            with self.assertRaises(fetch_article.NetworkDown):
                fetch_article.fetch_with_fallbacks('https://example.com/a', 'body', verbose=False)

    def test_ssl_error_still_falls_back(self):
        with mock.patch.object(fetch_article, 'fetch_article_text', side_effect=requests.exceptions.SSLError('cert')), \
             mock.patch.object(fetch_article, '_try_archive_ph', side_effect=requests.Timeout()), \
             mock.patch.object(fetch_article, '_try_substack_rss', return_value=None), \
             mock.patch.object(fetch_article, '_try_12ft', side_effect=requests.Timeout()):
            self.assertEqual(fetch_article.fetch_with_fallbacks('https://example.com/a', 'body', verbose=False)[1], 'fallback')

    def test_connect_timeout_still_falls_back(self):
        with mock.patch.object(fetch_article, 'fetch_article_text', side_effect=requests.ConnectTimeout('slow')), \
             mock.patch.object(fetch_article, '_try_archive_ph', side_effect=requests.Timeout()), \
             mock.patch.object(fetch_article, '_try_substack_rss', return_value=None), \
             mock.patch.object(fetch_article, '_try_12ft', side_effect=requests.Timeout()):
            self.assertEqual(fetch_article.fetch_with_fallbacks('https://example.com/a', 'body', verbose=False)[1], 'fallback')

    def test_process_link_propagates_and_does_not_cache(self):
        cache = {}
        with mock.patch.object(main, 'fetch_with_fallbacks', side_effect=fetch_article.NetworkDown('down')):
            with self.assertRaises(fetch_article.NetworkDown):
                main.process_link({'url': 'https://example.com/a', 'title': 't'}, 'body', 'prompt', cache)
        self.assertEqual(cache, {})


if __name__ == '__main__':
    unittest.main()
