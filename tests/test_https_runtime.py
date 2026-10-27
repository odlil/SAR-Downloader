"""Regression: relocated OpenSSL must not leave urllib with an empty CA store."""
import io
import os
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import certifi
from asf_api import ENDPOINT, SearchError, _open_catalog, _request
from https_runtime import ca_bundle, configure_https, https_context

TRUST_VARIABLES = ('SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'GDAL_HTTP_CAINFO')


class HTTPSRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in TRUST_VARIABLES}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_explicit_bundle_loads_roots_without_openssl_default_paths(self):
        # Simulate a removed build environment without providing an SSL_CERT_FILE.
        with patch('ssl.SSLContext.load_default_certs', side_effect=AssertionError('build-machine defaults used')):
            context = https_context()
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(ca_bundle(), certifi.where())

    def test_frozen_defaults_cover_urllib_requests_and_gdal_and_preserve_proxy(self):
        os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:12345'
        with patch('https_runtime.sys.platform', 'linux'), patch('https_runtime.sys.frozen', True, create=True):
            configure_https()
            configure_https()
        for variable in TRUST_VARIABLES:
            if variable != 'SSL_CERT_DIR':
                self.assertEqual(os.environ[variable], certifi.where())
        self.assertEqual(os.environ['HTTPS_PROXY'], 'http://127.0.0.1:12345')
        self.assertGreater(ssl.create_default_context().cert_store_stats()['x509_ca'], 0)

    def test_custom_ca_is_preserved_and_applied_to_urllib(self):
        for variable in ('SSL_CERT_FILE', 'CURL_CA_BUNDLE', 'REQUESTS_CA_BUNDLE'):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: certifi.where()}):
                self.assertEqual(ca_bundle(), certifi.where())
                self.assertGreater(https_context().cert_store_stats()['x509_ca'], 0)
        with patch.dict(os.environ, {'REQUESTS_CA_BUNDLE': '/missing/custom-proxy-ca.pem'}):
            with patch('https_runtime.sys.platform', 'linux'), patch('https_runtime.sys.frozen', True, create=True):
                configure_https()
            self.assertEqual(os.environ['REQUESTS_CA_BUNDLE'], '/missing/custom-proxy-ca.pem')
            with self.assertRaises(OSError):
                https_context()

    def test_asf_request_uses_a_verified_context_and_keeps_result_parsing(self):
        with patch('asf_api.sys.platform', 'linux'), patch('asf_api.sys.frozen', True, create=True), \
             patch('asf_api.urlopen', return_value=io.BytesIO(b'{"type":"FeatureCollection","features":[]}')) as opener:
            self.assertEqual(_request({'dataset': 'SENTINEL-1'}), [])
        context = opener.call_args.kwargs['context']
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_certificate_failure_has_specific_message(self):
        error = URLError(ssl.SSLCertVerificationError('fixture untrusted server'))
        with patch('asf_api._open_catalog', side_effect=error), self.assertRaisesRegex(SearchError, '证书验证失败'):
            _request({})

    def test_missing_ca_fails_before_network(self):
        with patch.dict(os.environ, {'SSL_CERT_FILE': '/missing/sar-ca.pem'}), patch('asf_api.urlopen') as request, \
             patch('asf_api.sys.platform', 'linux'), patch('asf_api.sys.frozen', True, create=True):
            with self.assertRaisesRegex(SearchError, '无法加载 HTTPS CA'):
                _request({})
            request.assert_not_called()

    def test_empty_trust_store_is_rejected(self):
        with patch('https_runtime.ssl.create_default_context') as factory:
            factory.return_value.cert_store_stats.return_value = {'x509_ca': 0}
            with self.assertRaises(ssl.SSLError):
                https_context()

    def test_source_and_windows_use_the_original_urlopen_arguments(self):
        for platform, frozen in (('win32', False), ('win32', True), ('linux', False)):
            with self.subTest(platform=platform, frozen=frozen), patch('asf_api.sys.platform', platform), \
                 patch('asf_api.sys.frozen', frozen, create=True), patch('asf_api.urlopen') as opener, \
                 patch('asf_api.https_context') as context:
                request = Request(ENDPOINT)
                self.assertIs(_open_catalog(request), opener.return_value)
                opener.assert_called_once_with(request, timeout=45)
                context.assert_not_called()

    def test_source_and_windows_do_not_change_certificate_environment(self):
        for platform, frozen in (('win32', False), ('win32', True), ('linux', False)):
            with self.subTest(platform=platform, frozen=frozen), patch('https_runtime.sys.platform', platform), \
                 patch('https_runtime.sys.frozen', frozen, create=True):
                before = dict(os.environ)
                configure_https()
                self.assertEqual(dict(os.environ), before)

    def test_nested_upstream_500_is_not_reported_as_bad_search_input(self):
        content = b'{"error":{"report":"Search failed to find results: HTTP 500: [\'An Internal Error has occurred.\']"}}'
        error = HTTPError(ENDPOINT, 400, 'fixture', {}, io.BytesIO(content))
        with patch('asf_api._open_catalog', side_effect=error), self.assertRaises(SearchError) as raised:
            _request({})
        self.assertEqual(raised.exception.status, 502)
        self.assertIn('上游目录服务异常', str(raised.exception))
        self.assertIn('上游 HTTP 500', str(raised.exception))
        self.assertTrue(error.fp.closed)

    def test_normal_400_keeps_parameter_error_detail(self):
        error = HTTPError(ENDPOINT, 400, 'fixture', {}, io.BytesIO(b'{"error":{"report":"Invalid processingLevel"}}'))
        with patch('asf_api._open_catalog', side_effect=error), self.assertRaisesRegex(SearchError, 'Invalid processingLevel'):
            _request({})

    def test_gateway_error_and_wrapped_timeout_keep_specific_status(self):
        for error in (HTTPError(ENDPOINT, 504, 'fixture', {}, io.BytesIO(b'{}')), URLError(TimeoutError())):
            with self.subTest(error=error), patch('asf_api._open_catalog', side_effect=error):
                with self.assertRaises(SearchError) as raised:
                    _request({})
                self.assertEqual(raised.exception.status, 504)
        self.assertIn('超时', str(raised.exception))
