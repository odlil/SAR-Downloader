from pathlib import Path
import sys
import unittest
import threading
from urllib.parse import urlsplit
from unittest.mock import Mock, patch
import requests
from requests.auth import HTTPBasicAuth

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from earthdata_auth import AuthError, EarthdataAuth, NoAuth, ScopedSession, TransferError, validate_url


def response(status=200, data=None, location=None):
    result=Mock(status_code=status,headers={"Location":location} if location else {})
    result.json.return_value=data
    result.__enter__=Mock(return_value=result);result.__exit__=Mock(return_value=False)
    return result


class AuthenticationTests(unittest.TestCase):
    def test_unauthorized_response_clears_only_its_own_login_generation(self):
        auth = EarthdataAuth()
        auth.token, auth.username = 'fixture-old', 'fixture-user'
        old = auth.session()
        auth.close()
        auth.token, auth.username = 'fixture-new', 'fixture-user'
        with patch.object(old.http, 'request', return_value=response(401)):
            # An old response cannot clear a newer login.
            old.on_auth_failure()
        self.assertTrue(auth.status()['loggedIn'])
        current = auth.session()
        with patch.object(current.http, 'request', return_value=response(401)):
            current.request('GET', 'https://datapool.asf.alaska.edu/test/scene.zip')
        self.assertFalse(auth.status()['loggedIn'])
        old.close()
        current.close()

    def test_logout_cancels_an_inflight_login(self):
        auth = EarthdataAuth()
        entered, release = threading.Event(), threading.Event()
        failures = []
        def request(method, url):
            if method == 'POST':
                entered.set()
                release.wait(3)
                return response(data={'access_token': 'fixture-token'})
            return response()
        def login():
            try:
                auth.login('fixture-user', 'fixture-password')
            except AuthError as error:
                failures.append(error)
        with patch('earthdata_auth.ScopedSession') as factory:
            factory.return_value.request.side_effect = request
            factory.return_value.http.cookies = requests.cookies.RequestsCookieJar()
            thread = threading.Thread(target=login)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(auth.logout()['loggedIn'])
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(len(failures), 1)
        self.assertFalse(auth.status()['loggedIn'])

    def test_validated_credentials_are_not_stored_and_logout_invalidates_sessions(self):
        auth=EarthdataAuth()
        with patch("earthdata_auth.ScopedSession") as factory:
            session=factory.return_value
            session.request.side_effect=[response(data={"access_token":"fixture-token"}),response()]
            cookies=requests.cookies.RequestsCookieJar();cookies.set("asf-urs","fixture-cookie",domain=".asf.alaska.edu")
            session.http.cookies=cookies
            status=auth.login("fixture-user","fixture-password")
        self.assertTrue(status["loggedIn"]);self.assertTrue(status["burstReady"])
        self.assertNotIn("fixture-token",str(status));self.assertNotIn("fixture-password",str(vars(auth)))
        transfer=auth.session(burst=True)
        auth.logout()
        with self.assertRaises(AuthError):transfer.guard()
        transfer.close();self.assertFalse(auth.status()["loggedIn"])
    def test_bad_credentials_and_network_failure_are_explicit_without_secrets(self):
        for reply, expected in ((response(401),AuthError),(requests.ConnectionError("sensitive request internals"),TransferError)):
            with patch("earthdata_auth.ScopedSession") as factory:
                if isinstance(reply,Exception):factory.return_value.request.side_effect=reply
                else:factory.return_value.request.return_value=reply
                with self.assertRaises(expected) as caught:EarthdataAuth().login("fixture","fixture-password")
                self.assertNotIn("sensitive",str(caught.exception));self.assertNotIn("fixture-password",str(caught.exception))
    def test_missing_burst_cookie_keeps_standard_login_but_blocks_burst(self):
        auth=EarthdataAuth()
        with patch("earthdata_auth.ScopedSession") as factory:
            factory.return_value.request.side_effect=[response(data={"access_token":"fixture-token"}),response()]
            factory.return_value.http.cookies=requests.cookies.RequestsCookieJar()
            self.assertFalse(auth.login("fixture","fixture-password")["burstReady"])
        with self.assertRaises(AuthError):auth.session(burst=True)
        auth.session().close()
    def test_basic_is_only_sent_to_earthdata_and_bearer_is_stripped_for_s3(self):
        session=ScopedSession(token="fixture-token",basic=("fixture","fixture-password"))
        with patch.object(session.http,"request",side_effect=[response(302,location="https://cumulus.asf.alaska.edu/login"),response(307,location="https://bucket.s3.us-west-2.amazonaws.com/data?signature=test"),response()]) as send:
            session.request("GET","https://urs.earthdata.nasa.gov/oauth/authorize")
            calls=send.call_args_list
            self.assertIsInstance(calls[0].kwargs["auth"],HTTPBasicAuth)
            self.assertIsInstance(calls[1].kwargs["auth"],NoAuth)
            self.assertEqual(calls[1].kwargs["headers"]["Authorization"],"Bearer fixture-token")
            self.assertNotIn("Authorization",calls[2].kwargs["headers"])
            self.assertIsInstance(calls[2].kwargs["auth"],NoAuth)
        session.close()
    def test_untrusted_or_insecure_redirect_is_blocked_before_request(self):
        for url in ("http://datapool.asf.alaska.edu/data", "https://asf.alaska.edu.evil.org/a", "https://user:password@asf.alaska.edu/a", "https://127.0.0.1/a"):
            with self.assertRaises(TransferError):validate_url(url)
        session=ScopedSession(token="fixture-token")
        with patch.object(session.http,"request",return_value=response(302,location="http://localhost/data")) as send:
            with self.assertRaises(TransferError):session.request("GET","https://datapool.asf.alaska.edu/a")
            self.assertEqual(send.call_count,1)
        session.close()

    def test_nisar_official_cloud_asset_receives_bearer_without_basic_credentials(self):
        url="https://nisar.asf.earthdatacloud.nasa.gov/NISAR-UR/scene.h5"
        self.assertEqual(validate_url(url,asset=True),url)
        session=ScopedSession(token="fixture-token",basic=("fixture","fixture-password"))
        with patch.object(session.http,"request",return_value=response()) as send:
            session.request("GET",url)
            self.assertEqual(send.call_args.kwargs["headers"]["Authorization"],"Bearer fixture-token")
            self.assertIsInstance(send.call_args.kwargs["auth"],NoAuth)
        session.close()

    def test_asf_cloudfront_redirect_preserves_range_without_forwarding_credentials(self):
        signed = "https://dy4owt9f80bz7.cloudfront.net/s3-source/scene.zip?X-Amz-Signature=fixture-signature"
        final = response(206)
        session = ScopedSession(token="fixture-token", basic=("fixture", "fixture-password"))
        session.http.cookies.set("asf-urs", "fixture-cookie", domain=".asf.alaska.edu")
        with patch.object(session.http, "request", side_effect=[
            response(307, location="https://sentinel1.asf.alaska.edu/SLC/SC/scene.zip"),
            response(302, location=signed), final,
        ]) as send:
            result = session.request("GET", "https://datapool.asf.alaska.edu/SLC/SC/scene.zip",
                                     headers={"Range": "bytes=1048576-", "If-Range": '"fixture-v1"',
                                              "Authorization": "caller-secret", "Cookie": "caller-cookie"}, stream=True)
            self.assertIs(result, final)
            self.assertEqual(send.call_count, 3)
            self.assertEqual(send.call_args.args[1], signed)
            for call in send.call_args_list:
                self.assertEqual(call.kwargs["headers"]["Range"], "bytes=1048576-")
                self.assertEqual(call.kwargs["headers"]["If-Range"], '"fixture-v1"')
                self.assertNotIn("Cookie", call.kwargs["headers"])
                self.assertIsInstance(call.kwargs["auth"], NoAuth)
            self.assertEqual(send.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer fixture-token")
            self.assertNotIn("Authorization", send.call_args.kwargs["headers"])
        session.close()

    def test_cloudfront_requires_delegation_from_asf_and_is_never_a_queue_url(self):
        url = "https://dy4owt9f80bz7.cloudfront.net/scene.zip?X-Amz-Signature=fixture"
        for options in ({}, {"asset": True}, {"redirect_from": "https://urs.earthdata.nasa.gov/a"},
                        {"redirect_from": "https://untrusted.example/a"},
                        {"redirect_from": "https://bucket.s3.amazonaws.com/a"},
                        {"redirect_from": "http://sentinel1.asf.alaska.edu/a"}):
            with self.subTest(options=options), self.assertRaises(TransferError):
                validate_url(url, **options)
        self.assertEqual(validate_url(url, redirect_from="https://sentinel1.asf.alaska.edu/a"), url)
        self.assertEqual(validate_url(url, redirect_from="https://nisar.asf.earthdatacloud.nasa.gov/a"), url)

    def test_cloudfront_relative_redirect_stays_in_the_delegated_distribution(self):
        session = ScopedSession(token="fixture-token")
        with patch.object(session.http, "request", side_effect=[
            response(302, location="https://dy4owt9f80bz7.cloudfront.net/scene.zip?signature=fixture"),
            response(307, location="/file/scene.zip?signature=next"), response(),
        ]) as send:
            session.request("GET", "https://sentinel1.asf.alaska.edu/SLC/SC/scene.zip")
            self.assertEqual(send.call_args.args[1], "https://dy4owt9f80bz7.cloudfront.net/file/scene.zip?signature=next")
            self.assertNotIn("Authorization", send.call_args.kwargs["headers"])
        session.close()

    def test_untrusted_redirect_reports_only_host_without_signed_secrets(self):
        for url in ("https://dy4owt9f80bz7.cloudfront.net.evil.example/scene?token=secret-value",
                    "http://dy4owt9f80bz7.cloudfront.net/scene?token=secret-value",
                    "https://127.0.0.1/scene?token=secret-value",
                    "https://user:password@dy4owt9f80bz7.cloudfront.net/scene?token=secret-value"):
            session = ScopedSession(token="fixture-token")
            with self.subTest(url=url), patch.object(session.http, "request", return_value=response(302, location=url)) as send:
                with self.assertRaises(TransferError) as caught:
                    session.request("GET", "https://sentinel1.asf.alaska.edu/a")
                self.assertEqual(send.call_count, 1)
                self.assertIn(urlsplit(url).hostname, str(caught.exception))
                self.assertNotIn("secret-value", str(caught.exception))
                self.assertNotIn("password", str(caught.exception))
            session.close()

    def test_subset_reuses_delegated_cdn_without_sending_earthdata_credentials(self):
        from nisar_subset_engine import WorkbenchRangeSession
        auth = EarthdataAuth()
        auth.token = 'fixture-token'
        original = 'https://nisar.asf.earthdatacloud.nasa.gov/NISAR-UR/scene.h5'
        signed = 'https://dy4owt9f80bz7.cloudfront.net/scene.h5?signature=fixture'
        session = WorkbenchRangeSession(auth, original)
        try:
            with patch.object(session.session.http, 'request', return_value=response(206)) as send:
                session.get(signed, headers={'Range': 'bytes=0-1023'}, timeout=(12, 40), allow_redirects=True)
                self.assertEqual(send.call_args.kwargs['headers'], {'Range': 'bytes=0-1023'})
                self.assertIsInstance(send.call_args.kwargs['auth'], NoAuth)
            auth.logout()
            with self.assertRaises(AuthError):
                session.get(original, headers={'Range': 'bytes=0-1023'})
        finally:
            session.close()


if __name__=="__main__":unittest.main(verbosity=2)
