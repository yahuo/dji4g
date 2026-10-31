import io
import json
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from dji4g_at import AtBusyError
from dji4g_desktop import (
    ENGINE_BUSY_EXIT, DesktopController, DesktopError, DesktopState, EmbeddedGateway,
    link_summary, main, notify_new_messages, run, safe_error, signal_bars,
)
from dji4g_gateway import GatewayStore, GatewayUnavailable, build_send_preview


class FakeGateway:
    def __init__(self):
        self.messages = []
        self.sends = []
        self.failure = None
        self.send_state = 'accepted_by_modem'
        self.ready = True
        self.cleanups = 0
        self.cleanup_result = {'deleted': 0, 'kept': 0}
        self.cleanup_failure = None

    def status(self):
        return {'connected': True, 'ready': self.ready,
                'modem': {'sim_ready': True, 'registered': True}}

    def list_messages(self, limit=50, after_id=None):
        if after_id is None:
            return sorted(self.messages, key=lambda m: m['id'], reverse=True)[:limit]
        return sorted([m for m in self.messages if m['id'] > after_id], key=lambda m: m['id'])[:limit]

    def get_message(self, identifier):
        return next(m for m in self.messages if m['id'] == identifier)

    def preview_send(self, recipient, body):
        return build_send_preview(recipient, body)

    def send(self, recipient, body, **kwargs):
        self.sends.append((recipient, body, kwargs))
        if self.failure:
            raise self.failure
        return {'state': self.send_state}

    def cleanup(self):
        self.cleanups += 1
        if self.cleanup_failure:
            raise self.cleanup_failure
        return self.cleanup_result

    def delete_message(self, identifier):
        before = len(self.messages)
        self.messages = [m for m in self.messages if m['id'] != identifier]
        return len(self.messages) < before

    def receive(self, identifier, direction='inbound'):
        self.messages.append({'id': identifier, 'peer': '+8613800000000',
                              'body': 'private verification code 123456', 'complete': True,
                              'direction': direction, 'encoding': 'gsm7', 'received_at': '2026-10-10'})


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / 'desktop'
        self.state = DesktopState(self.directory)
        self.gateway = FakeGateway()
        self.notify = Mock()
        self.now = 0
        self.controller = DesktopController(self.state, self.gateway, self.notify, lambda: self.now)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def notified(self):
        return [len(call.args[0]) for call in self.notify.call_args_list]

    def test_private_files_and_single_helper_lock(self):
        for p, mode in ((self.directory, 0o700), (self.directory / 'desktop.sqlite3', 0o600)):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), mode)
        with self.assertRaises(AtBusyError):
            DesktopState(self.directory)

    def test_first_start_suppresses_history_and_masks_inbox(self):
        self.gateway.receive(1)
        result = self.controller.poll()
        self.notify.assert_not_called()
        self.assertEqual(result['unread'], 0)
        self.assertEqual(self.state.cursor(), 1)
        self.assertNotIn('123456', json.dumps(result))
        self.assertNotIn('+8613800000000', json.dumps(result))
        self.assertNotIn('body', result['messages'][0])

    def test_new_messages_notify_once_and_unread_survives_restart(self):
        self.controller.poll()
        self.gateway.receive(1)
        self.gateway.receive(2)
        self.assertEqual(self.controller.poll()['unread'], 2)
        self.controller.poll()
        self.assertEqual(self.notified(), [2])
        self.state.close()
        self.state = DesktopState(self.directory)
        self.controller = DesktopController(self.state, self.gateway, self.notify)
        self.assertEqual(self.controller.poll()['unread'], 2)
        self.assertEqual(self.notified(), [2])

    def test_notification_failure_does_not_advance_cursor(self):
        self.controller.poll()
        self.gateway.receive(1)
        self.notify.side_effect = OSError('notification unavailable')
        with self.assertRaises(DesktopError) as raised:
            self.controller.poll()
        # Not reported as an unreachable gateway.
        self.assertIn('通知', safe_error(raised.exception))
        self.assertEqual(self.state.cursor(), 0)
        self.assertEqual(self.state.unread_ids(), set())
        self.notify.side_effect = None
        self.controller.poll()
        self.assertEqual(self.state.cursor(), 1)

    def test_backlog_paginates_without_skipping_and_outgoing_does_not_notify(self):
        self.controller.poll()
        for i in range(1, 503):
            self.gateway.receive(i)
        self.gateway.receive(503, 'outbound')
        self.controller.poll()
        self.assertEqual(self.state.cursor(), 500)
        self.controller.poll()
        self.assertEqual(self.state.cursor(), 503)
        self.assertEqual(self.state.unread_ids(), set(range(1, 503)))
        self.assertEqual(self.notified(), [500, 2])

    def test_show_is_explicit_marks_read_and_does_not_persist_body(self):
        self.controller.poll()
        self.gateway.receive(1)
        self.controller.poll()
        response = self.controller.command({'action': 'show', 'id': 1})
        self.assertIn('123456', response['message']['body'])
        self.assertEqual(response['unread'], 0)
        dump = '\n'.join(self.state.db.iterdump())
        self.assertNotIn('123456', dump)
        self.assertNotIn('+8613800000000', dump)
        with self.assertRaises(DesktopError):
            self.controller.command({'action': 'show', 'id': True})

    def test_mark_all_read_and_no_modem_delete(self):
        self.controller.poll()
        self.gateway.receive(1)
        self.controller.poll()
        self.assertEqual(self.controller.command({'action': 'mark_all_read'})['unread'], 0)
        self.assertEqual(len(self.gateway.messages), 1)

    def test_cleanup_needs_confirmation_and_a_ready_modem(self):
        for request in ({'action': 'cleanup'}, {'action': 'cleanup', 'confirmed': 'yes'}):
            with self.assertRaises(DesktopError):
                self.controller.command(request)
        self.gateway.ready = False
        with self.assertRaises(DesktopError):
            self.controller.command({'action': 'cleanup', 'confirmed': True})
        self.assertEqual(self.gateway.cleanups, 0)

    def test_cleanup_returns_counts_with_a_fresh_snapshot(self):
        self.gateway.cleanup_result = {'deleted': 35, 'kept': 1}
        result = self.controller.command({'action': 'cleanup', 'confirmed': True})
        self.assertEqual(result['type'], 'snapshot')
        self.assertEqual(result['cleaned'], {'deleted': 35, 'kept': 1})

        self.gateway.cleanup_failure = OSError('unplugged')
        with self.assertRaises(DesktopError) as raised:
            self.controller.command({'action': 'cleanup', 'confirmed': True})
        self.assertIn('本机记录不受影响', str(raised.exception))

    def test_delete_needs_confirmation_and_a_valid_id(self):
        self.controller.poll()
        self.gateway.receive(1)
        self.controller.poll()
        for request in ({'action': 'delete', 'id': 1}, {'action': 'delete', 'id': 1, 'confirmed': 'yes'},
                        {'action': 'delete', 'id': True, 'confirmed': True},
                        {'action': 'delete', 'id': '1', 'confirmed': True},
                        {'action': 'delete', 'id': 0, 'confirmed': True},
                        {'action': 'delete', 'id': 2, 'confirmed': True}):
            with self.assertRaises(DesktopError):
                self.controller.command(request)
        self.assertEqual(len(self.gateway.messages), 1)

    def test_delete_removes_one_message_and_its_unread_mark(self):
        self.controller.poll()
        self.gateway.receive(1)
        self.gateway.receive(2)
        self.controller.poll()
        # Deleting needs no modem: it only touches this machine.
        self.gateway.ready = False
        result = self.controller.command({'action': 'delete', 'id': 1, 'confirmed': True})
        self.assertEqual(result['type'], 'snapshot')
        self.assertEqual(result['deleted'], 1)
        self.assertEqual([m['id'] for m in result['messages']], [2])
        self.assertEqual(result['unread'], 1)
        self.assertEqual(self.state.unread_ids(), {2})
        self.assertEqual(self.gateway.cleanups, 0)

    def preview(self):
        return self.controller.command({'action': 'preview', 'recipient': '10086', 'body': 'test only'})

    def test_preview_never_sends_and_requires_exact_confirmation(self):
        preview = self.preview()
        self.assertEqual(self.gateway.sends, [])
        self.assertEqual(preview['preview']['segments'], 1)
        for confirmed in (None, False, 'true', 1):
            with self.assertRaises(DesktopError):
                self.controller.command({'action': 'send', 'key': preview['key'], 'confirmed': confirmed})
        self.assertEqual(self.gateway.sends, [])

    def test_confirmation_is_bound_to_immutable_preview_and_single_use(self):
        preview = self.preview()
        request = {'action': 'send', 'key': preview['key'], 'confirmed': True,
                   'recipient': '10010', 'body': 'tampered'}
        self.assertEqual(self.controller.command(request)['state'], 'accepted_by_modem')
        recipient, body, kwargs = self.gateway.sends[0]
        self.assertEqual((recipient, body), ('10086', 'test only'))
        self.assertTrue(kwargs['confirmed'])
        self.assertEqual(kwargs['idempotency_key'], preview['key'])
        with self.assertRaises(DesktopError):
            self.controller.command(request)
        self.assertEqual(len(self.gateway.sends), 1)

    def test_expired_cancelled_replaced_and_invalid_previews_cannot_send(self):
        preview = self.preview()
        self.now = 301
        with self.assertRaises(DesktopError):
            self.controller.command({'action': 'send', 'key': preview['key'], 'confirmed': True})
        preview = self.preview()
        self.controller.command({'action': 'cancel'})
        with self.assertRaises(DesktopError):
            self.controller.command({'action': 'send', 'key': preview['key'], 'confirmed': True})
        old = self.preview()
        self.preview()
        with self.assertRaises(DesktopError):
            self.controller.command({'action': 'send', 'key': old['key'], 'confirmed': True})
        with self.assertRaises(ValueError):
            self.controller.command({'action': 'preview', 'recipient': '', 'body': ''})
        self.assertIsNone(self.controller.draft)
        self.assertEqual(self.gateway.sends, [])

    def test_uncertain_submission_is_recorded_not_retried_or_leaked(self):
        self.gateway.failure = TimeoutError('sensitive body/token/number')
        preview = self.preview()
        request = {'action': 'send', 'key': preview['key'], 'confirmed': True}
        self.assertEqual(self.controller.command(request), {'type': 'sent', 'state': 'unknown'})
        with self.assertRaises(DesktopError):
            self.controller.command(request)
        self.assertEqual(len(self.gateway.sends), 1)
        self.assertEqual(self.controller.poll()['uncertain_sends'], 1)
        self.assertNotIn('test only', '\n'.join(self.state.db.iterdump()))

    def test_acknowledge_clears_the_warning_without_resending(self):
        self.gateway.failure = TimeoutError('no answer')
        preview = self.preview()
        request = {'action': 'send', 'key': preview['key'], 'confirmed': True}
        self.controller.command(request)
        self.gateway.failure = None
        self.assertEqual(self.controller.command({'action': 'acknowledge'})['uncertain_sends'], 0)
        with self.assertRaises(DesktopError):
            self.controller.command(request)
        self.assertEqual(len(self.gateway.sends), 1)
        self.assertEqual(self.state.db.execute('SELECT state FROM submissions').fetchone()[0], 'acknowledged')
        self.state.close()
        self.state = DesktopState(self.directory)
        self.controller = DesktopController(self.state, self.gateway, self.notify)
        self.assertEqual(self.controller.poll()['uncertain_sends'], 0)

    def test_interrupted_submission_recovers_as_unknown_without_resend(self):
        with self.state.db:
            self.state.db.execute("INSERT INTO submissions VALUES ('key', 'sending', 0)")
        self.state.close()
        self.state = DesktopState(self.directory)
        self.assertEqual(self.state.db.execute('SELECT state FROM submissions').fetchone()[0], 'unknown')
        self.assertEqual(self.gateway.sends, [])

    def test_stdio_errors_are_sanitized_and_eof_stops(self):
        output = io.StringIO()
        source = io.StringIO('invalid\n' + json.dumps({'action': 'preview', 'recipient': '', 'body': 'secret'}) + '\n')
        run(self.controller, source, output)
        values = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(values[0]['type'], 'snapshot')
        self.assertEqual(values[1]['type'], 'error')
        self.assertNotIn('secret', output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())

    def test_unchanged_snapshots_are_not_pushed_again(self):
        gateway = self.gateway

        class SlowSource:
            def __iter__(self):
                yield json.dumps({'action': 'refresh'}) + '\n'
                time.sleep(0.1)
                gateway.receive(1)
                time.sleep(0.1)

        output = io.StringIO()
        with patch('dji4g_desktop.POLL_SECONDS', 0.01):
            run(self.controller, SlowSource(), output)
        values = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([value.get('response', False) for value in values], [False, True, False])
        self.assertEqual(values[-1]['unread'], 1)
        self.assertEqual(self.notified(), [1])

    def test_send_error_and_unknown_commands_are_safe(self):
        self.assertNotIn('secret', safe_error(ValueError('secret')))
        self.assertIn('号码', safe_error(ValueError('secret')))
        self.assertIn('稍后', safe_error(GatewayUnavailable('secret')))
        self.assertNotIn('secret', safe_error(RuntimeError('secret')))
        with self.assertRaises(DesktopError):
            self.controller.command({'action': 'shell', 'command': 'anything'})


class LinkSummaryTests(unittest.TestCase):
    def test_signal_bars_follow_csq_and_reject_unknown(self):
        self.assertEqual([signal_bars(v) for v in (0, 2, 9, 10, 15, 20, 31)], [0, 1, 1, 2, 3, 4, 4])
        for value in (99, -1, None, True, '20', 20.0):
            self.assertIsNone(signal_bars(value))

    def test_summary_keeps_only_radio_and_storage_facts(self):
        status = {'connected': True, 'modem': {
            'signal': 18, 'registration': 'roaming', 'imsi': '460001234567890', 'iccid': '8986',
            'radio': {'rat': 'LTE', 'band': 3, 'rsrp_dbm': -107, 'sinr_db': 7, 'cell_id': 'secret'},
            'storage': {'SM': {'used': 24, 'total': 40, 'state': 'near_full'},
                        'ME': {'used': 3, 'total': 23, 'state': 'bogus'},
                        'XX': {'used': 1, 'total': 1}},
        }}
        summary = link_summary(status)
        self.assertEqual(summary, {
            'signal': 3, 'roaming': True,
            'radio': {'rat': 'LTE', 'band': 3, 'rsrp': -107, 'sinr': 7},
            'storage': {'SM': {'used': 24, 'total': 40, 'state': 'near_full'},
                        'ME': {'used': 3, 'total': 23, 'state': 'normal'}},
        })
        for private in ('460001234567890', '8986', 'secret'):
            self.assertNotIn(private, json.dumps(summary))

    def test_summary_ignores_stale_probe_and_malformed_fields(self):
        stale = {'connected': False, 'modem': {'signal': 20, 'radio': {'rat': 'LTE'}}}
        self.assertEqual(link_summary(stale), {'signal': None, 'roaming': False, 'radio': {}, 'storage': {}})
        odd = link_summary({'connected': True, 'modem': {
            'signal': 99, 'radio': {'rat': 'LTE; rm', 'band': '3', 'rsrp_dbm': True},
            'storage': {'SM': {'used': 1, 'total': 0}, 'ME': 'full'},
        }})
        self.assertEqual(odd, {'signal': None, 'roaming': False,
                               'radio': {'rat': '', 'band': None, 'rsrp': None, 'sinr': None}, 'storage': {}})
        self.assertEqual(link_summary({'connected': True})['storage'], {})


class EngineTests(unittest.TestCase):
    def test_notify_send_contains_only_counts(self):
        messages = [{'id': 1, 'peer': '+8613800000000', 'body': 'private body'}] * 2
        with patch('dji4g_desktop.subprocess.run') as call:
            notify_new_messages(messages)
        argv = call.call_args.args[0]
        self.assertEqual(argv[0], 'notify-send')
        self.assertIn('2 条新短信', argv[-1])
        self.assertNotIn('private body', ' '.join(argv))
        self.assertNotIn('8613800000000', ' '.join(argv))

    def test_embedded_gateway_reads_the_store_and_forwards_confirmed_sends(self):
        with tempfile.TemporaryDirectory() as temp:
            store = GatewayStore(Path(temp))
            try:
                core = Mock(store=store)
                core.status_snapshot.return_value = {'ready': True}
                adapter = EmbeddedGateway(core)
                self.assertEqual(adapter.status(), {'ready': True})
                self.assertEqual(adapter.list_messages(limit=1, after_id=0), [])
                with self.assertRaises(DesktopError):
                    adapter.get_message(1)
                adapter.send('+8613800000000', 'hi', idempotency_key='k', confirmed=True)
                core.send_confirmed.assert_called_once_with(
                    '+8613800000000', 'hi', idempotency_key='k', confirmed=True)
                core.cleanup_storage.return_value = {'deleted': 2, 'kept': 0}
                self.assertEqual(adapter.cleanup(), {'deleted': 2, 'kept': 0})
                self.assertFalse(adapter.delete_message(1))
            finally:
                store.close()

    def run_main(self, argv, **patches):
        gateway = Mock()
        gateway.worker_alive.return_value = False
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, redirect_stdout(output), \
                patch('dji4g_desktop.default_data_dir', return_value=Path(temp)), \
                patch('dji4g_desktop.start_gateway', **(patches or {'return_value': gateway})), \
                patch('dji4g_desktop.notify_new_messages') as notify_send, \
                patch('dji4g_desktop.run') as loop:
            code = main(argv)
            if loop.called:
                notify = loop.call_args.args[0].notify
                notify([{'id': 7, 'peer': '+8613800000000', 'body': 'code 123456',
                         'received_at': '2026-10-10T08:00:00Z', 'complete': True}])
        return code, output.getvalue(), gateway, notify_send

    def test_emit_incoming_replaces_notify_send_and_engine_stops_on_exit(self):
        code, output, gateway, notify_send = self.run_main(['--emit-incoming'])
        self.assertEqual(code, 0)
        notify_send.assert_not_called()
        self.assertEqual(json.loads(output), {'type': 'incoming', 'messages': [{
            'id': 7, 'peer': '+8613800000000', 'body': 'code 123456',
            'time': '2026-10-10T08:00:00Z', 'complete': True}]})
        gateway.stop.assert_called_once()
        gateway.store.close.assert_called_once()

    def test_default_keeps_metadata_only_notify_send(self):
        code, output, _, notify_send = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(output, '')
        self.assertEqual(len(notify_send.call_args.args[0]), 1)

    def test_engine_owned_elsewhere_is_reported_without_details(self):
        code, output, _, _ = self.run_main([], side_effect=GatewayUnavailable('/private/path'))
        self.assertEqual(code, ENGINE_BUSY_EXIT)
        value = json.loads(output)
        self.assertFalse(value['online'])
        self.assertIn('另一个短信进程', value['error'])
        self.assertNotIn('/private/path', output)


if __name__ == '__main__':
    unittest.main()
