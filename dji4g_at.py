"""Reusable PyUSB AT transport for the first-generation DJI Baiwang module.

The transport owns one USB AT endpoint pair for its lifetime.  Production use
also owns a non-blocking cross-process file lock, while tests may inject fake
endpoints and explicitly disable that lock.
"""

from __future__ import annotations

import errno
import fcntl
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dji4g_paths import default_data_dir

try:
    import usb.core as usb_core
    import usb.util as usb_util
except ModuleNotFoundError:
    usb_core = None
    usb_util = None


VID = 0x2CA3
PID = 0x4006
DEFAULT_LOCK_PATH = default_data_dir() / "at.lock"

Completion = Callable[[str], bool]

_TERMINAL_RE = re.compile(
    r"(?:^|\n)(?:OK|ERROR|\+CME ERROR(?::[^\n]*)?|\+CMS ERROR(?::[^\n]*)?)(?:$|\n)"
)
_ERROR_RE = re.compile(
    r"(?:^|\n)(?:ERROR|\+CME ERROR(?::[^\n]*)?|\+CMS ERROR(?::[^\n]*)?)(?:$|\n)"
)
_PROMPT_RE = re.compile(br"(?:^|[\r\n])>[ ]?")
_PDU_STATUS_REPORT_RE = re.compile(
    br"(?m)(?:(?<=\n)|\A)(\+CDS: [0-9]+\r?\n[0-9A-Fa-f]+)(?=\r?\n)"
)


class AtError(RuntimeError):
    """Base error for the AT transport."""


class AtBusyError(AtError):
    """Another process already owns the modem AT endpoint."""


class AtClosedError(AtError):
    """The transport has already been closed."""


class AtIoError(AtError):
    """A non-timeout USB read or write failed."""


class AtTimeoutError(AtError):
    """An AT operation did not reach its completion condition in time."""

    def __init__(
        self,
        message: str,
        *,
        partial_response: str = "",
        follow_up_sent: bool = False,
    ) -> None:
        super().__init__(message)
        self.partial_response = partial_response
        self.follow_up_sent = follow_up_sent
        # ``submitted`` is the SMS-oriented spelling used by gateway callers.
        self.submitted = follow_up_sent


def _normalize(raw: bytes | bytearray) -> str:
    return bytes(raw).decode(errors="replace").replace("\r", "").strip()


def _extract_pdu_status_reports(raw: bytes | bytearray) -> tuple[bytes, list[bytes]]:
    data = bytes(raw)
    reports = [match.group(1) for match in _PDU_STATUS_REPORT_RE.finditer(data)]
    if not reports:
        return data, []
    return _PDU_STATUS_REPORT_RE.sub(b"", data), reports


def response_complete(response: str) -> bool:
    """Return whether *response* contains an exact terminal result line."""

    normalized = response.replace("\r", "").strip()
    return bool(_TERMINAL_RE.search(normalized))


def response_ok(response: str) -> bool:
    """Return whether *response* contains an exact ``OK`` line."""

    return any(line.strip() == "OK" for line in response.replace("\r", "").splitlines())


def _response_error(response: str) -> bool:
    normalized = response.replace("\r", "").strip()
    return bool(_ERROR_RE.search(normalized))


def _is_timeout(error: BaseException) -> bool:
    return (
        getattr(error, "errno", None) in (errno.ETIMEDOUT, 60, 110)
        or "timed out" in str(error).lower()
        or "timeout" in str(error).lower()
    )


def _validate_timeout(value: float, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        comparison = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {comparison} and finite")
    return result


def _validate_command(command: str) -> bytes:
    if not isinstance(command, str):
        raise TypeError("command must be a string")
    if not command.strip():
        raise ValueError("command must not be empty")
    if "\r" in command or "\n" in command:
        raise ValueError("command must not contain CR or LF")
    try:
        return command.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("command must contain ASCII characters only") from error


@dataclass(frozen=True)
class CommandWithPrompt:
    """An AT command followed by raw bytes after the modem emits ``>``.

    ``follow_up`` is written verbatim.  A caller sending an SMS PDU must append
    Ctrl+Z itself; the transport deliberately adds neither Ctrl+Z nor CR/LF.
    """

    command: str
    follow_up: bytes
    prompt_timeout: float = 2.5
    timeout: float = 60.0
    completion: Completion | None = None


class InterProcessLock:
    """A private, non-blocking advisory lock backed by ``flock``."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_LOCK_PATH) -> None:
        self.path = Path(path).expanduser()
        self._fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> InterProcessLock:
        if self._fd is not None:
            return self

        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
        except OSError as error:
            raise AtError(f"Could not prepare AT lock file: {error}") from error

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise AtBusyError(
                    "The Baiwang AT endpoint is already owned by another process"
                ) from error
            raise AtError(f"Could not acquire AT lock: {error}") from error

        self._fd = fd
        return self

    def close(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> InterProcessLock:
        return self.acquire()

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


class AtTransport:
    """Exclusive, thread-safe transport for one Baiwang USB AT endpoint pair."""

    def __init__(
        self,
        *,
        device=None,
        out_endpoint=None,
        in_endpoint=None,
        usb_core_module=None,
        usb_util_module=None,
        lock_path: str | os.PathLike[str] = DEFAULT_LOCK_PATH,
        use_lock: bool = True,
        probe_timeout: float = 0.8,
        read_size: int = 512,
    ) -> None:
        if (out_endpoint is None) != (in_endpoint is None):
            raise ValueError("out_endpoint and in_endpoint must be supplied together")
        if isinstance(read_size, bool) or not isinstance(read_size, int) or read_size <= 0:
            raise ValueError("read_size must be a positive integer")

        self._usb_core = usb_core if usb_core_module is None else usb_core_module
        self._usb_util = usb_util if usb_util_module is None else usb_util_module
        self._mutex = threading.Lock()
        self._process_lock = InterProcessLock(lock_path) if use_lock else None
        self._read_size = read_size
        self._unsolicited_queue: list[bytes] = []
        self._unsolicited_fragment = bytearray()
        self.device = device
        self.out_endpoint = None
        self.in_endpoint = None
        self._closed = True

        if self._process_lock is not None:
            self._process_lock.acquire()

        try:
            if out_endpoint is not None:
                self.out_endpoint = out_endpoint
                self.in_endpoint = in_endpoint
            else:
                self._open_usb(_validate_timeout(probe_timeout, "probe_timeout"))
            self._closed = False
        except BaseException:
            self._release_resources()
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    def _open_usb(self, probe_timeout: float) -> None:
        if self._usb_core is None or self._usb_util is None:
            raise AtError("PyUSB is not installed")
        if self.device is None:
            try:
                self.device = self._usb_core.find(idVendor=VID, idProduct=PID)
            except Exception as error:
                raise AtIoError(f"Could not discover Baiwang USB device (check libusb): {error}") from error
        if self.device is None:
            raise AtError("Baiwang USB device 2ca3:4006 was not found")

        try:
            self.device.get_active_configuration()
        except Exception:
            try:
                self.device.set_configuration()
            except Exception as error:
                raise AtIoError(f"Could not configure the Baiwang USB device: {error}") from error

        self._probe_errors: list[str] = []
        for out_candidate, in_candidate in self._bulk_endpoint_pairs():
            if self._probe_pair(out_candidate, in_candidate, probe_timeout):
                self.out_endpoint = out_candidate
                self.in_endpoint = in_candidate
                return
        detail = "; ".join(dict.fromkeys(self._probe_errors))
        raise AtError(
            "The module was found, but no responsive bulk AT endpoint was found"
            + (f": {detail}" if detail else "")
        )

    def _bulk_endpoint_pairs(self):
        for configuration in self.device:
            for interface in configuration:
                if sys.platform.startswith("linux"):
                    # Leave CDC control/data and wireless networking to the kernel.
                    if interface.bInterfaceClass in (0x02, 0x0A, 0xE0):
                        continue
                    number = interface.bInterfaceNumber
                    try:
                        active = self.device.is_kernel_driver_active(number)
                    except Exception as error:
                        raise AtIoError(
                            "Could not inspect USB interface; check the Linux udev permissions: "
                            f"{error}"
                        ) from error
                    if active:
                        self._probe_errors.append(
                            f"interface {number} is owned by a kernel driver; not detached"
                        )
                        continue
                out_endpoints = []
                in_endpoints = []
                for endpoint in interface:
                    try:
                        endpoint_type = self._usb_util.endpoint_type(endpoint.bmAttributes)
                        direction = self._usb_util.endpoint_direction(endpoint.bEndpointAddress)
                    except (AttributeError, TypeError, ValueError):
                        continue
                    if endpoint_type != self._usb_util.ENDPOINT_TYPE_BULK:
                        continue
                    if direction == self._usb_util.ENDPOINT_OUT:
                        out_endpoints.append(endpoint)
                    elif direction == self._usb_util.ENDPOINT_IN:
                        in_endpoints.append(endpoint)
                for out_endpoint in out_endpoints:
                    for in_endpoint in in_endpoints:
                        yield out_endpoint, in_endpoint

    def _probe_pair(self, out_endpoint, in_endpoint, timeout: float) -> bool:
        self._drain_probe_input(in_endpoint)
        try:
            self._write_to(out_endpoint, b"AT\r\n", timeout)
        except (AtIoError, TimeoutError) as error:
            self._probe_errors.append(str(error))
            return False

        deadline = time.monotonic() + timeout
        chunks = bytearray()
        while time.monotonic() < deadline:
            try:
                chunks.extend(self._read_from(in_endpoint, deadline - time.monotonic()))
            except TimeoutError:
                continue
            except AtIoError as error:
                self._probe_errors.append(str(error))
                return False
            response = _normalize(chunks)
            if response_ok(response):
                return True
            if response_complete(response):
                return False
        return False

    def _drain_probe_input(self, in_endpoint) -> None:
        while True:
            try:
                self._read_from(in_endpoint, 0.01)
            except (TimeoutError, AtIoError):
                return

    def _require_open(self) -> None:
        if self._closed:
            raise AtClosedError("The AT transport is closed")

    def _write_to(self, endpoint, data: bytes, timeout: float) -> None:
        timeout_ms = max(1, math.ceil(timeout * 1000))
        try:
            written = endpoint.write(data, timeout=timeout_ms)
        except Exception as error:
            if _is_timeout(error):
                raise TimeoutError from error
            raise AtIoError(f"USB AT write failed: {error}") from error
        if written is not None and written != len(data):
            raise AtIoError(f"USB AT write was incomplete ({written}/{len(data)} bytes)")

    def _read_from(self, endpoint, timeout: float) -> bytes:
        timeout_ms = max(1, min(250, math.ceil(max(timeout, 0.001) * 1000)))
        try:
            return bytes(endpoint.read(self._read_size, timeout=timeout_ms))
        except Exception as error:
            if _is_timeout(error):
                raise TimeoutError from error
            raise AtIoError(f"USB AT read failed: {error}") from error

    @staticmethod
    def _is_done(response: str, completion: Completion | None) -> bool:
        if completion is None:
            return response_complete(response)
        return bool(completion(response) or _response_error(response))

    def _read_command_response(
        self,
        *,
        timeout: float,
        completion: Completion | None,
        initial: bytes | bytearray = b"",
        follow_up_sent: bool = False,
    ) -> str:
        deadline = time.monotonic() + timeout
        chunks = bytearray(initial)
        while True:
            filtered, reports = _extract_pdu_status_reports(chunks)
            if reports:
                self._unsolicited_queue.extend(reports)
                chunks = bytearray(filtered)
            response = _normalize(chunks)
            if self._is_done(response, completion):
                return response
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AtTimeoutError(
                    "AT command timed out before completion",
                    partial_response=response,
                    follow_up_sent=follow_up_sent,
                )
            try:
                chunks.extend(self._read_from(self.in_endpoint, remaining))
            except TimeoutError:
                continue

    def send(
        self,
        command: str,
        timeout: float = 2.5,
        completion: Completion | None = None,
    ) -> str:
        """Send one CRLF-terminated command and return its normalized transcript."""

        command_bytes = _validate_command(command)
        timeout = _validate_timeout(timeout, "timeout")
        if completion is not None and not callable(completion):
            raise TypeError("completion must be callable")

        with self._mutex:
            self._require_open()
            try:
                self._write_to(self.out_endpoint, command_bytes + b"\r\n", timeout)
            except TimeoutError as error:
                raise AtTimeoutError("AT command write timed out") from error
            return self._read_command_response(timeout=timeout, completion=completion)

    def send_with_prompt(
        self,
        command: str,
        follow_up: bytes,
        *,
        prompt_timeout: float = 2.5,
        timeout: float = 60.0,
        completion: Completion | None = None,
    ) -> str:
        """Send a command, wait for ``>``, then write *follow_up* verbatim."""

        command_bytes = _validate_command(command)
        if not isinstance(follow_up, bytes):
            raise TypeError("follow_up must be bytes")
        if not follow_up:
            raise ValueError("follow_up must not be empty")
        prompt_timeout = _validate_timeout(prompt_timeout, "prompt_timeout")
        timeout = _validate_timeout(timeout, "timeout")
        if completion is not None and not callable(completion):
            raise TypeError("completion must be callable")

        with self._mutex:
            self._require_open()
            try:
                self._write_to(self.out_endpoint, command_bytes + b"\r\n", prompt_timeout)
            except TimeoutError as error:
                raise AtTimeoutError("AT prompt command write timed out") from error

            deadline = time.monotonic() + prompt_timeout
            chunks = bytearray()
            while True:
                filtered, reports = _extract_pdu_status_reports(chunks)
                if reports:
                    self._unsolicited_queue.extend(reports)
                    chunks = bytearray(filtered)
                response = _normalize(chunks)
                prompt_match = _PROMPT_RE.search(chunks)
                if prompt_match is not None:
                    break
                if response_complete(response):
                    return response
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._try_escape()
                    raise AtTimeoutError(
                        "AT command timed out waiting for prompt",
                        partial_response=response,
                    )
                try:
                    chunks.extend(self._read_from(self.in_endpoint, remaining))
                except TimeoutError:
                    continue

            try:
                self._write_to(self.out_endpoint, follow_up, timeout)
            except TimeoutError as error:
                self._try_escape()
                raise AtTimeoutError(
                    "AT follow-up write timed out",
                    partial_response=_normalize(chunks),
                    follow_up_sent=True,
                ) from error

            try:
                return self._read_command_response(
                    timeout=timeout,
                    completion=completion,
                    initial=chunks[prompt_match.end() :],
                    follow_up_sent=True,
                )
            except AtTimeoutError:
                self._try_escape()
                raise

    def execute(self, command: str | CommandWithPrompt, **kwargs) -> str:
        """Execute a plain command string or a :class:`CommandWithPrompt`."""

        if isinstance(command, str):
            return self.send(command, **kwargs)
        if not isinstance(command, CommandWithPrompt):
            raise TypeError("command must be a string or CommandWithPrompt")
        if kwargs:
            raise TypeError("keyword overrides are not accepted for CommandWithPrompt")
        return self.send_with_prompt(
            command.command,
            command.follow_up,
            prompt_timeout=command.prompt_timeout,
            timeout=command.timeout,
            completion=command.completion,
        )

    def _try_escape(self) -> None:
        try:
            self._write_to(self.out_endpoint, b"\x1b", 1.0)
        except (AtIoError, TimeoutError):
            pass

    def read_unsolicited(self, timeout: float = 0.0) -> str:
        """Read currently available unsolicited bytes without issuing a command."""

        timeout = _validate_timeout(timeout, "timeout", allow_zero=True)
        with self._mutex:
            self._require_open()
            if self._unsolicited_queue:
                queued = b"\r\n".join(self._unsolicited_queue)
                self._unsolicited_queue.clear()
                return _normalize(queued)
            deadline = time.monotonic() + max(timeout, 0.01)
            chunks = self._unsolicited_fragment
            self._unsolicited_fragment = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._finish_unsolicited(chunks)
                try:
                    chunks.extend(self._read_from(self.in_endpoint, remaining))
                except TimeoutError:
                    return self._finish_unsolicited(chunks)

    def _finish_unsolicited(self, chunks: bytes | bytearray) -> str:
        data = bytes(chunks)
        boundary = max(data.rfind(b"\r"), data.rfind(b"\n"))
        if boundary < 0:
            self._unsolicited_fragment.extend(data)
            return ""
        self._unsolicited_fragment.extend(data[boundary + 1 :])
        return _normalize(data[: boundary + 1])

    def _release_resources(self) -> None:
        if self.device is not None and self._usb_util is not None:
            try:
                self._usb_util.dispose_resources(self.device)
            except Exception:
                pass
        if self._process_lock is not None:
            self._process_lock.close()

    def close(self) -> None:
        """Release USB resources and the process lock.  Safe to call repeatedly."""

        with self._mutex:
            if self._closed:
                return
            self._closed = True
            self._release_resources()
            self.out_endpoint = None
            self.in_endpoint = None

    def __enter__(self) -> AtTransport:
        self._require_open()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


__all__ = [
    "VID",
    "PID",
    "DEFAULT_LOCK_PATH",
    "AtError",
    "AtBusyError",
    "AtClosedError",
    "AtIoError",
    "AtTimeoutError",
    "CommandWithPrompt",
    "InterProcessLock",
    "AtTransport",
    "response_complete",
    "response_ok",
]
