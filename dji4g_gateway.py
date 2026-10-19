#!/usr/bin/env python3
"""In-process SMS engine for the DJI Baiwang modem: a private store plus one
serialized modem worker, embedded by the desktop helper (no network API).

The module intentionally does not import or open the USB transport.  A modem
factory is injected by the caller and is created and used only by the single
modem worker thread.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import fcntl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Iterable, Mapping, Protocol

from dji4g_paths import default_data_dir
from dji4g_sms import build_submit_pdus, validate_phone_number


LOGGER = logging.getLogger(__name__)
MAX_MESSAGE_LIMIT = 500
MAX_OUTBOUND_SEGMENTS = 10
SEND_REQUEST_TIMEOUT = MAX_OUTBOUND_SEGMENTS * 60.0 + 5.0
CLEANUP_REQUEST_TIMEOUT = 300.0
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DEVICE_EVENT_TYPES = {
    "device.connection",
    "sim.readiness",
    "radio.registration",
    "sms.storage",
}
_DEVICE_PAYLOAD_FIELDS = {
    "storage",
    "used",
    "total",
    "free",
    "utilization_percent",
    "previous_state",
    "reason",
}


class GatewayError(RuntimeError):
    pass


class GatewayUnavailable(GatewayError):
    pass


class IdempotencyConflict(GatewayError):
    pass


class ModemAdapter(Protocol):
    """Preferred modem adapter contract, consumed only by the modem worker."""

    def probe(self) -> Mapping[str, Any]: ...

    def list_messages(self, storage: str) -> Iterable[Any]: ...

    def delete_messages(self, storage: str, indexes: Iterable[int]) -> int: ...

    def send_message(self, recipient: str, body: str) -> Any: ...

    def configure_delivery_reports(self) -> None: ...

    def read_delivery_reports(self) -> Iterable[Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class GatewayConfig:
    data_dir: Path = field(default_factory=default_data_dir)
    poll_interval: float = 8.0
    status_interval: float = 15.0
    reconnect_initial: float = 1.0
    reconnect_max: float = 60.0
    storages: tuple[str, ...] = ("ME", "SM")

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser())
        for value in (
            self.poll_interval,
            self.status_interval,
            self.reconnect_initial,
            self.reconnect_max,
        ):
            if value <= 0:
                raise ValueError("gateway intervals must be positive")
        if not self.storages or any(storage not in {"ME", "SM"} for storage in self.storages):
            raise ValueError("storages must contain only ME and/or SM")


@dataclass(frozen=True)
class InboundMessage:
    raw_pdu: str
    peer: str
    body: str
    encoding: str | None = None
    received_at: str | None = None
    storage: str | None = None
    storage_index: int | None = None
    modem_status: str | None = None
    concat_reference: str | None = None
    concat_total: int | None = None
    concat_sequence: int | None = None
    concat_reference_bits: int | None = None


@dataclass(frozen=True)
class IngestResult:
    message_id: int | None
    duplicate: bool
    complete: bool
    group_id: int | None = None


@dataclass(frozen=True)
class DeliveryReport:
    raw_pdu: str
    message_reference: int
    recipient: str
    status: int
    outcome: str
    service_center_time: str | None
    discharge_time: str | None


@dataclass
class _ModemJob:
    operation: str
    args: tuple[Any, ...] = ()
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _object_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _first_field(value: Any, names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        candidate = _object_field(value, name, None)
        if candidate is not None:
            return candidate
    return default


def _timestamp_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _timestamp_epoch(value: str | None, fallback: float) -> float:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def normalize_inbound_message(value: Any, storage: str | None = None) -> InboundMessage:
    """Normalize a DecodedSMS-like object without retaining its Python type."""

    wrapper = value
    nested = _object_field(wrapper, "message", None)
    if nested is not None:
        value = nested

    raw_pdu = _first_field(value, ("raw_pdu", "pdu"))
    if raw_pdu is None:
        raw_pdu = _first_field(wrapper, ("raw_pdu", "pdu"))
    if isinstance(raw_pdu, (bytes, bytearray)):
        raw_pdu = bytes(raw_pdu).hex().upper()
    if not isinstance(raw_pdu, str) or not raw_pdu:
        raise ValueError("incoming message is missing raw_pdu")

    peer = _first_field(value, ("sender", "peer", "originator"), "")
    body = _first_field(value, ("text", "body"), "")
    if peer is None:
        peer = ""
    if body is None:
        body = ""
    if not isinstance(peer, str) or not isinstance(body, str):
        raise ValueError("incoming peer and text must be strings")

    concat = _object_field(value, "concat", None)
    reference = total = sequence = reference_bits = None
    if concat is not None:
        reference = _first_field(concat, ("reference", "ref"))
        total = _first_field(concat, ("total_parts", "total"))
        sequence = _first_field(concat, ("part_number", "sequence", "seq"))
        reference_bits = _first_field(concat, ("reference_bits", "ref_bits"), 8)
    else:
        reference = _first_field(value, ("concat_reference", "concat_ref"))
        total = _first_field(value, ("concat_total", "total_parts"))
        sequence = _first_field(value, ("concat_sequence", "part_number"))
        reference_bits = _first_field(value, ("concat_reference_bits", "reference_bits"))

    if any(part is not None for part in (reference, total, sequence)):
        if reference is None or total is None or sequence is None:
            raise ValueError("concat metadata must contain reference, total, and sequence")
        try:
            total = int(total)
            sequence = int(sequence)
            reference_bits = int(reference_bits or 8)
        except (TypeError, ValueError) as error:
            raise ValueError("concat metadata must be numeric") from error
        if not 1 <= total <= 255 or not 1 <= sequence <= total:
            raise ValueError("concat metadata is outside the valid range")
        if reference_bits not in (8, 16):
            raise ValueError("concat reference width must be 8 or 16 bits")
        if total == 1:
            reference = total = sequence = reference_bits = None
        else:
            reference = str(reference)
    else:
        reference = total = sequence = reference_bits = None

    resolved_storage = storage or _first_field(value, ("storage",), None)
    if resolved_storage is None:
        resolved_storage = _first_field(wrapper, ("storage",), None)
    index = _first_field(value, ("index", "storage_index"), None)
    if index is None:
        index = _first_field(wrapper, ("index", "storage_index"), None)
    if index is not None:
        try:
            index = int(index)
        except (TypeError, ValueError) as error:
            raise ValueError("storage index must be an integer") from error

    return InboundMessage(
        raw_pdu=raw_pdu,
        peer=peer,
        body=body,
        encoding=_first_field(value, ("encoding",), None),
        received_at=_timestamp_text(_first_field(value, ("timestamp", "received_at"), None)),
        storage=resolved_storage,
        storage_index=index,
        modem_status=_first_field(value, ("status",), _first_field(wrapper, ("status",), None)),
        concat_reference=reference,
        concat_total=total,
        concat_sequence=sequence,
        concat_reference_bits=reference_bits,
    )


def raw_pdu_fingerprint(raw_pdu: str) -> str:
    return hashlib.sha256(raw_pdu.encode("utf-8")).hexdigest()


def _recipient_key(recipient: str) -> str:
    digits = recipient[1:] if recipient.startswith("+") else recipient
    return hashlib.sha256(digits.encode("ascii")).hexdigest()


def normalize_delivery_report(value: Any) -> DeliveryReport:
    raw_pdu = _first_field(value, ("raw_pdu", "pdu"))
    if not isinstance(raw_pdu, str):
        raise ValueError("delivery report is missing raw_pdu")
    raw_pdu = "".join(raw_pdu.split()).upper()
    if not raw_pdu or len(raw_pdu) % 2 or re.fullmatch(r"[0-9A-F]+", raw_pdu) is None:
        raise ValueError("delivery report raw_pdu is invalid")
    reference = _first_field(value, ("message_reference", "reference", "message_ref"))
    if isinstance(reference, bool):
        raise ValueError("delivery report reference is invalid")
    try:
        reference = int(reference)
    except (TypeError, ValueError) as error:
        raise ValueError("delivery report reference is invalid") from error
    if not 0 <= reference <= 255:
        raise ValueError("delivery report reference is invalid")
    recipient = validate_recipient(_first_field(value, ("recipient", "destination")))
    status = _first_field(value, ("status", "tp_status"))
    if isinstance(status, bool):
        raise ValueError("delivery report status is invalid")
    try:
        status = int(status)
    except (TypeError, ValueError) as error:
        raise ValueError("delivery report status is invalid") from error
    if not 0 <= status <= 255:
        raise ValueError("delivery report status is invalid")
    outcome = _first_field(value, ("outcome", "delivery_status"))
    if outcome not in {"pending", "delivered", "failed", "unconfirmed"}:
        raise ValueError("delivery report outcome is invalid")
    return DeliveryReport(
        raw_pdu=raw_pdu,
        message_reference=reference,
        recipient=recipient,
        status=status,
        outcome=outcome,
        service_center_time=_timestamp_text(
            _first_field(value, ("service_center_time", "scts"))
        ),
        discharge_time=_timestamp_text(
            _first_field(value, ("discharge_time", "dt"))
        ),
    )


def mask_peer(peer: str) -> str:
    prefix = "+" if peer.startswith("+") else ""
    digits = peer[1:] if prefix else peer
    if not digits:
        return ""
    if len(digits) <= 4:
        return prefix + "*" * len(digits)
    return prefix + digits[:2] + "*" * (len(digits) - 4) + digits[-2:]


def validate_recipient(recipient: Any) -> str:
    return validate_phone_number(recipient)


def validate_body(body: Any) -> str:
    if not isinstance(body, str) or not body:
        raise ValueError("body must be a non-empty string")
    if len(body) > 4096:
        raise ValueError("body exceeds 4096 characters")
    return body


def validate_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_RE.fullmatch(value):
        raise ValueError("Idempotency-Key must contain 1-128 safe ASCII characters")
    return value


def validate_message_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    if not 1 <= value <= MAX_MESSAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_MESSAGE_LIMIT}")
    return value


def validate_storage(value: Any) -> str:
    if not isinstance(value, str) or value.upper() not in {"ME", "SM"}:
        raise ValueError("storage must be ME or SM")
    return value.upper()


def build_send_preview(recipient: Any, body: Any) -> dict[str, Any]:
    recipient = validate_recipient(recipient)
    body = validate_body(body)
    parts = build_submit_pdus(recipient, body)
    if len(parts) > MAX_OUTBOUND_SEGMENTS:
        raise ValueError(
            f"SMS would use {len(parts)} segments; maximum is {MAX_OUTBOUND_SEGMENTS}"
        )
    return {
        "recipient": mask_peer(recipient),
        "encoding": parts[0].encoding,
        "characters": len(body),
        "segments": len(parts),
    }


class GatewayStore:
    """Private SQLite persistence for messages, segments, sends, and outbox."""

    def __init__(self, data_dir: str | os.PathLike[str] | Path | None = None):
        self.data_dir = Path(data_dir or default_data_dir()).expanduser()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self.database_path = self.data_dir / "gateway.sqlite3"
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=10,
            check_same_thread=False,
        )
        os.chmod(self.database_path, 0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 10000")
        self._create_schema()

    def __enter__(self) -> "GatewayStore":
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
            fingerprint TEXT NOT NULL UNIQUE,
            peer TEXT NOT NULL,
            body TEXT NOT NULL,
            raw_pdu TEXT,
            storage TEXT,
            storage_index INTEGER,
            modem_status TEXT,
            encoding TEXT,
            received_at TEXT,
            complete INTEGER NOT NULL DEFAULT 1,
            concat_reference TEXT,
            concat_total INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS concat_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            peer_fingerprint TEXT NOT NULL,
            concat_reference TEXT NOT NULL,
            reference_bits INTEGER NOT NULL,
            total_parts INTEGER NOT NULL,
            encoding TEXT,
            anchor_at REAL NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER REFERENCES messages(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS concat_groups_open_idx
            ON concat_groups(peer_fingerprint, concat_reference, reference_bits, total_parts, complete);
        CREATE TABLE IF NOT EXISTS concat_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            group_id INTEGER NOT NULL REFERENCES concat_groups(id),
            sequence_number INTEGER NOT NULL,
            raw_pdu TEXT NOT NULL,
            peer TEXT NOT NULL,
            body TEXT NOT NULL,
            storage TEXT,
            storage_index INTEGER,
            modem_status TEXT,
            encoding TEXT,
            received_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(group_id, sequence_number)
        );
        CREATE TABLE IF NOT EXISTS outbound (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            recipient TEXT NOT NULL,
            body TEXT NOT NULL,
            preview_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('queued', 'sending', 'accepted_by_modem', 'failed', 'unknown')),
            modem_reference TEXT,
            error_code TEXT,
            delivery_requested INTEGER NOT NULL DEFAULT 0,
            delivery_status TEXT NOT NULL DEFAULT 'not_requested'
                CHECK(delivery_status IN ('not_requested', 'pending', 'delivered', 'failed', 'unconfirmed')),
            delivery_updated_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbound_sms_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outbound_id INTEGER NOT NULL REFERENCES outbound(id),
            segment_index INTEGER NOT NULL,
            segment_count INTEGER NOT NULL,
            message_reference INTEGER,
            recipient_key TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            delivery_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(delivery_status IN ('pending', 'delivered', 'failed', 'unconfirmed')),
            tp_status INTEGER,
            service_center_time TEXT,
            discharge_time TEXT,
            report_fingerprint TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(outbound_id, segment_index)
        );
        CREATE INDEX IF NOT EXISTS outbound_segments_match_idx
            ON outbound_sms_segments(message_reference, recipient_key, delivery_status);
        CREATE TABLE IF NOT EXISTS sms_delivery_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            raw_pdu TEXT NOT NULL,
            message_reference INTEGER NOT NULL,
            recipient_key TEXT NOT NULL,
            service_center_time TEXT,
            discharge_time TEXT,
            tp_status INTEGER NOT NULL,
            outcome TEXT NOT NULL
                CHECK(outcome IN ('pending', 'delivered', 'failed', 'unconfirmed')),
            received_at TEXT NOT NULL,
            segment_id INTEGER REFERENCES outbound_sms_segments(id),
            match_status TEXT NOT NULL
                CHECK(match_status IN ('matched', 'unmatched', 'ambiguous'))
        );
        CREATE TABLE IF NOT EXISTS delivery_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outbound_id INTEGER NOT NULL REFERENCES outbound(id),
            status TEXT NOT NULL
                CHECK(status IN ('delivered', 'failed', 'unconfirmed')),
            created_at TEXT NOT NULL,
            UNIQUE(outbound_id, status)
        );
        CREATE TABLE IF NOT EXISTS device_state (
            key TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS device_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL
                CHECK(event_type IN ('device.connection', 'sim.readiness',
                    'radio.registration', 'sms.storage')),
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
        with self._lock, self._connection:
            self._connection.executescript(schema)
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(concat_groups)")
            }
            if "encoding" not in columns:
                self._connection.execute("ALTER TABLE concat_groups ADD COLUMN encoding TEXT")
            if "anchor_at" not in columns:
                self._connection.execute("ALTER TABLE concat_groups ADD COLUMN anchor_at REAL")
                self._connection.execute(
                    "UPDATE concat_groups SET anchor_at = ? WHERE anchor_at IS NULL",
                    (time.time(),),
                )
            outbound_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(outbound)")
            }
            if "delivery_requested" not in outbound_columns:
                self._connection.execute(
                    "ALTER TABLE outbound ADD COLUMN delivery_requested "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "delivery_status" not in outbound_columns:
                self._connection.execute(
                    "ALTER TABLE outbound ADD COLUMN delivery_status "
                    "TEXT NOT NULL DEFAULT 'not_requested'"
                )
            if "delivery_updated_at" not in outbound_columns:
                self._connection.execute(
                    "ALTER TABLE outbound ADD COLUMN delivery_updated_at TEXT"
                )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def ping(self) -> bool:
        try:
            with self._lock:
                self._connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def _existing_fingerprint(self, fingerprint: str) -> IngestResult | None:
        row = self._connection.execute(
            "SELECT id FROM messages WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is not None:
            return IngestResult(int(row["id"]), True, True, None)
        row = self._connection.execute(
            """
            SELECT s.group_id, g.complete, g.message_id
            FROM concat_segments AS s
            JOIN concat_groups AS g ON g.id = s.group_id
            WHERE s.fingerprint = ?
            """,
            (fingerprint,),
        ).fetchone()
        if row is None:
            return None
        return IngestResult(
            int(row["message_id"]) if row["message_id"] is not None else None,
            True,
            bool(row["complete"]),
            int(row["group_id"]),
        )

    def ingest(
        self,
        value: Any,
        *,
        storage: str | None = None,
    ) -> IngestResult:
        message = normalize_inbound_message(value, storage=storage)
        fingerprint = raw_pdu_fingerprint(message.raw_pdu)
        now = _utc_now()
        anchor_at = _timestamp_epoch(message.received_at, time.time())

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._existing_fingerprint(fingerprint)
                if existing is not None:
                    self._connection.commit()
                    return existing

                if message.concat_reference is None:
                    cursor = self._connection.execute(
                        """
                        INSERT INTO messages (
                            direction, fingerprint, peer, body, raw_pdu, storage,
                            storage_index, modem_status, encoding, received_at,
                            complete, created_at
                        ) VALUES ('inbound', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            fingerprint,
                            message.peer,
                            message.body,
                            message.raw_pdu,
                            message.storage,
                            message.storage_index,
                            message.modem_status,
                            message.encoding,
                            message.received_at,
                            now,
                        ),
                    )
                    message_id = int(cursor.lastrowid)
                    self._connection.commit()
                    return IngestResult(message_id, False, True, None)

                peer_fingerprint = hashlib.sha256(message.peer.encode("utf-8")).hexdigest()
                row = self._connection.execute(
                    """
                    SELECT g.id
                    FROM concat_groups AS g
                    WHERE g.peer_fingerprint = ?
                      AND g.concat_reference = ?
                      AND g.reference_bits = ?
                      AND g.total_parts = ?
                      AND COALESCE(g.encoding, '') = ?
                      AND ABS(g.anchor_at - ?) <= 900
                      AND g.complete = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM concat_segments AS s
                          WHERE s.group_id = g.id AND s.sequence_number = ?
                      )
                    ORDER BY ABS(g.anchor_at - ?) ASC, g.updated_at DESC, g.id DESC
                    LIMIT 1
                    """,
                    (
                        peer_fingerprint,
                        message.concat_reference,
                        message.concat_reference_bits,
                        message.concat_total,
                        message.encoding or "",
                        anchor_at,
                        message.concat_sequence,
                        anchor_at,
                    ),
                ).fetchone()
                if row is None:
                    cursor = self._connection.execute(
                        """
                        INSERT INTO concat_groups (
                            peer_fingerprint, concat_reference, reference_bits,
                            total_parts, encoding, anchor_at, complete, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            peer_fingerprint,
                            message.concat_reference,
                            message.concat_reference_bits,
                            message.concat_total,
                            message.encoding,
                            anchor_at,
                            now,
                            now,
                        ),
                    )
                    group_id = int(cursor.lastrowid)
                else:
                    group_id = int(row["id"])

                self._connection.execute(
                    """
                    INSERT INTO concat_segments (
                        fingerprint, group_id, sequence_number, raw_pdu, peer,
                        body, storage, storage_index, modem_status, encoding,
                        received_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        group_id,
                        message.concat_sequence,
                        message.raw_pdu,
                        message.peer,
                        message.body,
                        message.storage,
                        message.storage_index,
                        message.modem_status,
                        message.encoding,
                        message.received_at,
                        now,
                    ),
                )
                self._connection.execute(
                    "UPDATE concat_groups SET updated_at = ? WHERE id = ?", (now, group_id)
                )
                segments = self._connection.execute(
                    """
                    SELECT * FROM concat_segments
                    WHERE group_id = ? ORDER BY sequence_number
                    """,
                    (group_id,),
                ).fetchall()
                expected = list(range(1, int(message.concat_total) + 1))
                actual = [int(segment["sequence_number"]) for segment in segments]
                if actual != expected:
                    self._connection.commit()
                    return IngestResult(None, False, False, group_id)

                combined_fingerprints = "|".join(segment["fingerprint"] for segment in segments)
                combined_fingerprint = "concat:" + hashlib.sha256(
                    combined_fingerprints.encode("ascii")
                ).hexdigest()
                first = segments[0]
                combined_body = "".join(segment["body"] for segment in segments)
                received_values = [segment["received_at"] for segment in segments if segment["received_at"]]
                received_at = min(received_values) if received_values else None
                cursor = self._connection.execute(
                    """
                    INSERT INTO messages (
                        direction, fingerprint, peer, body, storage,
                        storage_index, modem_status, encoding, received_at,
                        complete, concat_reference, concat_total, created_at
                    ) VALUES ('inbound', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        combined_fingerprint,
                        first["peer"],
                        combined_body,
                        first["storage"],
                        first["storage_index"],
                        first["modem_status"],
                        first["encoding"],
                        received_at,
                        message.concat_reference,
                        message.concat_total,
                        now,
                    ),
                )
                message_id = int(cursor.lastrowid)
                self._connection.execute(
                    """
                    UPDATE concat_groups
                    SET complete = 1, message_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (message_id, now, group_id),
                )
                self._connection.commit()
                return IngestResult(message_id, False, True, group_id)
            except BaseException:
                self._connection.rollback()
                raise

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "direction": row["direction"],
            "peer": row["peer"],
            "body": row["body"],
            "storage": row["storage"],
            "storage_index": row["storage_index"],
            "status": row["modem_status"],
            "encoding": row["encoding"],
            "received_at": row["received_at"],
            "complete": bool(row["complete"]),
            "concat_reference": row["concat_reference"],
            "concat_total": row["concat_total"],
            "created_at": row["created_at"],
        }

    def list_messages(
        self,
        limit: int = 50,
        after_id: int | None = None,
        storage: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = validate_message_limit(limit)
        if after_id is not None and (
            isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0
        ):
            raise ValueError("after_id must be a non-negative integer")
        if storage is not None:
            storage = validate_storage(storage)
        with self._lock:
            if after_id is None and storage is None:
                rows = self._connection.execute(
                    "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            elif after_id is None:
                rows = self._connection.execute(
                    "SELECT * FROM messages WHERE storage = ? ORDER BY id DESC LIMIT ?",
                    (storage, limit),
                ).fetchall()
            elif storage is None:
                rows = self._connection.execute(
                    "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (after_id, limit),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE id > ? AND storage = ? ORDER BY id ASC LIMIT ?
                    """,
                    (after_id, storage, limit),
                ).fetchall()
        return [self._message_dict(row) for row in rows]

    def get_message(self, message_id: int | None) -> dict[str, Any] | None:
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return self._message_dict(row) if row is not None else None

    def count_concat_groups(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS count FROM concat_groups").fetchone()
        return int(row["count"])

    def create_outbound(
        self, idempotency_key: str, recipient: str, body: str, preview: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        idempotency_key = validate_idempotency_key(idempotency_key)
        recipient = validate_recipient(recipient)
        body = validate_body(body)
        now = _utc_now()
        encoded_preview = json.dumps(dict(preview), separators=(",", ":"))
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM outbound WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row is not None:
                    if row["recipient"] != recipient or row["body"] != body:
                        raise IdempotencyConflict(
                            "idempotency key was already used for another payload"
                        )
                    self._connection.commit()
                    return self._outbound_dict(row), False
                cursor = self._connection.execute(
                    """
                    INSERT INTO outbound (
                        idempotency_key, recipient, body, preview_json, state,
                        delivery_requested, delivery_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', 1, 'not_requested', ?, ?)
                    """,
                    (idempotency_key, recipient, body, encoded_preview, now, now),
                )
                row = self._connection.execute(
                    "SELECT * FROM outbound WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                self._connection.commit()
                return self._outbound_dict(row), True
            except BaseException:
                self._connection.rollback()
                raise

    @staticmethod
    def _outbound_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "idempotency_key": row["idempotency_key"],
            "recipient": row["recipient"],
            "body": row["body"],
            "preview": json.loads(row["preview_json"]),
            "state": row["state"],
            "modem_reference": row["modem_reference"],
            "error_code": row["error_code"],
            "delivery_requested": bool(row["delivery_requested"]),
            "delivery_status": row["delivery_status"],
            "delivery_updated_at": row["delivery_updated_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_outbound(
        self,
        outbound_id: int,
        state: str,
        *,
        modem_reference: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"queued", "sending", "accepted_by_modem", "failed", "unknown"}:
            raise ValueError("invalid outbound state")
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE outbound
                SET state = ?, modem_reference = ?, error_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (state, modem_reference, error_code, _utc_now(), outbound_id),
            )
            row = self._connection.execute(
                "SELECT * FROM outbound WHERE id = ?", (outbound_id,)
            ).fetchone()
        if row is None:
            raise KeyError(outbound_id)
        return self._outbound_dict(row)

    def record_outbound_submission(
        self, outbound_id: int, message_references: Iterable[int | None]
    ) -> dict[str, Any]:
        if isinstance(outbound_id, bool) or not isinstance(outbound_id, int) or outbound_id <= 0:
            raise ValueError("outbound_id must be a positive integer")
        references: list[int | None] = []
        for value in message_references:
            if value is None:
                references.append(None)
                continue
            if isinstance(value, bool):
                raise ValueError("message reference is invalid")
            try:
                reference = int(value)
            except (TypeError, ValueError) as error:
                raise ValueError("message reference is invalid") from error
            if not 0 <= reference <= 255:
                raise ValueError("message reference is invalid")
            references.append(reference)
        now = _utc_now()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT * FROM outbound WHERE id = ?", (outbound_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(outbound_id)
                preview = json.loads(row["preview_json"])
                segment_count = int(preview["segments"])
                if len(references) > segment_count:
                    raise ValueError("message reference count exceeds SMS segments")
                references.extend([None] * (segment_count - len(references)))
                existing = self._connection.execute(
                    "SELECT COUNT(*) AS count FROM outbound_sms_segments WHERE outbound_id = ?",
                    (outbound_id,),
                ).fetchone()
                if int(existing["count"]):
                    raise ValueError("outbound submission was already recorded")
                recipient_key = _recipient_key(row["recipient"])
                for segment_index, reference in enumerate(references, start=1):
                    self._connection.execute(
                        """
                        INSERT INTO outbound_sms_segments (
                            outbound_id, segment_index, segment_count, message_reference,
                            recipient_key, submitted_at, delivery_status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            outbound_id,
                            segment_index,
                            segment_count,
                            reference,
                            recipient_key,
                            now,
                            now,
                        ),
                    )
                modem_reference = None
                if references and all(reference is not None for reference in references):
                    modem_reference = ":".join(str(reference) for reference in references)
                self._connection.execute(
                    """
                    UPDATE outbound
                    SET state = 'accepted_by_modem', modem_reference = ?, error_code = NULL,
                        delivery_status = 'pending', delivery_updated_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (modem_reference, now, now, outbound_id),
                )
                row = self._connection.execute(
                    "SELECT * FROM outbound WHERE id = ?", (outbound_id,)
                ).fetchone()
                self._connection.commit()
                return self._outbound_dict(row)
            except BaseException:
                self._connection.rollback()
                raise

    def ingest_delivery_report(self, value: Any) -> dict[str, Any]:
        report = normalize_delivery_report(value)
        fingerprint = raw_pdu_fingerprint(report.raw_pdu)
        now = _utc_now()
        recipient_key = _recipient_key(report.recipient)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT * FROM sms_delivery_reports WHERE fingerprint = ?",
                    (fingerprint,),
                ).fetchone()
                if existing is not None:
                    delivery_status = None
                    if existing["segment_id"] is not None:
                        state = self._connection.execute(
                            """
                            SELECT o.delivery_status
                            FROM outbound_sms_segments AS s
                            JOIN outbound AS o ON o.id = s.outbound_id
                            WHERE s.id = ?
                            """,
                            (existing["segment_id"],),
                        ).fetchone()
                        delivery_status = state["delivery_status"] if state else None
                    self._connection.commit()
                    return {
                        "duplicate": True,
                        "matched": existing["match_status"] == "matched",
                        "match_status": existing["match_status"],
                        "delivery_status": delivery_status,
                    }

                candidates = self._connection.execute(
                    """
                    SELECT s.id, s.outbound_id, s.submitted_at
                    FROM outbound_sms_segments AS s
                    JOIN outbound AS o ON o.id = s.outbound_id
                    WHERE s.message_reference = ? AND s.recipient_key = ?
                      AND s.delivery_status = 'pending'
                      AND o.state = 'accepted_by_modem'
                    ORDER BY s.submitted_at DESC, s.id DESC
                    """,
                    (report.message_reference, recipient_key),
                ).fetchall()
                candidate = candidates[0] if len(candidates) == 1 else None
                if len(candidates) > 1 and report.service_center_time:
                    report_time = _timestamp_epoch(report.service_center_time, float("nan"))
                    if report_time == report_time:
                        nearby = [
                            row
                            for row in candidates
                            if abs(
                                _timestamp_epoch(row["submitted_at"], report_time)
                                - report_time
                            )
                            <= 24 * 60 * 60
                        ]
                        if len(nearby) == 1:
                            candidate = nearby[0]
                if candidate is not None:
                    match_status = "matched"
                elif candidates:
                    match_status = "ambiguous"
                else:
                    match_status = "unmatched"

                cursor = self._connection.execute(
                    """
                    INSERT INTO sms_delivery_reports (
                        fingerprint, raw_pdu, message_reference, recipient_key,
                        service_center_time, discharge_time, tp_status, outcome,
                        received_at, segment_id, match_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        report.raw_pdu,
                        report.message_reference,
                        recipient_key,
                        report.service_center_time,
                        report.discharge_time,
                        report.status,
                        report.outcome,
                        now,
                        candidate["id"] if candidate is not None else None,
                        match_status,
                    ),
                )
                delivery_status = None
                outbound_id = None
                if candidate is not None:
                    outbound_id = int(candidate["outbound_id"])
                    self._connection.execute(
                        """
                        UPDATE outbound_sms_segments
                        SET delivery_status = ?, tp_status = ?, service_center_time = ?,
                            discharge_time = ?, report_fingerprint = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            report.outcome,
                            report.status,
                            report.service_center_time,
                            report.discharge_time,
                            fingerprint,
                            now,
                            candidate["id"],
                        ),
                    )
                    statuses = [
                        row["delivery_status"]
                        for row in self._connection.execute(
                            """
                            SELECT delivery_status FROM outbound_sms_segments
                            WHERE outbound_id = ? ORDER BY segment_index
                            """,
                            (outbound_id,),
                        ).fetchall()
                    ]
                    if "failed" in statuses:
                        delivery_status = "failed"
                    elif statuses and all(status == "delivered" for status in statuses):
                        delivery_status = "delivered"
                    elif statuses and "pending" not in statuses:
                        delivery_status = "unconfirmed"
                    else:
                        delivery_status = "pending"
                    previous = self._connection.execute(
                        "SELECT delivery_status FROM outbound WHERE id = ?",
                        (outbound_id,),
                    ).fetchone()["delivery_status"]
                    if delivery_status != previous:
                        self._connection.execute(
                            """
                            UPDATE outbound
                            SET delivery_status = ?, delivery_updated_at = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (delivery_status, now, now, outbound_id),
                        )
                        if delivery_status in {"delivered", "failed", "unconfirmed"}:
                            self._connection.execute(
                                """
                                INSERT OR IGNORE INTO delivery_events (
                                    outbound_id, status, created_at
                                ) VALUES (?, ?, ?)
                                """,
                                (outbound_id, delivery_status, now),
                            )
                self._connection.commit()
                return {
                    "id": int(cursor.lastrowid),
                    "duplicate": False,
                    "matched": candidate is not None,
                    "match_status": match_status,
                    "outbound_id": outbound_id,
                    "delivery_status": delivery_status,
                }
            except BaseException:
                self._connection.rollback()
                raise

    def list_delivery_events(
        self, limit: int = 50, *, after_id: int | None = None
    ) -> list[dict[str, Any]]:
        limit = validate_message_limit(limit)
        if after_id is not None and (
            isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0
        ):
            raise ValueError("after_id must be a non-negative integer")
        with self._lock:
            if after_id is None:
                rows = self._connection.execute(
                    """
                    SELECT e.*, o.recipient
                    FROM delivery_events AS e
                    JOIN outbound AS o ON o.id = e.outbound_id
                    ORDER BY e.id DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT e.*, o.recipient
                    FROM delivery_events AS e
                    JOIN outbound AS o ON o.id = e.outbound_id
                    WHERE e.id > ? ORDER BY e.id ASC LIMIT ?
                    """,
                    (after_id, limit),
                ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "outbound_id": int(row["outbound_id"]),
                "status": row["status"],
                "recipient_masked": mask_peer(row["recipient"]),
                "updated_at": row["created_at"],
            }
            for row in rows
        ]

    def get_device_state(self, key: str) -> dict[str, Any] | None:
        if not isinstance(key, str) or not key:
            raise ValueError("device state key must not be empty")
        with self._lock:
            row = self._connection.execute(
                "SELECT state, payload_json, updated_at FROM device_state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "state": row["state"],
            "payload": _safe_device_event_payload(json.loads(row["payload_json"])),
            "updated_at": row["updated_at"],
        }

    def record_device_state(
        self,
        key: str,
        state: str,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        emit_initial: bool = False,
    ) -> dict[str, Any] | None:
        if not isinstance(key, str) or not key:
            raise ValueError("device state key must not be empty")
        if event_type not in _DEVICE_EVENT_TYPES:
            raise ValueError("unsupported device event type")
        if not isinstance(state, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", state) is None:
            raise ValueError("invalid device state")
        safe_payload = _safe_device_event_payload(payload)
        now = _utc_now()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                previous = self._connection.execute(
                    "SELECT state FROM device_state WHERE key = ?", (key,)
                ).fetchone()
                previous_state = previous["state"] if previous is not None else None
                if previous_state == state:
                    self._connection.commit()
                    return None
                self._connection.execute(
                    """
                    INSERT INTO device_state (key, state, payload_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        state = excluded.state,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        state,
                        json.dumps(safe_payload, separators=(",", ":")),
                        now,
                    ),
                )
                if previous_state is None and not emit_initial:
                    self._connection.commit()
                    return None
                event_payload = dict(safe_payload)
                if previous_state is not None:
                    event_payload["previous_state"] = previous_state
                cursor = self._connection.execute(
                    """
                    INSERT INTO device_events (event_type, state, payload_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        state,
                        json.dumps(event_payload, separators=(",", ":")),
                        now,
                    ),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return {
            "id": int(cursor.lastrowid),
            "event_type": event_type,
            "state": state,
            "payload": event_payload,
            "created_at": now,
        }

    def list_device_events(
        self, limit: int = 50, *, after_id: int | None = None
    ) -> list[dict[str, Any]]:
        limit = validate_message_limit(limit)
        if after_id is not None and (
            isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0
        ):
            raise ValueError("after_id must be a non-negative integer")
        with self._lock:
            if after_id is None:
                rows = self._connection.execute(
                    "SELECT * FROM device_events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM device_events
                    WHERE id > ? ORDER BY id ASC LIMIT ?
                    """,
                    (after_id, limit),
                ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "event_type": row["event_type"],
                "state": row["state"],
                "payload": _safe_device_event_payload(json.loads(row["payload_json"])),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def recover_interrupted_outbound(self) -> tuple[int, int]:
        """Resolve states left behind by a prior process without retrying a send."""

        now = _utc_now()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                sending = self._connection.execute(
                    """
                    UPDATE outbound
                    SET state = 'unknown', error_code = 'ProcessRestarted', updated_at = ?
                    WHERE state = 'sending'
                    """,
                    (now,),
                ).rowcount
                queued = self._connection.execute(
                    """
                    UPDATE outbound
                    SET state = 'failed', error_code = 'ProcessRestartedBeforeSubmission',
                        updated_at = ?
                    WHERE state = 'queued'
                    """,
                    (now,),
                ).rowcount
                self._connection.commit()
                return int(sending), int(queued)
            except BaseException:
                self._connection.rollback()
                raise

def _safe_modem_references(result: Any) -> tuple[int | None, ...]:
    values = _first_field(result, ("message_references",), None)
    if values is None:
        value = _first_field(result, ("message_ref", "reference", "id"), None)
        if value is None and isinstance(result, (int, str)) and not isinstance(result, bool):
            value = result
        values = () if value is None else (value,)
    elif isinstance(values, (str, bytes, bytearray)):
        values = (values,)
    try:
        candidates = tuple(values)
    except TypeError:
        candidates = (values,)
    references: list[int | None] = []
    for value in candidates:
        if value is None or isinstance(value, bool):
            references.append(None)
            continue
        try:
            reference = int(value)
        except (TypeError, ValueError):
            references.append(None)
            continue
        references.append(reference if 0 <= reference <= 255 else None)
    return tuple(references)


def _safe_device_event_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in _DEVICE_PAYLOAD_FIELDS:
        field_value = value.get(key)
        if key == "storage" and field_value in {"ME", "SM"}:
            result[key] = field_value
        elif key in {"used", "total", "free"}:
            if isinstance(field_value, int) and not isinstance(field_value, bool) and field_value >= 0:
                result[key] = field_value
        elif key == "utilization_percent":
            if (
                isinstance(field_value, (int, float))
                and not isinstance(field_value, bool)
                and 0 <= field_value <= 100
            ):
                result[key] = round(float(field_value), 1)
        elif key in {"previous_state", "reason"}:
            if (
                isinstance(field_value, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", field_value)
            ):
                result[key] = field_value
    return result


def _safe_probe_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in ("ready", "connected", "at_ready", "sim_ready", "registered"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    registration = value.get("registration")
    if registration in {
        "home",
        "roaming",
        "registered",
        "searching",
        "denied",
        "not_registered",
        "unknown",
    }:
        result["registration"] = registration
    signal = value.get("signal")
    if isinstance(signal, (int, float)) and not isinstance(signal, bool):
        result["signal"] = signal

    radio = value.get("radio")
    if isinstance(radio, Mapping):
        safe_radio: dict[str, Any] = {}
        string_fields = {
            "parse_status": {"ok", "no_service", "parse_error", "unavailable"},
            "service": {"registered", "no_service"},
            "state": {"idle", "connected", "searching", "limited_service", "unknown"},
            "rat": {
                "LTE",
                "WCDMA",
                "GSM",
                "GPRS",
                "EDGE",
                "HSDPA",
                "HSUPA",
                "HSPA+",
                "TDSCDMA",
                "CAT-M1",
                "CAT-NB1",
                "NR5G-SA",
                "NR5G-NSA",
            },
            "duplex": {"FDD", "TDD"},
        }
        number_fields = {
            "band",
            "channel",
            "rsrp_dbm",
            "rsrq_db",
            "rssi_dbm",
            "sinr_db",
        }
        for key, allowed_values in string_fields.items():
            field_value = radio.get(key)
            if isinstance(field_value, str) and field_value in allowed_values:
                safe_radio[key] = field_value
        for key in number_fields:
            field_value = radio.get(key)
            if isinstance(field_value, (int, float)) and not isinstance(field_value, bool):
                safe_radio[key] = field_value
        if safe_radio:
            result["radio"] = safe_radio

    storage = value.get("storage")
    if isinstance(storage, Mapping):
        safe_storage: dict[str, Any] = {}
        for name in ("ME", "SM"):
            item = storage.get(name)
            if not isinstance(item, Mapping):
                continue
            used = item.get("used")
            total = item.get("total")
            if (
                isinstance(used, int)
                and not isinstance(used, bool)
                and isinstance(total, int)
                and not isinstance(total, bool)
                and 0 <= used <= total
                and total > 0
            ):
                safe_storage[name] = {"used": used, "total": total}
        if safe_storage:
            result["storage"] = safe_storage
    return result


def _storage_state(used: int, total: int, previous_state: str | None) -> str:
    if used >= total:
        return "full"
    utilization = used / total
    if previous_state in {"near_full", "full"}:
        return "normal" if utilization <= 0.70 else "near_full"
    return "near_full" if utilization >= 0.80 else "normal"


def _method_accepts_storage(method: Callable[..., Any]) -> bool:
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters) or any(
        parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        for parameter in parameters
    )


class SmsGateway:
    """Coordinates persistence and a single serialized modem worker."""

    def __init__(
        self,
        store: GatewayStore,
        modem_factory: Callable[[], ModemAdapter],
        *,
        poll_interval: float = 8.0,
        status_interval: float = 15.0,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 60.0,
        storages: tuple[str, ...] = ("ME", "SM"),
    ):
        if poll_interval <= 0 or status_interval <= 0:
            raise ValueError("poll and status intervals must be positive")
        self.store = store
        self._modem_factory = modem_factory
        self.poll_interval = poll_interval
        self.status_interval = status_interval
        self.reconnect_initial = reconnect_initial
        self.reconnect_max = reconnect_max
        self.storages = storages
        self._jobs: queue.Queue[_ModemJob | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._worker_ident: int | None = None
        self._instance_lock_fd: int | None = None
        self._modem: ModemAdapter | None = None
        self._running = False
        self._connected = False
        self._ready = False
        self._probe_status: dict[str, Any] = {}
        self._last_poll_at: str | None = None
        self._last_probe_at: str | None = None
        self._last_error_code: str | None = None
        self._retry_delay = reconnect_initial
        self._next_retry_at = 0.0
        self._device_state_candidates: dict[str, tuple[str, int]] = {}

    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
            if self._worker is not None and self._worker.is_alive():
                raise GatewayUnavailable("the previous modem worker is still stopping")
            if self._worker is not None and not self._worker.is_alive():
                self._jobs = queue.Queue()
            self._acquire_instance_lock()
            try:
                self.store.recover_interrupted_outbound()
            except BaseException:
                self._release_instance_lock()
                raise
            self._running = True
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop, name="dji4g-modem-worker", daemon=True
            )
            try:
                self._worker.start()
            except BaseException:
                self._running = False
                self._release_instance_lock()
                raise

    def stop(self, timeout: float = 5.0) -> None:
        with self._state_lock:
            was_running = self._running
            self._running = False
        if was_running:
            self._stop_event.set()
            while True:
                try:
                    pending = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if pending is not None:
                    pending.error = GatewayUnavailable("gateway is stopping")
                    pending.done.set()
            self._jobs.put(None)
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout)

    def healthy(self) -> bool:
        return bool(self.worker_alive() and self.store.ping())

    def worker_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _acquire_instance_lock(self) -> None:
        if self._instance_lock_fd is not None:
            return
        path = self.store.data_dir / "gateway.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            raise GatewayUnavailable("another gateway process owns the data directory") from error
        self._instance_lock_fd = descriptor

    def _release_instance_lock(self) -> None:
        descriptor, self._instance_lock_fd = self._instance_lock_fd, None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def ready(self) -> bool:
        with self._state_lock:
            return bool(self.healthy() and self._ready)

    def status_snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "healthy": self.healthy(),
                "ready": self._ready,
                "connected": self._connected,
                "modem": dict(self._probe_status),
                "last_probe_at": self._last_probe_at,
                "last_poll_at": self._last_poll_at,
                "last_error_code": self._last_error_code,
                "queue_depth": self._jobs.qsize(),
            }

    def _submit(self, operation: str, *args: Any, timeout: float = 65.0) -> Any:
        with self._state_lock:
            if not self._running or self._worker is None or not self._worker.is_alive():
                raise GatewayUnavailable("gateway modem worker is not running")
        job = _ModemJob(operation=operation, args=args)
        self._jobs.put(job)
        if not job.done.wait(timeout):
            raise TimeoutError("gateway modem worker did not complete in time")
        if job.error is not None:
            raise job.error
        return job.result

    def refresh_status(self, timeout: float = 10.0) -> dict[str, Any]:
        return self._submit("probe", timeout=timeout)

    def poll_now(self, timeout: float = 30.0) -> int:
        return self._submit("poll", timeout=timeout)

    def cleanup_storage(self, timeout: float = CLEANUP_REQUEST_TIMEOUT) -> dict[str, int]:
        return self._submit("cleanup", timeout=timeout)

    def preview_send(self, recipient: Any, body: Any) -> dict[str, Any]:
        return build_send_preview(recipient, body)

    def send_confirmed(
        self,
        recipient: Any,
        body: Any,
        *,
        idempotency_key: Any,
        confirmed: Any,
        timeout: float = SEND_REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise ValueError("confirmed must be true")
        recipient = validate_recipient(recipient)
        body = validate_body(body)
        idempotency_key = validate_idempotency_key(idempotency_key)
        preview = self.preview_send(recipient, body)
        outbound, created = self.store.create_outbound(
            idempotency_key, recipient, body, preview
        )
        if not created:
            return _public_outbound(outbound)
        try:
            result = self._submit("send", outbound["id"], recipient, body, timeout=timeout)
        except GatewayUnavailable as error:
            self.store.update_outbound(
                outbound["id"], "failed", error_code=type(error).__name__
            )
            raise
        return _public_outbound(result)

    def _worker_loop(self) -> None:
        self._worker_ident = threading.get_ident()
        next_probe = 0.0
        next_poll = 0.0
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= next_probe and now >= self._next_retry_at:
                    try:
                        self._perform_probe()
                    except BaseException as error:
                        self._record_modem_failure(error)
                        next_probe = self._next_retry_at
                    else:
                        next_probe = time.monotonic() + self.status_interval
                if self._ready and now >= next_poll:
                    try:
                        self._perform_poll()
                    except BaseException as error:
                        self._record_modem_failure(error)
                        next_probe = self._next_retry_at
                    next_poll = time.monotonic() + self.poll_interval

                try:
                    job = self._jobs.get(timeout=0.05)
                except queue.Empty:
                    continue
                if job is None:
                    break
                try:
                    if job.operation == "probe":
                        job.result = self._perform_probe()
                        next_probe = time.monotonic() + self.status_interval
                    elif job.operation == "poll":
                        job.result = self._perform_poll()
                        next_poll = time.monotonic() + self.poll_interval
                    elif job.operation == "send":
                        job.result = self._perform_send(*job.args)
                    elif job.operation == "cleanup":
                        try:
                            job.result = self._perform_cleanup()
                        finally:
                            # A failure inside cleanup closed the modem.
                            if self._modem is None:
                                next_probe = self._next_retry_at
                    else:
                        raise ValueError("unknown modem operation")
                except BaseException as error:
                    job.error = error
                    if job.operation not in {"send", "cleanup"}:
                        self._record_modem_failure(error)
                        next_probe = self._next_retry_at
                finally:
                    job.done.set()
        finally:
            self._close_modem()
            with self._state_lock:
                self._connected = False
                self._ready = False
            self._release_instance_lock()

    def _ensure_modem(self) -> ModemAdapter:
        if threading.get_ident() != self._worker_ident:
            raise RuntimeError("modem may only be accessed by its worker thread")
        if self._modem is None:
            self._modem = self._modem_factory()
            configure = getattr(self._modem, "configure_delivery_reports", None)
            if configure is not None:
                configure()
        return self._modem

    def _observe_device_state(
        self,
        key: str,
        state: str,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        degraded: bool,
        confirm_after: int = 2,
    ) -> None:
        previous = self.store.get_device_state(key)
        previous_state = previous["state"] if previous is not None else None
        if previous_state == state:
            self._device_state_candidates.pop(key, None)
            return
        required = confirm_after if degraded else 1
        candidate_state, candidate_count = self._device_state_candidates.get(
            key, ("", 0)
        )
        if candidate_state != state:
            candidate_count = 0
        candidate_count += 1
        if candidate_count < required:
            self._device_state_candidates[key] = (state, candidate_count)
            return
        self._device_state_candidates.pop(key, None)
        self.store.record_device_state(
            key,
            state,
            event_type=event_type,
            payload=payload,
            emit_initial=degraded,
        )

    def _observe_probe_states(self, probe: dict[str, Any]) -> None:
        connected = bool(probe.get("connected", True))
        self._observe_device_state(
            "connection",
            "connected" if connected else "disconnected",
            event_type="device.connection",
            payload={} if connected else {"reason": "usb_unavailable"},
            degraded=not connected,
        )
        if not connected:
            return

        sim_ready = probe.get("sim_ready")
        if isinstance(sim_ready, bool):
            self._observe_device_state(
                "sim",
                "ready" if sim_ready else "not_ready",
                event_type="sim.readiness",
                payload={} if sim_ready else {"reason": "sim_not_ready"},
                degraded=not sim_ready,
            )

        registration = probe.get("registration")
        if not isinstance(registration, str):
            registered = probe.get("registered")
            if isinstance(registered, bool):
                registration = "registered" if registered else "not_registered"
        if isinstance(registration, str):
            registration_ok = registration in {"home", "roaming", "registered"}
            self._observe_device_state(
                "registration",
                registration,
                event_type="radio.registration",
                payload={} if registration_ok else {"reason": "registration_unavailable"},
                degraded=not registration_ok,
            )

        storage = probe.get("storage")
        if not isinstance(storage, Mapping):
            return
        for name in ("ME", "SM"):
            item = storage.get(name)
            if not isinstance(item, dict):
                continue
            used = item.get("used")
            total = item.get("total")
            if not isinstance(used, int) or not isinstance(total, int) or total <= 0:
                continue
            previous = self.store.get_device_state(f"storage:{name}")
            previous_state = previous["state"] if previous is not None else None
            state = _storage_state(used, total, previous_state)
            payload = {
                "storage": name,
                "used": used,
                "total": total,
                "free": total - used,
                "utilization_percent": used * 100 / total,
            }
            item.update({key: value for key, value in payload.items() if key != "storage"})
            item["state"] = state
            self._observe_device_state(
                f"storage:{name}",
                state,
                event_type="sms.storage",
                payload=payload,
                degraded=state in {"near_full", "full"},
                confirm_after=1 if state == "full" else 2,
            )

    def _observe_probe_states_safely(self, probe: dict[str, Any]) -> None:
        try:
            self._observe_probe_states(probe)
        except BaseException as error:
            LOGGER.warning("device state persistence failed (%s)", type(error).__name__)

    def _perform_probe(self) -> dict[str, Any]:
        modem = self._ensure_modem()
        method = getattr(modem, "probe", None) or getattr(modem, "status", None)
        if method is None:
            raise GatewayUnavailable("modem adapter does not provide probe")
        probe = _safe_probe_status(method())
        connected = bool(probe.get("connected", True))
        sim_ready = probe.get("sim_ready", True)
        if isinstance(sim_ready, str):
            sim_ready = sim_ready.upper() == "READY"
        ready = bool(probe.get("ready", connected and bool(sim_ready)))
        if connected:
            self._observe_probe_states_safely(probe)
        with self._state_lock:
            self._probe_status = probe
            self._connected = connected
            self._ready = ready
            self._last_probe_at = _utc_now()
            self._last_error_code = None
        if not connected:
            raise GatewayUnavailable("modem probe reported disconnected")
        self._retry_delay = self.reconnect_initial
        self._next_retry_at = 0.0
        return self.status_snapshot()

    def _perform_poll(self) -> int:
        modem = self._ensure_modem()
        method = getattr(modem, "list_messages", None)
        if method is None:
            raise GatewayUnavailable("modem adapter does not provide list_messages")
        ingested = 0
        if _method_accepts_storage(method):
            batches = ((storage, method(storage)) for storage in self.storages)
        else:
            batches = ((None, method()),)
        for storage, messages in batches:
            for message in messages:
                try:
                    result = self.store.ingest(message, storage=storage)
                except ValueError as error:
                    LOGGER.warning("inbound SMS skipped (%s)", type(error).__name__)
                    continue
                if not result.duplicate:
                    ingested += 1
        read_reports = getattr(modem, "read_delivery_reports", None)
        if read_reports is not None:
            for report in read_reports():
                try:
                    self.store.ingest_delivery_report(report)
                except ValueError as error:
                    LOGGER.warning("delivery report skipped (%s)", type(error).__name__)
        with self._state_lock:
            self._connected = True
            self._last_poll_at = _utc_now()
            self._last_error_code = None
        return ingested

    def _perform_cleanup(self) -> dict[str, int]:
        """Free module storage, deleting only SMS whose raw PDU is stored here.

        Each storage is listed and ingested first, so a message that arrived
        after the last poll is saved before its slot is freed; one that cannot
        be saved stays on the module.  A refusal touches no modem state.
        """
        if not self._ready:
            raise GatewayUnavailable("modem is not ready")
        modem = self._modem
        list_messages = getattr(modem, "list_messages", None)
        delete_messages = getattr(modem, "delete_messages", None)
        if (
            list_messages is None
            or delete_messages is None
            or not _method_accepts_storage(list_messages)
        ):
            raise GatewayUnavailable("modem adapter does not provide storage cleanup")
        deleted = kept = 0
        try:
            for storage in self.storages:
                indexes: list[int] = []
                for message in list_messages(storage):
                    index = _object_field(message, "index")
                    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                        kept += 1
                        continue
                    try:
                        self.store.ingest(message, storage=storage)
                    except ValueError as error:
                        LOGGER.warning("inbound SMS kept on the module (%s)", type(error).__name__)
                        kept += 1
                        continue
                    indexes.append(index)
                if indexes:
                    deleted += delete_messages(storage, indexes)
        except BaseException as error:
            self._record_modem_failure(error)
            raise
        try:
            self._perform_probe()
        except BaseException as error:
            # The slots are already free; a later probe reports them.
            self._record_modem_failure(error)
        return {"deleted": deleted, "kept": kept}

    def _perform_send(
        self, outbound_id: int, recipient: str, body: str
    ) -> dict[str, Any]:
        self.store.update_outbound(outbound_id, "sending")
        try:
            modem = self._ensure_modem()
        except BaseException as error:
            result = self.store.update_outbound(
                outbound_id, "failed", error_code=type(error).__name__
            )
            self._record_modem_failure(error, observe_disconnect=False)
            return result
        method = getattr(modem, "send_message", None)
        if method is None:
            error = GatewayUnavailable("modem adapter does not provide send_message")
            result = self.store.update_outbound(
                outbound_id, "failed", error_code=type(error).__name__
            )
            self._record_modem_failure(error, observe_disconnect=False)
            return result
        try:
            modem_result = method(recipient, body)
        except BaseException as error:
            uncertain = any(
                getattr(error, attribute, False) is True
                for attribute in ("submitted", "follow_up_sent", "uncertain")
            )
            sent_parts = getattr(error, "sent_parts", 0)
            if isinstance(sent_parts, int) and not isinstance(sent_parts, bool) and sent_parts > 0:
                uncertain = True
            state = "unknown" if uncertain else "failed"
            result = self.store.update_outbound(
                outbound_id, state, error_code=type(error).__name__
            )
            self._record_modem_failure(error, observe_disconnect=False)
            return result
        return self.store.record_outbound_submission(
            outbound_id,
            _safe_modem_references(modem_result),
        )

    def _record_modem_failure(
        self, error: BaseException, *, observe_disconnect: bool = True
    ) -> None:
        error_code = type(error).__name__
        with self._state_lock:
            self._connected = False
            self._ready = False
            self._last_error_code = error_code
        if observe_disconnect:
            try:
                self._observe_device_state(
                    "connection",
                    "disconnected",
                    event_type="device.connection",
                    payload={"reason": "usb_unavailable"},
                    degraded=True,
                )
            except BaseException as state_error:
                LOGGER.warning(
                    "device state persistence failed (%s)", type(state_error).__name__
                )
        self._close_modem()
        self._next_retry_at = time.monotonic() + self._retry_delay
        self._retry_delay = min(self.reconnect_max, self._retry_delay * 2)
        LOGGER.warning("modem operation failed (%s)", error_code)

    def _close_modem(self) -> None:
        modem, self._modem = self._modem, None
        if modem is None:
            return
        close = getattr(modem, "close", None)
        if close is not None:
            try:
                close()
            except BaseException as error:
                LOGGER.warning("modem close failed (%s)", type(error).__name__)

def _public_outbound(value: Mapping[str, Any]) -> dict[str, Any]:
    preview = value["preview"]
    return {
        "id": value["id"],
        "state": value["state"],
        "recipient": mask_peer(value["recipient"]),
        "preview": preview,
        "encoding": preview["encoding"],
        "segments": preview["segments"],
        "delivery_status": value["delivery_status"],
        "delivery_updated_at": value["delivery_updated_at"],
        "modem_reference": value["modem_reference"],
        "error_code": value["error_code"],
        "created_at": value["created_at"],
        "updated_at": value["updated_at"],
    }
