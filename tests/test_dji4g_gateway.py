import json
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import dji4g_gateway


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


def incoming(
    pdu,
    text,
    *,
    reference=None,
    total=None,
    part=None,
    peer="+8613800000000",
    encoding="ucs2",
    timestamp="2026-07-24T12:00:00+08:00",
):
    concat = None
    if reference is not None:
        concat = SimpleNamespace(
            reference=reference,
            total_parts=total,
            part_number=part,
            reference_bits=8,
        )
    return SimpleNamespace(
        raw_pdu=pdu,
        sender=peer,
        text=text,
        encoding=encoding,
        timestamp=timestamp,
        storage="ME",
        index=7,
        status="REC READ",
        concat=concat,
    )


def delivery_report(
    raw_pdu,
    message_reference,
    *,
    outcome="delivered",
    status=0,
    recipient="+8613800000000",
    service_center_time="2026-07-24T12:00:05+08:00",
    discharge_time="2026-07-24T12:00:08+08:00",
):
    return SimpleNamespace(
        raw_pdu=raw_pdu,
        message_reference=message_reference,
        recipient=recipient,
        outcome=outcome,
        status=status,
        service_center_time=service_center_time,
        discharge_time=discharge_time,
    )


class FakeModem:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.delivery_reports = []
        self.sent = []
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.thread_ids = set()
        self._lock = threading.Lock()

    def _enter(self, name):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.thread_ids.add(threading.get_ident())
            self.calls.append(name)
        time.sleep(0.01)

    def _leave(self):
        with self._lock:
            self.active -= 1

    def status(self):
        self._enter("status")
        try:
            return {
                "connected": True,
                "sim_ready": True,
                "registered": True,
                "signal": 18,
                "imsi": "460001234567890",
            }
        finally:
            self._leave()

    def list_messages(self):
        self._enter("list")
        try:
            return list(self.messages)
        finally:
            self._leave()

    def send_message(self, recipient, body):
        self._enter("send")
        try:
            self.sent.append((recipient, body))
            return {"message_ref": len(self.sent)}
        finally:
            self._leave()

    def configure_delivery_reports(self):
        self.calls.append("configure_delivery")

    def read_delivery_reports(self):
        reports, self.delivery_reports = self.delivery_reports, []
        return reports


class MutableStatusModem(FakeModem):
    def __init__(self, status):
        super().__init__()
        self.status_value = status
        self.status_error = None

    def status(self):
        self._enter("status")
        try:
            if self.status_error is not None:
                raise self.status_error
            return dict(self.status_value)
        finally:
            self._leave()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "gateway data"
        self.store = dji4g_gateway.GatewayStore(self.data_dir)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_data_directory_and_database_are_private(self):
        self.assertEqual(stat.S_IMODE(self.data_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.database_path.stat().st_mode), 0o600)

    def test_existing_gateway_database_is_migrated_in_place(self):
        legacy_dir = Path(self.temp.name) / "legacy"
        legacy_dir.mkdir()
        database = legacy_dir / "gateway.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            """
            CREATE TABLE outbound (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                recipient TEXT NOT NULL,
                body TEXT NOT NULL,
                preview_json TEXT NOT NULL,
                state TEXT NOT NULL,
                modem_reference TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
        connection.close()

        migrated = dji4g_gateway.GatewayStore(legacy_dir)
        try:
            columns = {
                row["name"]
                for row in migrated._connection.execute("PRAGMA table_info(outbound)")
            }
            self.assertTrue(
                {"delivery_requested", "delivery_status", "delivery_updated_at"}
                <= columns
            )
            preview = dji4g_gateway.build_send_preview("10086", "hello")
            outbound, _ = migrated.create_outbound(
                "migrated-send", "10086", "hello", preview
            )
            self.assertEqual(outbound["delivery_status"], "not_requested")
        finally:
            migrated.close()

    def test_raw_pdu_is_idempotent_across_restart(self):
        first = self.store.ingest(incoming("0011AA", "first"))
        duplicate = self.store.ingest(incoming("0011AA", "first"))
        self.assertTrue(first.complete)
        self.assertFalse(first.duplicate)
        self.assertEqual(duplicate.message_id, first.message_id)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(len(self.store.list_messages()), 1)

        self.store.close()
        self.store = dji4g_gateway.GatewayStore(self.data_dir)
        after_restart = self.store.ingest(incoming("0011AA", "first"))
        self.assertTrue(after_restart.duplicate)
        self.assertEqual(len(self.store.list_messages()), 1)

    def test_single_part_concat_is_stored_as_a_complete_message(self):
        result = self.store.ingest(
            incoming("0011AB", "single", reference=7, total=1, part=1)
        )

        self.assertTrue(result.complete)
        self.assertIsNone(result.group_id)
        messages = self.store.list_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["body"], "single")

    def test_out_of_order_concat_reassembles_and_ref_reuse_starts_new_group(self):
        pending = self.store.ingest(
            incoming("00A2", "world", reference=9, total=2, part=2)
        )
        self.assertFalse(pending.complete)
        complete = self.store.ingest(
            incoming("00A1", "hello ", reference=9, total=2, part=1)
        )
        self.assertTrue(complete.complete)
        self.assertEqual(self.store.get_message(complete.message_id)["body"], "hello world")

        second_first = self.store.ingest(
            incoming("00B1", "new ", reference=9, total=2, part=1)
        )
        self.assertFalse(second_first.complete)
        second_complete = self.store.ingest(
            incoming("00B2", "message", reference=9, total=2, part=2)
        )
        self.assertTrue(second_complete.complete)
        self.assertNotEqual(second_complete.message_id, complete.message_id)
        bodies = {row["body"] for row in self.store.list_messages()}
        self.assertEqual(bodies, {"hello world", "new message"})
        self.assertEqual(self.store.count_concat_groups(), 2)

    def test_invalid_limit_is_rejected_by_store(self):
        for value in (0, -1, 501, True, "10"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.list_messages(limit=value)

    def test_after_id_returns_only_newer_messages_in_ascending_order(self):
        first = self.store.ingest(incoming("003001", "one"))
        second = self.store.ingest(incoming("003002", "two"))
        third = self.store.ingest(incoming("003003", "three"))
        rows = self.store.list_messages(after_id=first.message_id)
        self.assertEqual([row["id"] for row in rows], [second.message_id, third.message_id])

    def test_storage_filter_is_applied_before_limit_and_cursor(self):
        me_first = self.store.ingest(incoming("004001", "me one"))
        self.store.ingest(SimpleNamespace(**{**incoming("004002", "sm one").__dict__, "storage": "SM"}))
        me_second = self.store.ingest(incoming("004003", "me two"))
        rows = self.store.list_messages(limit=1, storage="ME")
        self.assertEqual([row["id"] for row in rows], [me_second.message_id])
        rows = self.store.list_messages(after_id=me_first.message_id, storage="ME")
        self.assertEqual([row["id"] for row in rows], [me_second.message_id])

    def test_concat_group_requires_matching_encoding_and_nearby_timestamp(self):
        self.store.ingest(
            incoming("001101", "A", reference=4, total=2, part=1, encoding="gsm7")
        )
        different_encoding = self.store.ingest(
            incoming("001102", "B", reference=4, total=2, part=2, encoding="ucs2")
        )
        self.assertFalse(different_encoding.complete)
        self.assertEqual(self.store.count_concat_groups(), 2)

        self.store.ingest(
            incoming(
                "002201",
                "C",
                reference=5,
                total=2,
                part=1,
                timestamp="2026-07-24T12:00:00+08:00",
            )
        )
        far_later = self.store.ingest(
            incoming(
                "002202",
                "D",
                reference=5,
                total=2,
                part=2,
                timestamp="2026-07-24T12:16:00+08:00",
            )
        )
        self.assertFalse(far_later.complete)
        self.assertEqual(self.store.count_concat_groups(), 4)

    def test_interleaved_reused_concat_ref_uses_nearest_timestamp(self):
        self.store.ingest(
            incoming(
                "0060A1",
                "A1",
                reference=7,
                total=2,
                part=1,
                timestamp="2026-07-24T12:00:00+08:00",
            )
        )
        self.store.ingest(
            incoming(
                "0060B1",
                "B1",
                reference=7,
                total=2,
                part=1,
                timestamp="2026-07-24T12:01:00+08:00",
            )
        )
        self.store.ingest(
            incoming(
                "0060A2",
                "A2",
                reference=7,
                total=2,
                part=2,
                timestamp="2026-07-24T12:00:01+08:00",
            )
        )
        self.store.ingest(
            incoming(
                "0060B2",
                "B2",
                reference=7,
                total=2,
                part=2,
                timestamp="2026-07-24T12:01:01+08:00",
            )
        )
        self.assertEqual(
            {row["body"] for row in self.store.list_messages()}, {"A1A2", "B1B2"}
        )

    def test_outbound_state_and_idempotency_survive_restart(self):
        preview = dji4g_gateway.build_send_preview("10086", "hello")
        outbound, created = self.store.create_outbound(
            "persistent-send", "10086", "hello", preview
        )
        self.assertTrue(created)
        self.store.update_outbound(outbound["id"], "accepted_by_modem")
        self.store.close()
        self.store = dji4g_gateway.GatewayStore(self.data_dir)
        repeated, created = self.store.create_outbound(
            "persistent-send", "10086", "hello", preview
        )
        self.assertFalse(created)
        self.assertEqual(repeated["state"], "accepted_by_modem")
        with self.assertRaises(dji4g_gateway.IdempotencyConflict):
            self.store.create_outbound(
                "persistent-send", "10086", "different", preview
            )

    def test_segment_reports_are_deduplicated_and_aggregated(self):
        preview = dji4g_gateway.build_send_preview(
            "+8613800000000", "中" * 71
        )
        outbound, _ = self.store.create_outbound(
            "delivery-long", "+8613800000000", "中" * 71, preview
        )
        submitted = self.store.record_outbound_submission(
            outbound["id"], (42, 43)
        )
        self.assertEqual(submitted["delivery_status"], "pending")

        first = self.store.ingest_delivery_report(
            delivery_report("AA01", 42)
        )
        self.assertTrue(first["matched"])
        self.assertEqual(first["delivery_status"], "pending")
        duplicate = self.store.ingest_delivery_report(
            delivery_report("AA01", 42)
        )
        self.assertTrue(duplicate["duplicate"])

        final = self.store.ingest_delivery_report(
            delivery_report("AA02", 43)
        )
        self.assertEqual(final["delivery_status"], "delivered")
        events = self.store.list_delivery_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "delivered")
        encoded = json.dumps(events[0])
        self.assertNotIn("+8613800000000", encoded)
        self.assertNotIn("中", encoded)
        self.assertNotIn("AA02", encoded)

    def test_failed_segment_fails_the_whole_outbound(self):
        preview = dji4g_gateway.build_send_preview("+8613800000000", "hello")
        outbound, _ = self.store.create_outbound(
            "delivery-failed", "+8613800000000", "hello", preview
        )
        self.store.record_outbound_submission(outbound["id"], (17,))

        result = self.store.ingest_delivery_report(
            delivery_report("BB01", 17, outcome="failed", status=0x40)
        )

        self.assertEqual(result["delivery_status"], "failed")
        self.assertEqual(self.store.list_delivery_events()[0]["status"], "failed")

    def test_device_state_events_are_persistent_deduplicated_and_metadata_only(self):
        self.assertIsNone(
            self.store.record_device_state(
                "sim", "ready", event_type="sim.readiness", payload={}
            )
        )
        self.assertIsNone(
            self.store.record_device_state(
                "sim", "ready", event_type="sim.readiness", payload={}
            )
        )

        degraded = self.store.record_device_state(
            "sim",
            "not_ready",
            event_type="sim.readiness",
            payload={
                "reason": "sim_not_ready",
                "body": "secret",
                "phone": "+8613800000000",
                "raw_response": "+CPIN: SIM PIN",
            },
        )

        self.assertEqual(degraded["state"], "not_ready")
        self.assertEqual(
            degraded["payload"],
            {"reason": "sim_not_ready", "previous_state": "ready"},
        )
        self.assertIsNone(
            self.store.record_device_state(
                "sim", "not_ready", event_type="sim.readiness", payload={}
            )
        )
        recovered = self.store.record_device_state(
            "sim", "ready", event_type="sim.readiness", payload={}
        )
        self.assertEqual(recovered["payload"]["previous_state"], "not_ready")

        self.store.close()
        self.store = dji4g_gateway.GatewayStore(self.data_dir)
        self.assertEqual(self.store.get_device_state("sim")["state"], "ready")
        self.assertEqual(
            [event["state"] for event in self.store.list_device_events(after_id=0)],
            ["not_ready", "ready"],
        )

    def test_ambiguous_reference_is_persisted_without_guessing(self):
        preview = dji4g_gateway.build_send_preview("+8613800000000", "hello")
        first, _ = self.store.create_outbound(
            "delivery-ambiguous-1", "+8613800000000", "hello", preview
        )
        second, _ = self.store.create_outbound(
            "delivery-ambiguous-2", "+8613800000000", "hello", preview
        )
        self.store.record_outbound_submission(first["id"], (9,))
        self.store.record_outbound_submission(second["id"], (9,))

        result = self.store.ingest_delivery_report(delivery_report("CC01", 9))

        self.assertFalse(result["matched"])
        self.assertEqual(result["match_status"], "ambiguous")
        self.assertEqual(self.store.list_delivery_events(), [])


class GatewayWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = dji4g_gateway.GatewayStore(Path(self.temp.name) / "data")

    def tearDown(self):
        if hasattr(self, "gateway"):
            self.gateway.stop()
        self.store.close()
        self.temp.cleanup()

    def test_modem_calls_are_serial_and_run_on_one_worker(self):
        modem = FakeModem()
        self.gateway = dji4g_gateway.SmsGateway(
            self.store,
            modem_factory=lambda: modem,
            poll_interval=60,
            status_interval=60,
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))

        threads = []
        for index in range(8):
            target = self.gateway.poll_now if index % 2 else self.gateway.refresh_status
            thread = threading.Thread(target=target)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(modem.max_active, 1)
        self.assertEqual(len(modem.thread_ids), 1)

    def test_malformed_inbound_message_does_not_block_later_messages(self):
        modem = FakeModem(
            [
                incoming("001101", "first"),
                SimpleNamespace(raw_pdu="", sender="10086", text="broken"),
                incoming("001102", "second"),
            ]
        )
        self.gateway = dji4g_gateway.SmsGateway(
            self.store,
            modem_factory=lambda: modem,
            poll_interval=60,
            status_interval=60,
        )

        self.gateway.start()

        self.assertTrue(wait_until(lambda: len(self.store.list_messages()) == 2))
        self.assertEqual(
            [message["body"] for message in self.store.list_messages()],
            ["second", "first"],
        )

    def test_disconnect_uses_backoff_then_recovers(self):
        modem = FakeModem()
        attempts = []

        def factory():
            attempts.append(time.monotonic())
            if len(attempts) == 1:
                raise OSError("not connected")
            return modem

        self.gateway = dji4g_gateway.SmsGateway(
            self.store,
            modem_factory=factory,
            poll_interval=60,
            status_interval=60,
            reconnect_initial=0.01,
            reconnect_max=0.02,
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))
        self.assertGreaterEqual(len(attempts), 2)
        self.assertGreaterEqual(attempts[1] - attempts[0], 0.009)

    def test_send_failure_is_unknown_only_when_submission_is_uncertain(self):
        class SubmittedError(RuntimeError):
            submitted = True

        class FailingModem(FakeModem):
            def __init__(self):
                super().__init__()
                self.errors = [RuntimeError("before submit"), SubmittedError("after submit")]

            def send_message(self, recipient, body):
                self._enter("send")
                try:
                    raise self.errors.pop(0)
                finally:
                    self._leave()

        modem = FailingModem()
        self.gateway = dji4g_gateway.SmsGateway(
            self.store, modem_factory=lambda: modem, poll_interval=60, status_interval=60
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))
        failed = self.gateway.send_confirmed(
            "+8613800000000", "first", idempotency_key="failed-1", confirmed=True
        )
        uncertain = self.gateway.send_confirmed(
            "+8613800000000", "second", idempotency_key="unknown-1", confirmed=True
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(uncertain["state"], "unknown")
        self.assertEqual(
            [
                event
                for event in self.store.list_device_events(after_id=0)
                if event["event_type"] == "device.connection"
            ],
            [],
        )

    def test_send_is_validated_confirmed_and_idempotent(self):
        modem = FakeModem([])
        self.gateway = dji4g_gateway.SmsGateway(
            self.store, modem_factory=lambda: modem, poll_interval=60, status_interval=60
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))
        with self.assertRaises(ValueError):
            self.gateway.preview_send("not-a-number", "hello")
        preview = self.gateway.preview_send("+8613800000000", "hello")
        self.assertNotEqual(preview["recipient"], "+8613800000000")
        with self.assertRaises(ValueError):
            self.gateway.preview_send("+8613800000000", "中" * 671)

        def send(body="hello", confirmed=True):
            return self.gateway.send_confirmed(
                "+8613800000000", body, idempotency_key="send-1", confirmed=confirmed
            )

        with self.assertRaises(ValueError):
            send(confirmed=False)
        self.assertEqual(modem.sent, [])
        sent = send()
        self.assertEqual(sent["state"], "accepted_by_modem")
        self.assertEqual(sent["delivery_status"], "pending")
        self.assertEqual(send()["id"], sent["id"])
        self.assertEqual(len(modem.sent), 1)
        with self.assertRaises(dji4g_gateway.IdempotencyConflict):
            send(body="different")
        self.assertEqual(len(modem.sent), 1)

    def test_start_recovers_interrupted_outbound_without_retry(self):
        preview = dji4g_gateway.build_send_preview("10086", "hello")
        queued, _ = self.store.create_outbound("queued-crash", "10086", "hello", preview)
        sending, _ = self.store.create_outbound("sending-crash", "10086", "hello", preview)
        self.store.update_outbound(sending["id"], "sending")
        modem = FakeModem()
        self.gateway = dji4g_gateway.SmsGateway(
            self.store, modem_factory=lambda: modem, poll_interval=60, status_interval=60
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))
        recovered_queued, created = self.store.create_outbound(
            "queued-crash", "10086", "hello", preview
        )
        self.assertFalse(created)
        self.assertEqual(recovered_queued["state"], "failed")
        recovered_sending, created = self.store.create_outbound(
            "sending-crash", "10086", "hello", preview
        )
        self.assertFalse(created)
        self.assertEqual(recovered_sending["state"], "unknown")
        self.assertEqual(modem.sent, [])

    def test_start_refuses_while_previous_worker_is_still_stopping(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingModem(FakeModem):
            def send_message(self, recipient, body):
                entered.set()
                release.wait(2)
                return {"message_ref": 1}

        modem = BlockingModem()
        self.gateway = dji4g_gateway.SmsGateway(
            self.store, modem_factory=lambda: modem, poll_interval=60, status_interval=60
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))
        sender = threading.Thread(
            target=lambda: self.gateway.send_confirmed(
                "10086", "hello", idempotency_key="blocking", confirmed=True
            )
        )
        sender.start()
        self.assertTrue(entered.wait(1))
        self.gateway.stop(timeout=0.01)
        with self.assertRaises(dji4g_gateway.GatewayUnavailable):
            self.gateway.start()
        release.set()
        sender.join(2)
        self.gateway.stop(timeout=2)
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))

    def test_status_filters_sensitive_modem_fields(self):
        modem = FakeModem()
        self.gateway = dji4g_gateway.SmsGateway(
            self.store, modem_factory=lambda: modem, poll_interval=60, status_interval=60
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))
        encoded = json.dumps(self.gateway.status_snapshot())
        self.assertNotIn("460001234567890", encoded)
        self.assertNotIn("imsi", encoded.lower())

        self.gateway.stop()
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))

    def test_status_keeps_only_structured_radio_and_storage_fields(self):
        modem = MutableStatusModem(
            {
                "connected": True,
                "ready": True,
                "at_ready": True,
                "sim_ready": True,
                "registered": True,
                "registration": "home",
                "radio": {
                    "state": "idle",
                    "service": "registered",
                    "rat": "LTE",
                    "duplex": "FDD",
                    "band": 3,
                    "channel": 1850,
                    "rsrp_dbm": -69,
                    "rsrq_db": -6,
                    "rssi_dbm": -42,
                    "sinr_db": 20,
                    "cell_id": "1A2B3C4",
                    "tac": "5D6E",
                    "mcc": 460,
                },
                "storage": {
                    "ME": {"used": 4, "total": 10, "raw": "secret"},
                    "SM": {"used": 1, "total": 10},
                    "MT": {"used": 5, "total": 20},
                },
                "imsi": "460001234567890",
            }
        )
        self.gateway = dji4g_gateway.SmsGateway(
            self.store, modem_factory=lambda: modem, poll_interval=60, status_interval=60
        )

        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))

        status = self.gateway.status_snapshot()["modem"]
        self.assertEqual(status["radio"]["channel"], 1850)
        self.assertEqual(status["storage"]["ME"]["state"], "normal")
        self.assertEqual(status["storage"]["ME"]["free"], 6)
        encoded = json.dumps(status)
        for forbidden in ("1A2B3C4", "5D6E", "460001234567890", "cell_id", "tac", "mcc", "raw", "MT"):
            self.assertNotIn(forbidden, encoded)

    def test_sim_registration_and_connection_events_are_debounced_and_recover(self):
        modem = MutableStatusModem(
            {
                "connected": True,
                "ready": True,
                "sim_ready": True,
                "registered": True,
                "registration": "home",
            }
        )
        self.gateway = dji4g_gateway.SmsGateway(
            self.store,
            modem_factory=lambda: modem,
            poll_interval=60,
            status_interval=60,
            reconnect_initial=0.001,
            reconnect_max=0.002,
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))
        self.assertEqual(self.store.list_device_events(), [])

        modem.status_value["sim_ready"] = False
        modem.status_value["ready"] = False
        self.gateway.refresh_status()
        self.assertEqual(self.store.list_device_events(), [])
        self.gateway.refresh_status()
        self.assertEqual(self.store.list_device_events()[0]["state"], "not_ready")

        modem.status_value["sim_ready"] = True
        modem.status_value["ready"] = True
        self.gateway.refresh_status()
        self.assertEqual(self.store.list_device_events()[0]["state"], "ready")

        modem.status_value["registration"] = "searching"
        modem.status_value["registered"] = False
        self.gateway.refresh_status()
        self.gateway.refresh_status()
        self.assertEqual(self.store.list_device_events()[0]["state"], "searching")

        modem.status_error = OSError("USB serial 123456 unavailable")
        for _ in range(2):
            with self.assertRaises(OSError):
                self.gateway.refresh_status()
        events = self.store.list_device_events(after_id=0)
        self.assertEqual(
            [event["state"] for event in events if event["event_type"] == "device.connection"],
            ["disconnected"],
        )
        self.assertNotIn("123456", json.dumps(events))

        modem.status_error = None
        modem.status_value["registration"] = "home"
        modem.status_value["registered"] = True
        self.gateway.refresh_status()
        events = self.store.list_device_events(after_id=0)
        self.assertEqual(
            [event["state"] for event in events if event["event_type"] == "device.connection"],
            ["disconnected", "connected"],
        )

    def test_storage_capacity_uses_hysteresis_and_full_is_immediate(self):
        modem = MutableStatusModem(
            {
                "connected": True,
                "ready": True,
                "sim_ready": True,
                "registered": True,
                "registration": "home",
                "storage": {"ME": {"used": 0, "total": 10}},
            }
        )
        self.gateway = dji4g_gateway.SmsGateway(
            self.store, modem_factory=lambda: modem, poll_interval=60, status_interval=60
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))

        modem.status_value["storage"]["ME"]["used"] = 8
        self.gateway.refresh_status()
        self.assertEqual(self.store.list_device_events(), [])
        self.gateway.refresh_status()
        self.assertEqual(self.store.list_device_events()[0]["state"], "near_full")

        modem.status_value["storage"]["ME"]["used"] = 7
        self.gateway.refresh_status()
        self.assertEqual(self.store.list_device_events()[0]["state"], "normal")

        modem.status_value["storage"]["ME"]["used"] = 10
        self.gateway.refresh_status()
        newest = self.store.list_device_events()[0]
        self.assertEqual(newest["state"], "full")
        self.assertEqual(newest["payload"]["free"], 0)

    def test_worker_configures_and_persists_delivery_reports(self):
        modem = FakeModem()
        self.gateway = dji4g_gateway.SmsGateway(
            self.store, modem_factory=lambda: modem, poll_interval=60, status_interval=60
        )
        self.gateway.start()
        self.assertTrue(wait_until(lambda: self.gateway.ready()))
        sent = self.gateway.send_confirmed(
            "+8613800000000", "hello", idempotency_key="delivery-worker", confirmed=True
        )
        modem.delivery_reports.append(delivery_report("DD01", 1))

        self.gateway.poll_now()

        self.assertIn("configure_delivery", modem.calls)
        events = self.store.list_delivery_events()
        self.assertEqual(events[0]["outbound_id"], sent["id"])
        self.assertEqual(events[0]["status"], "delivered")
