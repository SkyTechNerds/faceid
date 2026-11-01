"""verify_tls gegen die Umgebung und die Anmeldung bei go2rtc.

Beides fiel erst in der Home-Assistant-App auf: das Basis-Image setzt CURL_CA_BUNDLE, was
``verify = False`` aushebelte, und eine go2rtc mit api.username/password antwortete 401.
"""
import base64
import os
import types
import unittest
from unittest import mock

from app.frigate_api import FrigateAPI, frigate_client

URL = "https://frigate.invalid:8971/api/config"


def basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


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
        self.assertIsNone(api.go2rtc_headers)

    def test_empty_app_options_mean_not_set(self):
        # run.sh schreibt leere Optionen als "" — das muss wirken wie nicht gesetzt.
        api = frigate_client({"frigate": {"url": "http://f:5000", "go2rtc_url": "",
                                          "go2rtc_user": "", "go2rtc_password": ""}})
        self.assertEqual(api.go2rtc, "http://f:1984")
        self.assertIsNone(api.go2rtc_headers)

    def test_app_options_reach_the_client(self):
        api = frigate_client({"frigate": {"url": "https://proxy.example",
                                          "go2rtc_url": "http://10.0.0.5:1984/",
                                          "go2rtc_user": "go2rtc", "go2rtc_password": "pw"}})
        self.assertEqual(api.go2rtc, "http://10.0.0.5:1984")
        self.assertEqual(api.go2rtc_headers, {"Authorization": basic("go2rtc", "pw")})

    def test_live_frame_sends_the_login(self):
        api = FrigateAPI("http://f:5000", go2rtc_user="go2rtc", go2rtc_password="pw")
        with mock.patch.object(api.session, "get") as get, \
                mock.patch("app.frigate_api.time.sleep"):
            get.return_value = mock.Mock(status_code=401, content=b"")
            self.assertIsNone(api.live_frame("cam", timeout=1))
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": basic("go2rtc", "pw")})
        self.assertEqual(api.go2rtc_status, 401)

    def test_password_outside_latin1_does_not_crash(self):
        # requests' auth=(user, pw) warf hier UnicodeEncodeError, die App brach beim Start ab.
        api = FrigateAPI("http://127.0.0.1:9", go2rtc_user="go2rtc", go2rtc_password="p€ss")
        self.assertEqual(api.go2rtc_headers, {"Authorization": basic("go2rtc", "p€ss")})
        with mock.patch("app.frigate_api.time.sleep"):
            self.assertIsNone(api.live_frame("cam", timeout=0.5))
        self.assertIsNone(api.go2rtc_status)

    def test_timeout_on_retry_keeps_the_401(self):
        import requests
        api = FrigateAPI("http://f:5000", go2rtc_user="go2rtc", go2rtc_password="pw")
        with mock.patch.object(api.session, "get") as get, \
                mock.patch("app.frigate_api.time.sleep"):
            get.side_effect = [mock.Mock(status_code=401, content=b""),
                               requests.Timeout("slow")]
            self.assertIsNone(api.live_frame("cam", timeout=1))
        self.assertEqual(api.go2rtc_status, 401)

    def test_status_does_not_leak_into_the_next_call(self):
        import requests
        api = FrigateAPI("http://f:5000")
        api.go2rtc_status = 401
        with mock.patch.object(api.session, "get", side_effect=requests.Timeout("x")), \
                mock.patch("app.frigate_api.time.sleep"):
            api.live_frame("cam", timeout=1)
        self.assertIsNone(api.go2rtc_status)

    def test_disabled_client_has_the_current_attributes(self):
        api = frigate_client({"frigate": {"url": ""}})
        self.assertFalse(api.enabled)
        self.assertIsNone(api.go2rtc_headers)
        self.assertIsNone(api.go2rtc_status)
        self.assertIsNone(api.live_frame("cam"))

    def test_password_without_user_warns(self):
        with self.assertLogs("faceid.frigate", "WARNING") as logs:
            api = FrigateAPI("http://f:5000", go2rtc_password="pw")
        self.assertIsNone(api.go2rtc_headers)
        self.assertIn("go2rtc_user is empty", logs.output[0])


class Go2rtcStartupCheckTests(unittest.TestCase):
    def _check(self, status):
        from app.mqtt_listener import EventProcessor
        frigate = types.SimpleNamespace(go2rtc="http://f:1984", go2rtc_status=status,
                                        live_frame=lambda cam, timeout: None)
        proc = types.SimpleNamespace(live_hires_cameras={"cam"}, live_hires=True,
                                     frigate=frigate, poll_interval=0,
                                     _frigate_cameras=lambda: [])
        with self.assertLogs("faceid", "WARNING") as logs:
            EventProcessor._check_go2rtc(proc)
        return logs.output[0]

    def test_401_is_named(self):
        self.assertIn("answered HTTP 401", self._check(401))

    def test_other_status_is_named_too(self):
        line = self._check(404)
        self.assertIn("answered HTTP 404", line)
        self.assertNotIn("go2rtc_user", line)

    def test_no_answer_stays_no_answer(self):
        self.assertIn("did not answer", self._check(None))


if __name__ == "__main__":
    unittest.main()
