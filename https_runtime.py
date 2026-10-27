"""HTTPS trust independent of the OpenSSL paths on the build machine."""
from __future__ import annotations

import os
import ssl
import sys

import certifi


def ca_bundle():
    # Keep explicit corporate/proxy CA overrides; otherwise use the bundled roots.
    return (os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE')
            or os.environ.get('SSL_CERT_FILE') or certifi.where())


def configure_https():
    if sys.platform == 'linux' and getattr(sys, 'frozen', False):
        bundle = ca_bundle()
        for name in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'GDAL_HTTP_CAINFO'):
            if not os.environ.get(name):
                os.environ[name] = bundle


def https_context():
    context = ssl.create_default_context(cafile=ca_bundle())
    if not context.cert_store_stats()['x509_ca']:
        raise ssl.SSLError('The HTTPS CA bundle contains no trusted root certificates.')
    return context
