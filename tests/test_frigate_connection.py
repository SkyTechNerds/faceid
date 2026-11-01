"""verify_tls gegen die Umgebung und die Anmeldung bei go2rtc.

Beides fiel erst in der Home-Assistant-App auf: das Basis-Image setzt CURL_CA_BUNDLE, was
``verify = False`` aushebelte, und eine go2rtc mit api.username/password antwortete 401.
"""
import os
import unittest
from unittest import mock

from app.frigate_api import FrigateAPI, frigate_client

URL = "https://frigate.invalid:8971/api/config"


class VerifyTlsTests(unittest.TestCase):
    def test_verify_false_holds_against_curl_ca_bundle(self):
        api = FrigateAPI("https://frigate.invalid:8971")
        with mock.patch.dict(os.environ, {"CURL_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt"}):
            settings = api.session.merge_environment_settings(URL, {}, None, None, None)
        self.assertIs(settings["verify"], False)

    def test_verify_true_still_uses_the_bundle(self):
        # Wer eine eigene CA ueber die Umgebung mitgibt, soll sie weiter bekommen.
        api = FrigateAPI("https://frigate.invalid:8971", verify_tls=True)
        with mock.patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": "/x/ca.pem"}):
            settings = api.session.merge_environment_settings(URL, {}, None, None, None)
        self.assertEqual(settings["verify"], "/x/ca.pem")

    def test_verify_given_per_call_wins(self):
        api = FrigateAPI("https://frigate.invalid:8971")
        settings = api.session.merge_environment_settings(URL, {}, None, "/y/ca.pem", None)
        self.assertEqual(settings["verify"], "/y/ca.pem")


class Go2rtcTests(unittest.TestCase):
    def test_default_is_frigates_host_without_login(self):
        api = FrigateAPI("https://192.168.1.10:8971")
        self.assertEqual(api.go2rtc, "http://192.168.1.10:1984")
        self.assertIsNone(api.go2rtc_auth)

    def test_empty_app_options_mean_not_set(self):
        # run.sh schreibt leere Optionen als "" — das muss wirken wie nicht gesetzt.
        api = frigate_client({"frigate": {"url": "http://f:5000", "go2rtc_url": "",
                                          "go2rtc_user": "", "go2rtc_password": ""}})
        self.assertEqual(api.go2rtc, "http://f:1984")
        self.assertIsNone(api.go2rtc_auth)

    def test_app_options_reach_the_client(self):
        api = frigate_client({"frigate": {"url": "https://proxy.example",
                                          "go2rtc_url": "http://10.0.0.5:1984/",
                                          "go2rtc_user": "go2rtc", "go2rtc_password": "pw"}})
        self.assertEqual(api.go2rtc, "http://10.0.0.5:1984")
        self.assertEqual(api.go2rtc_auth, ("go2rtc", "pw"))

    def test_live_frame_sends_the_login(self):
        api = FrigateAPI("http://f:5000", go2rtc_user="go2rtc", go2rtc_password="pw")
        with mock.patch.object(api.session, "get") as get, \
                mock.patch("app.frigate_api.time.sleep"):
            get.return_value = mock.Mock(status_code=401, content=b"")
            self.assertIsNone(api.live_frame("cam", timeout=1))
        self.assertEqual(get.call_args.kwargs["auth"], ("go2rtc", "pw"))


if __name__ == "__main__":
    unittest.main()
