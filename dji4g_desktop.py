#!/usr/bin/env python3
"""JSON-lines SMS engine for the desktop UIs (Omarchy panel, macOS menu bar app).

The modem worker and message store run in this process; there is no network
API. stdin/stdout carry UI requests and responses, not shell commands. No
message content is logged. Only the user-confirmed send operation sends SMS,
only a user-confirmed cleanup deletes SMS from the module, after they are
stored locally, and only a user-confirmed delete removes one SMS from this
machine.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from dji4g_at import AtBusyError, InterProcessLock
from dji4g_gateway import (
    GatewayConfig, GatewayStore, GatewayUnavailable, SmsGateway, mask_peer,
)
from dji4g_modem import SmsModem
from dji4g_paths import default_data_dir

# Store reads are local; the modem itself is polled by the worker thread.
POLL_SECONDS = 2.0
PREVIEW_TTL = 300.0
# Exit status when another engine or the CLI owns the data or the modem; the
# UIs keep showing the engine's own reason instead of a generic crash notice.
ENGINE_BUSY_EXIT = 75  # EX_TEMPFAIL
MAX_INPUT_BYTES = 64 * 1024


class DesktopError(RuntimeError):
    """Only fixed, non-sensitive messages may cross the UI boundary."""


class DesktopState:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = InterProcessLock(directory / "desktop.lock").acquire()
        try:
            path = directory / "desktop.sqlite3"
            self.db = sqlite3.connect(path)
            os.chmod(path, 0o600)
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS unread (id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS submissions (
                    key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            # A process may have died after submitting an SMS. Never retry it.
            with self.db:
                self.db.execute("UPDATE submissions SET state = 'unknown' WHERE state = 'sending'")
        except BaseException:
            self.lock.close()
            raise

    def cursor(self) -> int | None:
        row = self.db.execute("SELECT value FROM state WHERE key = 'cursor'").fetchone()
        return int(row[0]) if row else None

    def set_cursor(self, value: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES ('cursor', ?)", (value,))

    def unread_ids(self) -> set[int]:
        return {row[0] for row in self.db.execute("SELECT id FROM unread")}

    def mark_read(self, identifier: int | None = None) -> None:
        with self.db:
            if identifier is None:
                self.db.execute("DELETE FROM unread")
            else:
                self.db.execute("DELETE FROM unread WHERE id = ?", (identifier,))

    def close(self) -> None:
        self.db.close()
        self.lock.close()


def notify_new_messages(messages: list[dict[str, Any]]) -> None:
    # Metadata only, including in the desktop's notification history/lock screen.
    subprocess.run(
        ["notify-send", "--app-name=DJI 短信", "--icon=mail-unread", "--",
         "收到新短信", f"{len(messages)} 条新短信，请打开短信面板查看。"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=5,
    )


def signal_bars(value: Any) -> int | None:
    """Map an AT+CSQ reading (0-31, 99 = unknown) onto 0-4 bars."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 31:
        return None
    return sum(value >= threshold for threshold in (2, 10, 15, 20))


def link_summary(status: dict[str, Any]) -> dict[str, Any]:
    """Radio and storage facts for the panel; contains no identifiers."""
    if not status.get("connected"):
        # The gateway keeps its last probe after the module is unplugged.
        return {"signal": None, "roaming": False, "radio": {}, "storage": {}}
    modem = status.get("modem") or {}
    radio = modem.get("radio") if isinstance(modem.get("radio"), dict) else {}
    stores = modem.get("storage") if isinstance(modem.get("storage"), dict) else {}

    def number(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    rat = radio.get("rat")
    storage = {}
    for name in ("SM", "ME"):
        store = stores.get(name)
        if not isinstance(store, dict):
            continue
        used, total = number(store.get("used")), number(store.get("total"))
        if used is None or not total:
            continue
        state = store.get("state")
        storage[name] = {"used": used, "total": total,
                         "state": state if state in {"near_full", "full"} else "normal"}
    return {
        "signal": signal_bars(modem.get("signal")),
        "roaming": modem.get("registration") == "roaming",
        "radio": {
            "rat": rat if isinstance(rat, str) and rat.isalnum() and len(rat) <= 8 else "",
            "band": number(radio.get("band")),
            "rsrp": number(radio.get("rsrp_dbm")),
            "sinr": number(radio.get("sinr_db")),
        },
        "storage": storage,
    }


def write_line(output, value: dict[str, Any]) -> None:
    output.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()


def incoming_event(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Full sender and text, for a UI that posts its own notifications."""
    return {"type": "incoming", "messages": [{
        "id": m["id"], "peer": m.get("peer", ""), "body": m.get("body", ""),
        "time": m.get("received_at") or m.get("created_at") or "",
        "complete": bool(m.get("complete")),
    } for m in messages]}


class EmbeddedGateway:
    """The controller's view of the in-process modem worker and store."""

    def __init__(self, gateway: SmsGateway):
        self.gateway = gateway

    def status(self) -> dict[str, Any]:
        return self.gateway.status_snapshot()

    def list_messages(self, limit: int = 50, after_id: int | None = None) -> list[dict[str, Any]]:
        return self.gateway.store.list_messages(limit, after_id=after_id)

    def get_message(self, identifier: int) -> dict[str, Any]:
        message = self.gateway.store.get_message(identifier)
        if message is None:
            raise DesktopError("短信不存在")
        return message

    def preview_send(self, recipient: Any, body: Any) -> dict[str, Any]:
        return self.gateway.preview_send(recipient, body)

    def send(self, recipient: Any, body: Any, *, idempotency_key: str, confirmed: bool) -> dict[str, Any]:
        return self.gateway.send_confirmed(
            recipient, body, idempotency_key=idempotency_key, confirmed=confirmed,
        )

    def cleanup(self) -> dict[str, int]:
        return self.gateway.cleanup_storage()

    def delete_message(self, identifier: int) -> bool:
        return self.gateway.store.delete_message(identifier)


class DesktopController:
    def __init__(
        self, state: DesktopState, gateway: Any,
        notify: Callable[[list[dict[str, Any]]], None] = notify_new_messages,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.state = state
        self.gateway = gateway
        self.notify = notify
        self.clock = clock
        self.draft: dict[str, Any] | None = None

    def poll(self) -> dict[str, Any]:
        gateway = self.gateway
        status = gateway.status()
        cursor = self.state.cursor()
        if cursor is None:
            # First start adopts the current head; historical SMS do not notify.
            latest = gateway.list_messages(limit=1)
            with self.state.db:
                self.state.set_cursor(max((int(m["id"]) for m in latest), default=0))
        else:
            messages = gateway.list_messages(limit=500, after_id=cursor)
            if messages:
                latest = max(int(message["id"]) for message in messages)
                if latest <= cursor:
                    raise DesktopError("短信存储返回了无效的消息游标")
                incoming = [m for m in messages if m.get("direction") == "inbound"]
                with self.state.db:
                    for message in incoming:
                        self.state.db.execute("INSERT OR IGNORE INTO unread VALUES (?)", (message["id"],))
                    if incoming:
                        try:
                            self.notify(incoming)
                        except (OSError, subprocess.SubprocessError) as error:
                            # Rolls back the transaction; the next poll notifies again.
                            raise DesktopError("无法发送桌面通知，稍后自动重试") from error
                    self.state.set_cursor(latest)
        return self.snapshot(gateway, status)

    def snapshot(self, gateway=None, status=None) -> dict[str, Any]:
        gateway = gateway or self.gateway
        status = status if status is not None else gateway.status()
        unread = self.state.unread_ids()
        messages = gateway.list_messages(limit=50)
        modem = status.get("modem") or {}
        return {
            "type": "snapshot", "online": True,
            "ready": bool(status.get("ready")), "connected": bool(status.get("connected")),
            "sim_ready": bool(modem.get("sim_ready")),
            "registered": bool(modem.get("registered")),
            **link_summary(status),
            "unread": len(unread),
            "uncertain_sends": self.state.db.execute(
                "SELECT COUNT(*) FROM submissions WHERE state = 'unknown'"
            ).fetchone()[0],
            "messages": [{
                "id": m["id"], "peer": mask_peer(m.get("peer", "")),
                "time": m.get("received_at") or m.get("created_at") or "",
                "encoding": m.get("encoding", ""), "complete": bool(m.get("complete")),
                "direction": m.get("direction", ""), "unread": m["id"] in unread,
            } for m in messages],
        }

    def command(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "refresh":
            return self.poll()
        if action == "mark_all_read":
            self.state.mark_read()
            return self.snapshot()
        if action == "show":
            identifier = request.get("id")
            if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
                raise DesktopError("无效的短信编号")
            message = self.gateway.get_message(identifier)
            self.state.mark_read(identifier)
            return {"type": "detail", "message": {
                "id": identifier, "peer": message.get("peer", ""),
                "body": message.get("body", ""), "complete": bool(message.get("complete")),
                "time": message.get("received_at") or message.get("created_at") or "",
            }, "unread": len(self.state.unread_ids())}
        if action == "cancel":
            self.draft = None
            return {"type": "cancelled"}
        if action == "delete":
            # Local only: a copy on the module is left for cleanup to free.
            if request.get("confirmed") is not True:
                raise DesktopError("必须先确认删除")
            identifier = request.get("id")
            if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
                raise DesktopError("无效的短信编号")
            if not self.gateway.delete_message(identifier):
                raise DesktopError("短信不存在")
            self.state.mark_read(identifier)
            return {**self.snapshot(), "deleted": identifier}
        if action == "cleanup":
            # Frees ME/SM slots; the messages stay in the local store.
            if request.get("confirmed") is not True:
                raise DesktopError("必须先确认清理")
            if not self.gateway.status().get("ready"):
                raise DesktopError("模块未就绪，暂时无法清理")
            try:
                result = self.gateway.cleanup()
            except Exception as error:
                raise DesktopError("清理中断，模块里的短信可能只删除了一部分；本机记录不受影响") from error
            return {**self.snapshot(), "cleaned": {
                "deleted": int(result.get("deleted", 0)), "kept": int(result.get("kept", 0)),
            }}
        if action == "acknowledge":
            # The user checked the outcome themselves; this only stops the
            # warning and can never make a consumed key usable again.
            with self.state.db:
                self.state.db.execute("UPDATE submissions SET state = 'acknowledged' WHERE state = 'unknown'")
            return self.snapshot()
        if action == "preview":
            self.draft = None
            recipient, body = request.get("recipient"), request.get("body")
            preview = self.gateway.preview_send(recipient, body)
            # Recipient/body are held only in memory, never in argv or state DB.
            self.draft = {"key": str(uuid.uuid4()), "recipient": recipient, "body": body,
                          "expires": self.clock() + PREVIEW_TTL}
            return {"type": "preview", "key": self.draft["key"], "preview": preview}
        if action == "send":
            if request.get("confirmed") is not True:
                raise DesktopError("必须先预览并明确确认发送")
            draft = self.draft
            if not draft or request.get("key") != draft["key"]:
                raise DesktopError("确认已失效，请重新预览；不要重复发送")
            if self.clock() >= draft["expires"]:
                self.draft = None
                raise DesktopError("预览已过期，请重新预览")
            # Consume before any modem operation: replay/double-click cannot send twice.
            self.draft = None
            with self.state.db:
                self.state.db.execute(
                    "INSERT INTO submissions VALUES (?, 'sending', ?)", (draft["key"], time.time())
                )
            try:
                result = self.gateway.send(
                    draft["recipient"], draft["body"], idempotency_key=draft["key"], confirmed=True,
                )
            except Exception:
                with self.state.db:
                    self.state.db.execute("UPDATE submissions SET state = 'unknown' WHERE key = ?", (draft["key"],))
                return {"type": "sent", "state": "unknown"}
            send_state = result.get("state", "unknown")
            if send_state not in {"accepted_by_modem", "failed", "unknown"}:
                send_state = "unknown"
            with self.state.db:
                self.state.db.execute("UPDATE submissions SET state = ? WHERE key = ?", (send_state, draft["key"]))
            return {"type": "sent", "state": send_state}
        raise DesktopError("不支持的操作")


def safe_error(error: Exception) -> str:
    if isinstance(error, DesktopError):
        return str(error)
    if isinstance(error, ValueError):
        return "号码或正文无效，或短信超过 10 段"
    if isinstance(error, (GatewayUnavailable, TimeoutError)):
        return "模块未就绪或正忙，请稍后重试"
    return "短信引擎内部错误"


def run(controller: DesktopController, source=sys.stdin, output=sys.stdout) -> None:
    commands: queue.Queue[str | None] = queue.Queue(maxsize=32)

    def read_commands() -> None:
        for line in source:
            commands.put(line)
        commands.put(None)

    threading.Thread(target=read_commands, daemon=True).start()
    last: dict[str, Any] | None = None
    next_poll = 0.0
    while True:
        now = time.monotonic()
        if now >= next_poll:
            try:
                snapshot = controller.poll()
            except Exception as error:
                snapshot = {"type": "snapshot", "online": False, "error": safe_error(error)}
            # Unsolicited snapshots are pushed only when something changed.
            if snapshot != last:
                last = snapshot
                write_line(output, snapshot)
            next_poll = time.monotonic() + POLL_SECONDS
        try:
            line = commands.get(timeout=max(0, next_poll - time.monotonic()))
        except queue.Empty:
            continue
        if line is None:
            return
        action = None
        try:
            if len(line.encode("utf-8")) > MAX_INPUT_BYTES:
                raise DesktopError("输入内容过长")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise DesktopError("无效的请求")
            action = request.get("action")
            response = controller.command(request)
            if response.get("type") == "snapshot":
                last = dict(response)
            response["response"] = True
            response["action"] = action
            write_line(output, response)
        except Exception as error:
            write_line(output, {"type": "error", "response": True, "action": action, "error": safe_error(error)})


def start_gateway(config: GatewayConfig) -> SmsGateway:
    gateway = SmsGateway(
        GatewayStore(config.data_dir), SmsModem.open,
        poll_interval=config.poll_interval, status_interval=config.status_interval,
        reconnect_initial=config.reconnect_initial, reconnect_max=config.reconnect_max,
        storages=config.storages,
    )
    try:
        gateway.start()
    except BaseException:
        gateway.store.close()
        raise
    return gateway


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--emit-incoming", action="store_true",
        help="write new inbound SMS, including sender and text, to stdout for a UI "
             "that posts its own notifications, instead of calling notify-send",
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    state = gateway = None
    try:
        try:
            state = DesktopState(default_data_dir() / "desktop")
            gateway = start_gateway(GatewayConfig())
        except (AtBusyError, GatewayUnavailable):
            write_line(sys.stdout, {"type": "snapshot", "online": False,
                                    "error": "另一个短信进程正在使用短信数据，请先退出它"})
            return ENGINE_BUSY_EXIT
        if args.emit_incoming:
            def notify(messages: list[dict[str, Any]]) -> None:
                write_line(sys.stdout, incoming_event(messages))
        else:
            notify = notify_new_messages
        run(DesktopController(state, EmbeddedGateway(gateway), notify))
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    except Exception:
        # No traceback: exception strings could contain message text.
        print("DJI desktop helper could not start", file=sys.stderr)
        return 1
    finally:
        if gateway is not None:
            # Releases the modem and the data-directory lock before exit.
            gateway.stop()
            if not gateway.worker_alive():
                gateway.store.close()
        if state is not None:
            state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
