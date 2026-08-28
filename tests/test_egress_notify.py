#!/usr/bin/env python3
"""Unit tests for egress ntfy push notifications."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

import egress_notify as notify  # noqa: E402
from egress_test_sync import join_thread_or_fail  # noqa: E402

SENTINEL_TOPIC = "sentinel-topic-xyzzy"
SENTINEL_TOKEN = "sentinel-token-xyzzy"
SENTINEL_OP_TOKEN = "sentinel-op-token-xyzzy"


class ReadSecretsEnvTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        path = Path(tempfile.mkdtemp()) / "missing.env"
        self.assertEqual(notify.read_secrets_env(path, ("NTFY_URL",)), {})

    def test_parses_requested_keys_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.env"
            path.write_text(
                "# comment\n"
                "\n"
                "OTHER=value\n"
                'export NTFY_URL="https://ntfy.example"\n'
                "NTFY_TOPIC=djinn-agents\n"
                "NTFY_TOKEN='secret-token'\n",
                encoding="utf-8",
            )
            result = notify.read_secrets_env(path, notify.NTFY_ENV_NAMES)
            self.assertEqual(
                result,
                {
                    "NTFY_URL": "https://ntfy.example",
                    "NTFY_TOPIC": "djinn-agents",
                    "NTFY_TOKEN": "secret-token",
                },
            )
            self.assertNotIn("OTHER", result)


class LoadNtfySettingsTests(unittest.TestCase):
    def _settings(
        self,
        *,
        base: Path,
        env: dict[str, str] | None = None,
        secrets: str = "",
        broker_host: str = "10.8.0.1",
        broker_port: int = 8816,
        operator_token: str = "op-token",
    ) -> notify.NtfySettings | None:
        secrets_path = base / notify.SECRETS_FILENAME
        if secrets:
            secrets_path.write_text(secrets, encoding="utf-8")
        with mock.patch.dict("os.environ", env or {}, clear=True):
            return notify.load_ntfy_settings(
                base,
                {**dict(__import__("os").environ), **(env or {})},
                broker_host=broker_host,
                broker_port=broker_port,
                operator_token=operator_token,
            )

    def test_no_url_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(self._settings(base=Path(tmp)))

    def test_env_wins_over_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            settings = self._settings(
                base=base,
                secrets="NTFY_URL=https://file.example\nNTFY_TOPIC=file-topic\n",
                env={"NTFY_URL": "https://env.example", "NTFY_TOPIC": SENTINEL_TOPIC},
            )
            assert settings is not None
            self.assertEqual(settings.url, "https://env.example")
            self.assertEqual(settings.topic, SENTINEL_TOPIC)

    def test_invalid_url_logs_warning_without_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bad_url = "https://bad#topic.example"
            with self.assertLogs("egress_notify", level="WARNING") as captured:
                result = self._settings(
                    base=base,
                    env={"NTFY_URL": bad_url},
                )
            self.assertIsNone(result)
            joined = "\n".join(captured.output)
            self.assertNotIn(bad_url, joined)

    def test_trailing_slash_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(
                base=Path(tmp),
                env={"NTFY_URL": "https://ntfy.example/"},
            )
            assert settings is not None
            self.assertEqual(settings.url, "https://ntfy.example")

    def test_default_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(
                base=Path(tmp),
                env={"NTFY_URL": "https://ntfy.example"},
            )
            assert settings is not None
            self.assertEqual(settings.topic, notify.NTFY_DEFAULT_TOPIC)

    def test_loopback_bind_disables_actions(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                with tempfile.TemporaryDirectory() as tmp:
                    settings = self._settings(
                        base=Path(tmp),
                        env={"NTFY_URL": "https://ntfy.example"},
                        broker_host=host,
                        operator_token=SENTINEL_OP_TOKEN,
                    )
                    assert settings is not None
                    self.assertIsNone(settings.broker_url)
                    self.assertIsNone(settings.operator_token)

    def test_non_loopback_carries_broker_url_and_operator_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(
                base=Path(tmp),
                env={"NTFY_URL": "https://ntfy.example"},
                broker_host="10.8.0.1",
                broker_port=8816,
                operator_token=SENTINEL_OP_TOKEN,
            )
            assert settings is not None
            self.assertEqual(settings.broker_url, "http://10.8.0.1:8816")
            self.assertEqual(settings.operator_token, SENTINEL_OP_TOKEN)


class BuildNtfyPayloadTests(unittest.TestCase):
    def _settings(self, *, actions: bool = True) -> notify.NtfySettings:
        return notify.NtfySettings(
            url="https://ntfy.example",
            topic=SENTINEL_TOPIC,
            token=SENTINEL_TOKEN,
            broker_url="http://10.8.0.1:8816" if actions else None,
            operator_token=SENTINEL_OP_TOKEN if actions else None,
        )

    def _notification(self, **overrides: object) -> notify.EgressNotification:
        base = {
            "request_id": "deadbeef",
            "container": "coding-brassbottle",
            "host": "docs.stripe.com",
            "port": 443,
        }
        base.update(overrides)
        return notify.EgressNotification(**base)  # type: ignore[arg-type]

    def test_hostname_request_with_actions(self):
        n = self._notification(
            uid=1000,
            comm="curl",
            reason="npm install",
        )
        payload = notify.build_ntfy_payload(n, self._settings())
        self.assertEqual(
            payload["title"],
            "egress: coding-brassbottle → docs.stripe.com:443",
        )
        self.assertEqual(
            payload["message"],
            "Allow docs.stripe.com (and everything under it)?\n"
            "reason: npm install\n"
            "process: uid=1000 comm=curl\n"
            "req deadbeef",
        )
        self.assertEqual(payload["priority"], notify.NTFY_PRIORITY)
        self.assertEqual(payload["tags"], ["lock"])
        actions = payload["actions"]
        assert isinstance(actions, list)
        self.assertEqual(len(actions), 3)
        labels = [a["label"] for a in actions]
        self.assertEqual(labels, ["Allow", "Allow + persist", "Deny"])
        allow_body = json.loads(actions[0]["body"])
        self.assertEqual(
            allow_body,
            {
                "container": "coding-brassbottle",
                "host": "docs.stripe.com",
                "decision": "allow",
                "scope": "live",
            },
        )
        self.assertEqual(
            actions[0]["headers"]["Authorization"],
            f"Bearer {SENTINEL_OP_TOKEN}",
        )

    def test_actions_off_omits_key(self):
        payload = notify.build_ntfy_payload(
            self._notification(),
            self._settings(actions=False),
        )
        self.assertNotIn("actions", payload)

    def test_ip_request_warning_tag_and_deny_only(self):
        n = self._notification(host="192.0.2.55", port=5432, host_is_ip=True)
        payload = notify.build_ntfy_payload(n, self._settings())
        self.assertEqual(payload["tags"], ["warning"])
        actions = payload["actions"]
        assert isinstance(actions, list)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["label"], "Deny")
        deny_body = json.loads(actions[0]["body"])
        self.assertEqual(
            deny_body,
            {
                "container": "coding-brassbottle",
                "host": "192.0.2.55",
                "decision": "deny",
                "reason": "denied from notification",
            },
        )

    def test_never_more_than_three_actions(self):
        payload = notify.build_ntfy_payload(self._notification(), self._settings())
        actions = payload.get("actions", [])
        self.assertLessEqual(len(actions), 3)


class NtfyNotifierSendTests(unittest.TestCase):
    def _notification(self) -> notify.EgressNotification:
        return notify.EgressNotification(
            request_id="cafebabe",
            container="coding-brassbottle",
            host="neon.tech",
            port=443,
        )

    def test_send_posts_json_payload(self):
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=0):
            captured["request"] = request
            captured["timeout"] = timeout
            return mock.Mock(status=200, __enter__=lambda s: s, __exit__=mock.Mock())

        settings = notify.NtfySettings(
            url="https://ntfy.example",
            topic=SENTINEL_TOPIC,
            token=SENTINEL_TOKEN,
            broker_url="http://10.8.0.1:8816",
            operator_token=SENTINEL_OP_TOKEN,
        )
        notifier = notify.NtfyNotifier(settings, urlopen=fake_urlopen)
        with self.assertLogs("egress_notify", level="INFO") as captured_logs:
            notifier.send(self._notification())

        request = captured["request"]
        assert isinstance(request, notify.urllib.request.Request)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://ntfy.example/")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["topic"], SENTINEL_TOPIC)
        self.assertIn("Authorization", request.headers)
        joined = "\n".join(captured_logs.output)
        self.assertNotIn(SENTINEL_TOPIC, joined)
        self.assertNotIn(SENTINEL_TOKEN, joined)

    def test_send_without_token_omits_authorization(self):
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout=0):
            captured["request"] = request
            return mock.Mock(status=200, __enter__=lambda s: s, __exit__=mock.Mock())

        settings = notify.NtfySettings(
            url="https://ntfy.example",
            topic=SENTINEL_TOPIC,
        )
        notifier = notify.NtfyNotifier(settings, urlopen=fake_urlopen)
        notifier.send(self._notification())
        request = captured["request"]
        assert isinstance(request, notify.urllib.request.Request)
        self.assertNotIn("Authorization", request.headers)

    def test_urlerror_logs_warning_and_does_not_raise(self):
        def fake_urlopen(request, timeout=0):
            raise urllib.error.URLError("boom")

        settings = notify.NtfySettings(
            url="https://ntfy.example",
            topic=SENTINEL_TOPIC,
        )
        notifier = notify.NtfyNotifier(settings, urlopen=fake_urlopen)
        with self.assertLogs("egress_notify", level="WARNING") as captured:
            notifier.send(self._notification())
        self.assertTrue(any("reason=URLError" in line for line in captured.output))

    def test_http_error_logs_warning_with_status_code(self):
        def fake_urlopen(request, timeout=0):
            raise urllib.error.HTTPError(
                "https://ntfy.example/",
                403,
                "Forbidden",
                {},
                io.BytesIO(b""),
            )

        settings = notify.NtfySettings(
            url="https://ntfy.example",
            topic=SENTINEL_TOPIC,
        )
        notifier = notify.NtfyNotifier(settings, urlopen=fake_urlopen)
        with self.assertLogs("egress_notify", level="WARNING") as captured:
            notifier.send(self._notification())
        self.assertTrue(any("403" in line for line in captured.output))

    def test_send_async_runs_in_thread(self):
        calls: list[notify.EgressNotification] = []

        def fake_urlopen(request, timeout=0):
            return mock.Mock(status=200, __enter__=lambda s: s, __exit__=mock.Mock())

        settings = notify.NtfySettings(
            url="https://ntfy.example",
            topic=SENTINEL_TOPIC,
        )
        notifier = notify.NtfyNotifier(
            settings,
            urlopen=lambda *args, **kwargs: (
                calls.append("called") or fake_urlopen(*args, **kwargs)
            ),
        )
        n = self._notification()
        thread = notifier.send_async(n)
        join_thread_or_fail(thread, timeout=5.0, label="egress-notify")
        self.assertEqual(thread.name, "egress-notify")
        self.assertEqual(len(calls), 1)


class BindReachabilityTests(unittest.TestCase):
    def _settings(self, bind: str):
        with tempfile.TemporaryDirectory() as tmp:
            return notify.load_ntfy_settings(
                Path(tmp),
                {"NTFY_URL": "https://ntfy.example.net"},
                broker_host=bind,
                broker_port=3129,
                operator_token="op-sentinel",
            )

    def test_unspecified_bind_disables_actions(self):
        for bind in ("0.0.0.0", "::"):
            with self.subTest(bind=bind), self.assertLogs(notify.LOG, level="INFO") as logs:
                settings = self._settings(bind)
            self.assertIsNotNone(settings)
            self.assertIsNone(settings.broker_url)
            self.assertIsNone(settings.operator_token)
            self.assertTrue(any("bind_unspecified" in r.getMessage() for r in logs.records))

    def test_loopback_bind_logs_reason(self):
        with self.assertLogs(notify.LOG, level="INFO") as logs:
            settings = self._settings("127.0.0.1")
        self.assertIsNone(settings.broker_url)
        self.assertTrue(any("bind_loopback" in r.getMessage() for r in logs.records))

    def test_hostname_bind_enables_actions(self):
        settings = self._settings("wg-host.internal")
        self.assertEqual(settings.broker_url, "http://wg-host.internal:3129")
        self.assertEqual(settings.operator_token, "op-sentinel")


if __name__ == "__main__":
    unittest.main()
