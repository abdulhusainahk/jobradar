"""Behavioral regression coverage for the durable notification outbox."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import requests

from jobradar import notify, state


def job(identifier="one", **changes):
    return {"id": identifier, "company": "Example", "title": f"DevOps {identifier}",
            "location": "Remote", "url": f"https://example.test/jobs/{identifier}",
            "_india": True, **changes}


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = {"ok": True} if body is None else body

    def json(self):
        return self.body


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "seen_jobs.json"
        self.enterContext(patch.object(state, "STATE_FILE", str(self.path)))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(notify.time, "sleep"))

    def enable_telegram(self):
        os.environ.update(TELEGRAM_BOT_TOKEN="private-token", TELEGRAM_CHAT_ID="123")

    def enable_email(self):
        os.environ.update(EMAIL_USER="sender@example.test", EMAIL_APP_PASSWORD="secret",
                          EMAIL_TO="")

    def queued(self, jobs, channels):
        current = state.load()
        state.initialize(current, [])
        for alert in jobs:
            state.enqueue(current, alert, channels)
        state.save(current)
        return current

    def deliver(self, current):
        alerts = state.pending_jobs(current)
        for alert in alerts:
            state.enqueue(current, alert, notify.configured_channels())
        routes = {state.key(alert): state.channels_due(current, alert) for alert in alerts}
        state.save(current)
        result = notify.dispatch(alerts, routes)
        state.record_delivery(current, result)
        state.save(current)
        return result

    def test_failed_delivery_retries_saved_alert_without_provider_role(self):
        self.enable_telegram()
        alert = job()
        current = self.queued([alert], ["telegram"])
        with patch.object(notify.requests, "post", side_effect=requests.ConnectionError("offline")):
            first = self.deliver(current)
        self.assertEqual(first, {"telegram": {state.key(alert): False}})
        recovered = state.load()
        self.assertNotIn(state.key(alert), recovered["seen"])
        self.assertEqual(state.pending_jobs(recovered), [alert])
        with patch.object(notify.requests, "post", return_value=Response()):
            second = self.deliver(recovered)
        self.assertEqual(second, {"telegram": {state.key(alert): True}})
        self.assertEqual(state.pending_jobs(state.load()), [])
        self.assertFalse(state.is_new(state.load(), alert))

    def test_successful_channel_is_not_repeated_after_other_channel_fails(self):
        self.enable_telegram()
        self.enable_email()
        alert = job()
        current = self.queued([alert], ["telegram", "email"])
        telegram_messages = []

        def send_telegram(url, **kwargs):
            telegram_messages.append(kwargs["json"]["text"])
            return Response()

        with patch.object(notify.requests, "post", side_effect=send_telegram):
            with patch.object(notify.smtplib, "SMTP", side_effect=OSError("offline")):
                self.deliver(current)
            recovered = state.load()
            self.assertEqual(state.channels_due(recovered, alert), ["email"])
            with patch.object(notify.smtplib, "SMTP") as smtp:
                smtp.return_value.__enter__.return_value.sendmail.return_value = {}
                self.deliver(recovered)
        self.assertEqual(len(telegram_messages), 1)
        self.assertIn("DevOps one", telegram_messages[0])
        self.assertEqual(state.pending_jobs(state.load()), [])

    def test_partial_chunks_only_retry_unsent_jobs(self):
        self.enable_telegram()
        alerts = [job(str(index), ai_note="analysis " * 250) for index in range(3)]
        current = self.queued(alerts, ["telegram"])
        accepted = []
        responses = iter([Response(), Response(500), Response()])

        def first_run(url, **kwargs):
            response = next(responses)
            if response.status_code == 200:
                accepted.append(kwargs["json"]["text"])
            return response

        with patch.object(notify.requests, "post", side_effect=first_run):
            first = self.deliver(current)
        self.assertEqual(first["telegram"], {state.key(alerts[0]): True,
                                           state.key(alerts[1]): False,
                                           state.key(alerts[2]): True})
        recovered = state.load()
        self.assertEqual(state.pending_jobs(recovered), [alerts[1]])

        def retry(url, **kwargs):
            accepted.append(kwargs["json"]["text"])
            return Response()

        with patch.object(notify.requests, "post", side_effect=retry):
            self.deliver(recovered)
        for alert in alerts:
            self.assertEqual(sum(alert["title"] in text for text in accepted), 1)
        self.assertEqual(state.pending_jobs(state.load()), [])

    def test_missing_channels_do_not_finalize_and_can_be_bound_later(self):
        alert = job()
        current = self.queued([alert], [])
        self.assertEqual(self.deliver(current), {})
        self.assertNotIn(state.key(alert), state.load()["seen"])
        self.enable_telegram()
        with patch.object(notify.requests, "post", return_value=Response()):
            result = self.deliver(state.load())
        self.assertEqual(result, {"telegram": {state.key(alert): True}})
        self.assertEqual(state.pending_jobs(state.load()), [])

    def test_required_unconfigured_channel_remains_pending(self):
        alert = job()
        current = self.queued([alert], ["email"])
        result = self.deliver(current)
        self.assertEqual(result, {"email": {state.key(alert): False}})
        self.assertEqual(state.channels_due(state.load(), alert), ["email"])
        self.assertNotIn(state.key(alert), state.load()["seen"])

    def test_legacy_history_migrates_without_rebaselining(self):
        old = {"seen": {state.key(job()): "2026-01-01T00:00:00+00:00"}}
        self.path.write_text(json.dumps(old), encoding="utf-8")
        current = state.load()
        self.assertTrue(current["initialized"])
        self.assertFalse(state.is_new(current, job()))
        state.save(current)
        self.assertEqual(state.load()["seen"], old["seen"])
        self.path.write_text('{"seen": {}}', encoding="utf-8")
        self.assertTrue(state.load()["initialized"])

    def test_empty_baseline_is_explicit_and_persistent(self):
        current = state.load()
        self.assertFalse(current["initialized"])
        with self.assertRaises(state.StateError):
            state.enqueue(current, job(), [])
        state.initialize(current, [])
        state.save(current)
        self.assertTrue(state.load()["initialized"])
        with self.assertRaises(state.StateError):
            state.initialize(current, [job()])
        self.assertTrue(state.is_new(current, job()))

    def test_corrupt_state_fails_instead_of_resetting_history(self):
        documents = ["not json", "[]", "{}", '{"seen": []}', '{"seen": {"x": null}}',
                     '{"version": 3, "seen": {}}',
                     '{"version": 2, "initialized": false, "seen": {"x": "date"}, "pending": {}}']
        for document in documents:
            with self.subTest(document=document):
                self.path.write_text(document, encoding="utf-8")
                with self.assertRaises(state.StateError):
                    state.load()
                self.assertEqual(self.path.read_text(encoding="utf-8"), document)

    def test_interrupted_atomic_save_preserves_previous_state(self):
        current = self.queued([job()], ["telegram"])
        previous = self.path.read_bytes()
        state.record_delivery(current, {"telegram": {state.key(job()): True}})
        with patch.object(state.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                state.save(current)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(state.pending_jobs(state.load()), [job()])
        self.assertEqual(list(self.path.parent.glob(".jobradar-*.tmp")), [])

    def test_overlong_blocks_and_entities_keep_valid_message_boundaries(self):
        self.enable_telegram()
        alerts = [job("huge", title="<&\U0001f680" * 4000, ai_note="x" * 10000),
                  job("long-url", url="https://example.test/" + "a" * 5000),
                  job("normal", title="SRE & DevOps <platform>",
                      url="https://example.test/?one=1&two=2")]
        messages = []

        def send(url, **kwargs):
            messages.append(kwargs["json"]["text"])
            return Response()

        with patch.object(notify.requests, "post", side_effect=send):
            result = notify.dispatch(alerts)
        self.assertEqual(result, {"telegram": {state.key(alert): True for alert in alerts}})
        for text in messages:
            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3800)
            root = ET.fromstring("<root>" + text + "</root>")
            self.assertTrue(all(node.tag in {"root", "b", "i", "a"} for node in root.iter()))
        roots = [ET.fromstring("<root>" + text + "</root>") for text in messages]
        self.assertIn(alerts[2]["url"], [link.attrib["href"] for root in roots for link in root.iter("a")])
        self.assertIn(alerts[2]["title"], "".join("".join(root.itertext()) for root in roots))

    def test_rate_limit_exhaustion_is_bounded_and_failure_is_pending(self):
        self.enable_telegram()
        current = self.queued([job()], ["telegram"])
        with patch.object(notify.requests, "post", return_value=Response(
                429, {"parameters": {"retry_after": 999999}})) as post:
            result = self.deliver(current)
        self.assertEqual(post.call_count, 4)
        self.assertEqual(result, {"telegram": {state.key(job()): False}})
        self.assertEqual(state.pending_jobs(state.load()), [job()])

    def test_telegram_errors_never_expose_token_bearing_urls(self):
        self.enable_telegram()
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with patch.object(notify.requests, "post", side_effect=requests.ConnectionError(
                    "https://api.telegram.org/botprivate-token/sendMessage")):
                result = notify.dispatch([job()])
        self.assertEqual(result, {"telegram": {state.key(job()): False}})
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("api.telegram.org", output.getvalue())
        self.assertIn("ConnectionError", output.getvalue())

    def test_telegram_http_success_with_api_rejection_is_failure(self):
        self.enable_telegram()
        with patch.object(notify.requests, "post", return_value=Response(200, {"ok": False})):
            result = notify.dispatch([job()])
        self.assertEqual(result, {"telegram": {state.key(job()): False}})

    def test_empty_email_destination_falls_back_and_refusals_fail(self):
        self.enable_email()
        deliveries = []
        with patch.object(notify.smtplib, "SMTP") as smtp:
            connection = smtp.return_value.__enter__.return_value

            def accept(sender, recipients, message):
                deliveries.append((recipients, message))
                return {}

            connection.sendmail.side_effect = accept
            result = notify.dispatch([job()])
            self.assertEqual(result, {"email": {state.key(job()): True}})
            self.assertEqual(deliveries[0][0], ["sender@example.test"])
            self.assertIn("To: sender@example.test", deliveries[0][1])
            connection.sendmail.side_effect = lambda *args: {"sender@example.test": (550, b"refused")}
            self.assertEqual(notify.dispatch([job()]), {"email": {state.key(job()): False}})


if __name__ == "__main__":
    unittest.main()
