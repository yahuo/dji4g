#!/usr/bin/env python3
"""Manage and diagnose a first-generation DJI Baiwang 4G USB module."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from dji4g_at import AtBusyError, AtError, AtTransport, PID, VID, response_ok
from dji4g_modem import MAX_SMS_PARTS, SmsModem, SmsModemError
from dji4g_sms import SMSCodecError, build_submit_pdus


MODE_VALUES = {"restore": 0, "mac": 1, "linux": 1, "windows": 3}
DEFAULT_SMS_POLL_SECONDS = 8.0


class Dji4gError(RuntimeError):
    pass


def parse_usbnet(response: str) -> int | None:
    match = re.search(r'\+QCFG:\s*"usbnet",\s*(\d+)', response)
    return int(match.group(1)) if match else None


def parse_registration(response: str) -> int | None:
    match = re.search(r"\+(?:C|CE|CG)REG:\s*\d+,\s*(\d+)", response)
    return int(match.group(1)) if match else None


def parse_hardware_port(output: str, port_name: str) -> str | None:
    for block in re.split(r"\n\s*\n", output):
        if f"Hardware Port: {port_name}" not in block:
            continue
        match = re.search(r"^Device:\s*(\S+)", block, re.MULTILINE)
        return match.group(1) if match else None
    return None


def parse_gateway(dhcp_packet: str) -> str | None:
    match = re.search(r"router \(ip_mult\): \{\s*([^,}\s]+)", dhcp_packet)
    if not match:
        return None
    try:
        return str(ipaddress.ip_address(match.group(1)))
    except ValueError:
        return None


def parse_first_ipv4(output: str) -> str | None:
    for line in output.splitlines():
        value = line.strip()
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.version == 4:
            return value
    return None


AtSession = AtTransport


def print_command(session: AtSession, label: str, command: str, timeout: float = 2.5) -> str:
    print(f"\n[{label}] {command}")
    response = session.send(command, timeout=timeout)
    print(response or "(no response)")
    return response


def command_status(_args: argparse.Namespace) -> None:
    commands = (
        ("Model", "ATI"),
        ("USB mode", 'AT+QCFG="usbnet"'),
        ("SIM", "AT+CPIN?"),
        ("Signal", "AT+CSQ"),
        ("Operator", "AT+COPS?"),
        ("LTE registration", "AT+CEREG?"),
        ("Packet registration", "AT+CGREG?"),
        ("Packet attach", "AT+CGATT?"),
        ("PDP state", "AT+CGACT?"),
        ("PDP parameters", "AT+CGCONTRDP=1"),
        ("Radio", 'AT+QENG="servingcell"'),
        ("USBNet", "AT+QNETDEVSTATUS?"),
    )
    with AtSession() as session:
        for label, command in commands:
            print_command(session, label, command)


def command_sms_check(_args: argparse.Namespace) -> None:
    results = []
    with AtSession() as session:
        for label, command in (
            ("SIM", "AT+CPIN?"),
            ("SMS text/PDU modes", "AT+CMGF=?"),
            ("SMS storage", "AT+CPMS=?"),
            ("Character sets", "AT+CSCS=?"),
            ("New-message indications", "AT+CNMI=?"),
        ):
            results.append(print_command(session, label, command))

    supported = sum(response_ok(response) for response in results[1:])
    print(f"\nSMS capability checks acknowledged: {supported}/{len(results) - 1}")
    if supported != len(results) - 1:
        raise Dji4gError("One or more SMS capability commands were not acknowledged")


def command_set_mode(args: argparse.Namespace) -> None:
    target = MODE_VALUES[args.mode]
    if not args.yes:
        raise Dji4gError(
            "This command persistently changes the module and reboots it. "
            "Re-run with --yes after checking the selected mode."
        )

    with AtSession() as session:
        before_response = print_command(session, "Current USB mode", 'AT+QCFG="usbnet"')
        before = parse_usbnet(before_response)
        if before is None:
            raise Dji4gError("Could not read the current usbnet value; refusing to write")
        if before == target:
            print(f"\nusbnet is already {target}; no change or reboot was performed")
            return

        changed = print_command(session, "Write USB mode", f'AT+QCFG="usbnet",{target}')
        if not response_ok(changed):
            raise Dji4gError("The module did not acknowledge the usbnet write; refusing to reboot")

        verified_response = print_command(session, "Verify USB mode", 'AT+QCFG="usbnet"')
        if parse_usbnet(verified_response) != target:
            raise Dji4gError("The usbnet value did not verify; refusing to reboot")

        try:
            restarted = print_command(session, "Reboot module", "AT+CFUN=1,1", timeout=4)
            if restarted and not response_ok(restarted):
                raise Dji4gError("The module did not acknowledge the reboot command")
        except AtError:
            print("The device disconnected for reboot after the verified write")

    print(f"\nChanged usbnet from {before} to {target}; wait for USB re-enumeration")


def _run(command: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise Dji4gError(f"Required command was not found: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise Dji4gError(f"Command timed out: {' '.join(command)}") from error


@dataclass(frozen=True)
class HostNetwork:
    interface: str
    local_ip: str
    gateway: str


def detect_mac_network() -> HostNetwork:
    if sys.platform != "darwin":
        raise Dji4gError("The host-side network test currently supports macOS only")

    services = _run(["networksetup", "-listallhardwareports"])
    interface = parse_hardware_port(services.stdout, "Baiwang")
    if not interface:
        raise Dji4gError("The Baiwang macOS network interface was not found")

    address = _run(["ipconfig", "getifaddr", interface])
    local_ip = address.stdout.strip()
    try:
        ipaddress.ip_address(local_ip)
    except ValueError as error:
        raise Dji4gError(f"{interface} has no valid IPv4 DHCP address") from error

    packet = _run(["ipconfig", "getpacket", interface])
    gateway = parse_gateway(packet.stdout)
    if not gateway:
        raise Dji4gError(f"Could not read the DHCP gateway for {interface}")

    return HostNetwork(interface=interface, local_ip=local_ip, gateway=gateway)


def detect_linux_network(sysfs_net: Path = Path("/sys/class/net")) -> HostNetwork:
    interfaces = []
    for entry in sysfs_net.iterdir():
        device = (entry / "device").resolve()
        for parent in (device, *device.parents):
            vendor_path = parent / "idVendor"
            product_path = parent / "idProduct"
            if vendor_path.is_file() and product_path.is_file():
                if (
                    vendor_path.read_text().strip().lower() == f"{VID:04x}"
                    and product_path.read_text().strip().lower() == f"{PID:04x}"
                ):
                    interfaces.append(entry.name)
                break
    if len(interfaces) != 1:
        raise Dji4gError(
            "Expected one Baiwang Linux network interface, "
            f"found {len(interfaces)}; check USB ECM mode and the cdc_ether driver"
        )
    interface = interfaces[0]

    def ip_json(*arguments: str) -> list:
        result = _run(["ip", "-j", "-4", *arguments])
        if result.returncode != 0:
            raise Dji4gError(f"Could not inspect {interface}: {result.stderr.strip()}")
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise Dji4gError("ip did not return valid JSON") from error
        if not isinstance(value, list):
            raise Dji4gError("ip did not return a JSON list")
        return value

    addresses = ip_json("address", "show", "dev", interface)
    local_ips = {
        info["local"]
        for address in addresses
        for info in address.get("addr_info", [])
        if info.get("family") == "inet" and info.get("scope") == "global"
    }
    if len(local_ips) != 1:
        raise Dji4gError(f"{interface} needs one global IPv4 address; check DHCP")
    routes = ip_json("route", "show", "table", "all", "dev", interface)
    gateways = {
        route["gateway"] for route in routes
        if route.get("dst") == "default" and route.get("gateway")
    }
    if len(gateways) != 1:
        raise Dji4gError(f"{interface} needs one IPv4 default gateway; check DHCP/routes")
    local_ip, gateway = local_ips.pop(), gateways.pop()
    try:
        ipaddress.IPv4Address(local_ip)
        ipaddress.IPv4Address(gateway)
    except ValueError as error:
        raise Dji4gError(f"{interface} has invalid IPv4 network settings") from error
    return HostNetwork(interface=interface, local_ip=local_ip, gateway=gateway)


def detect_host_network() -> HostNetwork:
    if sys.platform == "darwin":
        return detect_mac_network()
    if sys.platform.startswith("linux"):
        return detect_linux_network()
    raise Dji4gError("The host-side network test supports macOS and Linux only")


def wait_for_registration(session: AtSession, wait_seconds: int) -> None:
    deadline = time.monotonic() + wait_seconds
    last_registration = ""
    last_attach = ""
    while True:
        sim = session.send("AT+CPIN?")
        if "+CPIN: READY" not in sim:
            raise Dji4gError(f"SIM is not ready:\n{sim}")

        last_registration = session.send("AT+CEREG?")
        last_attach = session.send("AT+CGATT?")
        if parse_registration(last_registration) in (1, 5) and "+CGATT: 1" in last_attach:
            return
        if time.monotonic() >= deadline:
            raise Dji4gError(
                "The module did not register and attach before the timeout:\n"
                f"{last_registration}\n{last_attach}"
            )
        time.sleep(5)


def command_network_test(args: argparse.Namespace) -> None:
    with AtSession() as session:
        print(f"Waiting up to {args.wait} seconds for LTE registration...")
        wait_for_registration(session, args.wait)

        for label, command in (
            ("Operator", "AT+COPS?"),
            ("Signal", "AT+CSQ"),
            ("PDP state", "AT+CGACT?"),
            ("PDP parameters", "AT+CGCONTRDP=1"),
            ("USBNet", "AT+QNETDEVSTATUS?"),
        ):
            print_command(session, label, command)

    network = detect_host_network()
    print(
        f"\n[{sys.platform}] interface={network.interface} "
        f"local_ip={network.local_ip} gateway={network.gateway}"
    )

    gateway_test = _run(
        ["ping", "-c", "2", "-S" if sys.platform == "darwin" else "-I",
         network.local_ip, network.gateway], timeout=8
    )
    if gateway_test.returncode != 0:
        raise Dji4gError(f"The module gateway is unreachable:\n{gateway_test.stdout}{gateway_test.stderr}")
    print("Module gateway: reachable")

    parsed_url = urlparse(args.url)
    if parsed_url.scheme != "https" or not parsed_url.hostname:
        raise Dji4gError("--url must be an HTTPS URL")
    if parsed_url.port not in (None, 443):
        raise Dji4gError("--url currently supports HTTPS port 443 only")

    dns = _run(
        [
            "dig",
            "-4",
            "-b",
            network.local_ip,
            f"@{network.gateway}",
            parsed_url.hostname,
            "+short",
            "+time=5",
            "+tries=1",
        ],
        timeout=8,
    )
    resolved_ip = parse_first_ipv4(dns.stdout)
    if not resolved_ip:
        raise Dji4gError(f"DNS through the module failed:\n{dns.stdout}{dns.stderr}")
    print(f"Module DNS: {parsed_url.hostname} -> {resolved_ip}")

    curl = _run(
        [
            "curl",
            "-4",
            "--noproxy",
            "*",
            "--interface",
            network.interface,
            "--resolve",
            f"{parsed_url.hostname}:443:{resolved_ip}",
            "--connect-timeout",
            "8",
            "--max-time",
            "20",
            "--silent",
            "--show-error",
            "--output",
            "/dev/null",
            "--write-out",
            "HTTP=%{http_code} REMOTE=%{remote_ip} CONNECT=%{time_connect}s "
            "TLS=%{time_appconnect}s TOTAL=%{time_total}s\\n",
            args.url,
        ],
        timeout=25,
    )
    print(curl.stdout.strip())
    if curl.returncode != 0:
        raise Dji4gError(f"HTTPS through {network.interface} failed:\n{curl.stderr.strip()}")
    match = re.search(r"HTTP=(\d{3})", curl.stdout)
    if not match or int(match.group(1)) >= 400:
        raise Dji4gError("HTTPS connected, but the test URL returned an error status")
    print("Network test: PASS")


def command_modem_ping(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", args.host):
        raise Dji4gError("The ping host contains unsupported characters")
    command = f'AT+QPING=1,"{args.host}",{args.timeout},{args.count}'

    def completed(response: str) -> bool:
        return bool(
            re.search(r"\+QPING:\s*[5-9]\d\d(?:$|\n)", response)
            or re.search(r"\+QPING:\s*0,4,", response)
        )

    with AtSession() as session:
        response = session.send(
            command,
            timeout=args.timeout * args.count + 5,
            completion=completed,
        )
    print(response or "(no response)")
    if re.search(r"\+QPING:\s*[5-9]\d\d(?:$|\n)", response):
        raise Dji4gError(
            "The module-side QPING failed. Run network-test as the authoritative host-path test."
        )


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed





def _field(value: object, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping):
            candidate = value.get(name)
        else:
            candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return default


def mask_address(value: object) -> str:
    """Return a display-safe address without logging a full sender or recipient."""

    if not isinstance(value, str) or not value:
        return "unknown"
    prefix = "+" if value.startswith("+") else ""
    digits = value[1:] if prefix else value
    if digits.isdigit():
        visible = digits[-2:] if len(digits) >= 2 else ""
        hidden = "*" * max(3, len(digits) - len(visible))
        return prefix + hidden + visible
    return "***"


def _display_timestamp(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _message_view(message: object, *, show_sender: bool) -> dict[str, Any]:
    sender = _field(message, "peer", "sender")
    concat = _field(message, "concat")
    total_parts = _field(message, "concat_total")
    if total_parts is None and concat is not None:
        total_parts = _field(concat, "total_parts")
    parts_received = _field(message, "parts_received", default=())
    return {
        "id": _field(message, "id"),
        "storage": _field(message, "storage"),
        "index": _field(message, "storage_index", "index"),
        "status": _field(message, "status"),
        "sender": sender if show_sender else mask_address(sender),
        "timestamp": _display_timestamp(
            _field(message, "received_at", "timestamp", "created_at")
        ),
        "encoding": _field(message, "encoding"),
        "complete": bool(_field(message, "complete", default=True)),
        "total_parts": total_parts,
        "parts_received": list(parts_received or ()),
        "text": _field(message, "body", "text"),
    }


def _print_messages(
    messages: list[object], *, json_output: bool, show_sender: bool
) -> None:
    views = [_message_view(message, show_sender=show_sender) for message in messages]
    if json_output:
        print(json.dumps(views, ensure_ascii=False))
        return
    if not views:
        print("No messages.")
        return
    for view in views:
        location = view["storage"] or "?"
        if view["index"] is not None:
            location += f":{view['index']}"
        identifier = f" id={view['id']}" if view["id"] is not None else ""
        timestamp = f" time={view['timestamp']}" if view["timestamp"] else ""
        completeness = "complete" if view["complete"] else "incomplete"
        print(
            f"[{location}{identifier}] from={view['sender']} "
            f"encoding={view['encoding'] or 'unknown'} {completeness}{timestamp}"
        )
        print(view["text"] if view["text"] is not None else "(multipart SMS incomplete)")


def _print_watch_message(
    message: object, *, json_output: bool, show_sender: bool
) -> None:
    if json_output:
        view = _message_view(message, show_sender=show_sender)
        print(json.dumps(view, ensure_ascii=False), flush=True)
        return
    _print_messages([message], json_output=False, show_sender=show_sender)
    sys.stdout.flush()


def _selected_storages(storage: str) -> tuple[str, ...]:
    return ("ME", "SM") if storage == "all" else (storage,)


def _direct_list(storage: str, mode: str) -> list[object]:
    modem = SmsModem.open()
    try:
        return modem.list_complete_messages(_selected_storages(storage), mode=mode)
    finally:
        modem.close()


def _direct_send(
    recipient: str, body: str, *, encoding: str, mode: str
) -> object:
    modem = SmsModem.open()
    try:
        return modem.send_sms(recipient, body, encoding=encoding, mode=mode)
    finally:
        modem.close()


def _read_sms_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    if str(args.text_file) == "-":
        try:
            stream = getattr(sys.stdin, "buffer", None)
            if stream is None:
                return sys.stdin.read()
            return stream.read().decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise Dji4gError("stdin must contain valid UTF-8 text") from error
    try:
        return Path(args.text_file).expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise Dji4gError(f"Could not read --text-file ({type(error).__name__})") from error
    except UnicodeError as error:
        raise Dji4gError("--text-file must contain valid UTF-8 text") from error


def _local_sms_preview(recipient: str, body: str, encoding: str) -> list[object]:
    parts = build_submit_pdus(recipient, body, encoding=encoding)
    if len(parts) > MAX_SMS_PARTS:
        raise Dji4gError(
            f"SMS would use {len(parts)} segments; maximum is {MAX_SMS_PARTS}"
        )
    selected_encoding = _field(parts[0], "encoding", default="unknown")
    print(
        f"SMS preview: to={mask_address(recipient)} "
        f"encoding={selected_encoding} segments={len(parts)}"
    )
    return list(parts)


def _confirm_sms_send() -> bool:
    try:
        with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as terminal:
            terminal.write("Type SEND to submit this SMS: ")
            terminal.flush()
            return terminal.readline().rstrip("\r\n") == "SEND"
    except OSError as error:
        raise Dji4gError(
            "A controlling terminal is required for SMS confirmation"
        ) from error


def command_sms_list(args: argparse.Namespace) -> None:
    messages = _direct_list(args.storage, args.mode)
    _print_messages(
        list(messages), json_output=args.json, show_sender=args.show_sender
    )


def command_sms_send(args: argparse.Namespace) -> None:
    body = _read_sms_text(args)
    _local_sms_preview(args.to, body, args.encoding)
    if not args.execute:
        print("Preview only; no SMS was sent. Re-run with --execute to request confirmation.")
        return
    if not _confirm_sms_send():
        raise Dji4gError("SMS send was not confirmed; nothing was sent")

    result = _direct_send(
        args.to,
        body,
        encoding=args.encoding,
        mode=args.mode,
    )
    state = _field(result, "state", "status", default="accepted_by_modem")
    segments = _field(result, "segments", "segment_count")
    suffix = f" segments={segments}" if segments is not None else ""
    print(f"SMS submission: state={state}{suffix}")


def _message_fingerprint(message: object) -> tuple[object, ...]:
    raw_pdus = tuple(_field(message, "raw_pdus", default=()) or ())
    if raw_pdus:
        return ("pdu",) + raw_pdus
    raw_pdu = _field(message, "raw_pdu")
    if raw_pdu:
        return ("pdu", raw_pdu)
    return (
        "text",
        _field(message, "storage"),
        _field(message, "storage_index", "index"),
        _field(message, "peer", "sender"),
        _display_timestamp(_field(message, "received_at", "timestamp")),
        _field(message, "body", "text"),
    )


def _watch_direct(args: argparse.Namespace) -> None:
    modem = SmsModem.open()
    try:
        initial = modem.list_complete_messages(
            _selected_storages(args.storage), mode=args.mode
        )
        seen = {_message_fingerprint(message) for message in initial}
        print("Watching for new SMS with exclusive direct modem access...", file=sys.stderr)
        while True:
            time.sleep(args.poll)
            messages = modem.list_complete_messages(
                _selected_storages(args.storage), mode=args.mode
            )
            for message in messages:
                fingerprint = _message_fingerprint(message)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                _print_watch_message(
                    message,
                    json_output=args.json,
                    show_sender=args.show_sender,
                )
    finally:
        modem.close()


def command_sms_watch(args: argparse.Namespace) -> None:
    if args.mode == "text":
        raise Dji4gError(
            "sms watch requires PDU mode because Text CMGL has no safe record framing"
        )
    _watch_direct(args)


def _add_sms_view_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--storage", choices=("all", "ME", "SM"), default="all")
    parser.add_argument("--mode", choices=("pdu", "text"), default="pdu")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--show-sender",
        action="store_true",
        help="show full sender addresses in this local terminal",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnose and manage a first-generation DJI Baiwang 4G USB module"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="show modem, SIM, radio and data status")
    status.set_defaults(handler=command_status)

    sms = subparsers.add_parser("sms-check", help="check SMS AT-command support without sending SMS")
    sms.set_defaults(handler=command_sms_check)

    set_mode = subparsers.add_parser("set-mode", help="persistently change the USB networking mode")
    set_mode.add_argument("mode", choices=tuple(MODE_VALUES))
    set_mode.add_argument(
        "--yes",
        action="store_true",
        help="confirm the persistent write and module reboot",
    )
    set_mode.set_defaults(handler=command_set_mode)

    network = subparsers.add_parser(
        "network-test",
        help="verify LTE, DHCP, module DNS and HTTPS through Baiwang on macOS/Linux",
    )
    network.add_argument("--wait", type=int, default=60, help="registration timeout in seconds")
    network.add_argument(
        "--url",
        default="https://www.baidu.com/",
        help="HTTPS URL used for the bound-interface test",
    )
    network.set_defaults(handler=command_network_test)

    modem_ping = subparsers.add_parser(
        "modem-ping",
        help="run the modem's QPING diagnostic (network-test is more authoritative)",
    )
    modem_ping.add_argument("host", nargs="?", default="1.1.1.1")
    modem_ping.add_argument("--timeout", type=int, choices=range(1, 256), default=4)
    modem_ping.add_argument("--count", type=int, choices=range(1, 11), default=3)
    modem_ping.set_defaults(handler=command_modem_ping)

    sms = subparsers.add_parser(
        "sms", help="list, send or watch SMS with exclusive modem access"
    )
    sms_subparsers = sms.add_subparsers(dest="sms_command", required=True)

    sms_list = sms_subparsers.add_parser(
        "list", help="list SMS in the modem's ME/SM storage"
    )
    _add_sms_view_options(sms_list)
    sms_list.set_defaults(handler=command_sms_list)

    sms_send = sms_subparsers.add_parser(
        "send", help="preview an SMS and optionally submit it after TTY confirmation"
    )
    sms_send.add_argument("--to", required=True, help="numeric SMS recipient")
    text_source = sms_send.add_mutually_exclusive_group(required=True)
    text_source.add_argument("--text", help="SMS text (prefer --text-file for secrets)")
    text_source.add_argument("--text-file", type=Path, help="UTF-8 file containing SMS text")
    sms_send.add_argument(
        "--encoding",
        choices=("auto", "gsm7", "ucs2"),
        default="auto",
        help="SMS encoding (default: %(default)s)",
    )
    sms_send.add_argument(
        "--mode",
        choices=("auto", "pdu", "text"),
        default="auto",
        help="send mode (default: %(default)s)",
    )
    sms_send.add_argument(
        "--execute",
        action="store_true",
        help="request an additional exact SEND confirmation from /dev/tty",
    )
    sms_send.set_defaults(handler=command_sms_send)

    sms_watch = sms_subparsers.add_parser(
        "watch", help="print newly received SMS without deleting modem storage"
    )
    _add_sms_view_options(sms_watch)
    sms_watch.add_argument(
        "--poll",
        type=_positive_float,
        default=DEFAULT_SMS_POLL_SECONDS,
        help="poll interval in seconds (default: %(default)s)",
    )
    sms_watch.set_defaults(handler=command_sms_watch)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except Dji4gError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except AtBusyError as error:
        print(
            f"ERROR: {error}. The SMS engine of the macOS menu bar app or the "
            "Omarchy panel holds the modem while it runs; quit it first.",
            file=sys.stderr,
        )
        return 1
    except AtError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (
        SMSCodecError,
        SmsModemError,
        ValueError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
