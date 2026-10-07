"""Offline regressions for failures found in the live download audit."""
import io
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import Mock, patch

import polite


class SourceRequestTests(unittest.TestCase):
    def test_unicode_path_and_query_are_encoded_without_double_encoding(self):
        url = 'https://medium.com/\uc0c1\ubd88\uacbd/story%20name?q=caf\u00e9&next=a%2Fb'
        with patch.object(polite, 'limiter', return_value=Mock()), \
                patch.object(polite.urllib.request, 'urlopen', return_value=io.BytesIO(b'body')) as open_url:
            self.assertEqual(polite.get(url), b'body')
        sent = open_url.call_args.args[0].full_url
        self.assertEqual(sent, 'https://medium.com/%EC%83%81%EB%B6%88%EA%B2%BD/story%20name?q=caf%C3%A9&next=a%2Fb')
        sent.encode('ascii')

    def test_max_bytes_caps_the_read(self):
        # A hostile or mislabelled image URL must not stream unbounded bytes into memory.
        with patch.object(polite, 'limiter', return_value=Mock()), \
                patch.object(polite.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(b'x' * 100)):
            with self.assertRaises(ValueError):
                polite.get('https://miro.medium.com/big.png', max_bytes=10)
            self.assertEqual(polite.get('https://miro.medium.com/ok.png', max_bytes=1000), b'x' * 100)

    def test_network_errors_are_distinguished(self):
        cases = [
            (urllib.error.URLError(socket.gaierror(11001, 'getaddrinfo failed')), 'DNS'),
            (urllib.error.URLError(ssl.SSLEOFError(8, 'EOF')), 'TLS'),
            (urllib.error.HTTPError('https://example.test', 403, 'Forbidden', {}, None), 'HTTP 403'),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, polite.describe_error(error))


if __name__ == '__main__':
    unittest.main()
