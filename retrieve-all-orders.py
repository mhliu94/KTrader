#!/usr/bin/env python3
"""Export every available US stock order and email the CSV.

Run from KTrader (or invoke this file by its full path):
    ./retrieve-all-orders.py --configure-email   # one-time SMTP setup per box
    ./retrieve-all-orders.py                     # export and email
    ./retrieve-all-orders.py --export-only       # query without sending email

Uses tiger_openapi_config.properties and TIGER_* / TIGEROPEN_* environment settings.
The string ID comes from --string-id, TIGER_UI_ACCOUNT_IDS, or the literal
TIGER_UI_ACCOUNT_IDS default in start_tiger_listener.sh. The listener is never
executed. Use --account AND --string-id when overriding the local account.

Email defaults to 922lmh922@gmail.com. SMTP settings come from the ignored
retrieve-all-orders.local.json file or SMTP_HOST, SMTP_PORT, SMTP_SECURITY
(ssl/starttls), SMTP_USER, SMTP_PASSWORD/SMTP_PASSWORD_FILE, and SMTP_FROM.
For Gmail use smtp.gmail.com:465, SSL, and an app password, not a login password:
https://support.google.com/accounts/answer/185833

Requires Python 3.9+ and tigeropen==3.6.0. Automatically uses .venv/bin/python
when the current Python lacks tigeropen. No additional email packages needed.
"""

import argparse
import csv
import getpass
import hashlib
import importlib.util
import json
import logging
import os
import re
import smtplib
import socket
import ssl
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RECIPIENT = "922lmh922@gmail.com"
PAGE_SIZE = 300
ORDER_FIELDS = (
    "id", "order_id", "parent_id", "external_id", "action", "order_type", "status",
    "quantity", "quantity_scale", "filled", "filled_scale", "remaining", "limit_price",
    "aux_price", "avg_fill_price", "commission", "gst", "realized_pnl", "time_in_force",
    "outside_rth", "trading_session_type", "order_time", "trade_time", "update_time",
    "expire_time", "trail_stop_price", "trailing_percent", "percent_offset", "source",
    "reason", "user_mark", "liquidation", "algo_strategy", "total_cash_amount",
    "filled_cash_amount", "refund_cash_amount",
)
CONTRACT_FIELDS = ("symbol", "sec_type", "market", "currency", "exchange", "contract_id", "local_symbol")
TIME_FIELDS = ("order_time", "trade_time", "update_time", "expire_time")
CSV_FIELDS = ("string_id", "account", *CONTRACT_FIELDS, *ORDER_FIELDS,
              *(name + "_utc" for name in TIME_FIELDS))


class ExportError(Exception):
    """An actionable error whose message is safe to print."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--properties", "--tiger-props", help="Tiger properties file or directory")
    parser.add_argument("--account", help="Override Tiger account; requires --string-id")
    parser.add_argument("--string-id", help="Local account label, e.g. TIGER_RONGHUANG")
    parser.add_argument("--email-config", type=Path, default=ROOT / "retrieve-all-orders.local.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "order_exports")
    parser.add_argument("--to", default=RECIPIENT, help="Recipient (default: %(default)s)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--configure-email", action="store_true", help="Interactively save SMTP settings, then exit")
    mode.add_argument("--export-only", action="store_true", help="Save CSV without sending email")
    args = parser.parse_args(argv)
    if args.account and not args.string_id:
        parser.error("--account requires --string-id to avoid labeling another account with the local ID")
    return args


def load_email_config(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ExportError(f"Cannot read email configuration: {path}") from None
    if not isinstance(data, dict):
        raise ExportError("Email configuration must be a JSON object.")
    return data


def atomic_write(path, content):
    """Owner-only file, atomically replaced after successful serialization."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure_email(path):
    previous = load_email_config(path)

    def prompt(label, key, default):
        value = previous.get(key, default)
        return input(f"{label} [{value}]: ").strip() or value

    data = {
        "host": prompt("SMTP host", "host", "smtp.gmail.com"),
        "security": prompt("SMTP security (ssl/starttls)", "security", "ssl"),
        "username": prompt("SMTP username", "username", RECIPIENT),
    }
    data["port"] = prompt("SMTP port", "port", 465 if data["security"] == "ssl" else 587)
    data["from"] = prompt("Sender email", "from", data["username"])
    password = getpass.getpass("SMTP app password (blank keeps the saved password): ")
    data["password"] = password or previous.get("password", "")
    if data["host"] == "smtp.gmail.com":
        data["password"] = data["password"].replace(" ", "")
    # Validate the saved values independently of any ambient SMTP variables.
    smtp_settings(data, environ={})
    atomic_write(path, (json.dumps(data, indent=2) + "\n").encode())
    print(f"Saved owner-only SMTP configuration to {path}. Run ./retrieve-all-orders.py to export and send.")


def valid_address(value):
    return isinstance(value, str) and "\r" not in value and "\n" not in value and parseaddr(value)[1] == value and "@" in value


def smtp_settings(data, environ=None):
    env = os.environ if environ is None else environ
    host = env.get("SMTP_HOST", data.get("host", "smtp.gmail.com"))
    security = env.get("SMTP_SECURITY", data.get("security", "ssl")).lower()
    if security not in ("ssl", "starttls"):
        raise ExportError("SMTP_SECURITY must be ssl or starttls.")
    try:
        port = int(env.get("SMTP_PORT", data.get("port", 465 if security == "ssl" else 587)))
    except (TypeError, ValueError):
        raise ExportError("SMTP_PORT must be an integer.") from None
    if not 1 <= port <= 65535:
        raise ExportError("SMTP_PORT must be between 1 and 65535.")
    username = env.get("SMTP_USER", data.get("username", ""))
    password = env.get("SMTP_PASSWORD", data.get("password", ""))
    password_file = env.get("SMTP_PASSWORD_FILE", data.get("password_file"))
    if password_file and "SMTP_PASSWORD" not in env:
        password = Path(password_file).expanduser().read_text().rstrip("\r\n")
    sender = env.get("SMTP_FROM", data.get("from", username))
    if not valid_address(sender) or not host or (bool(username) != bool(password)):
        raise ExportError("Email is not configured. Run ./retrieve-all-orders.py --configure-email, "
                          "or set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM.")
    return dict(host=host, port=port, security=security, username=username, password=password, sender=sender)


def resolve_string_id(args):
    value = args.string_id or os.getenv("TIGER_UI_ACCOUNT_IDS")
    if not value:
        listener = ROOT / "start_tiger_listener.sh"
        if listener.exists():
            # Read only the literal ID assignment. Never source the trading launcher.
            text = listener.read_text(encoding="utf-8")
            match = re.search(r'^\s*(?:export\s+)?TIGER_UI_ACCOUNT_IDS="\$\{TIGER_UI_ACCOUNT_IDS:-([A-Za-z0-9_.-]+)\}"', text, re.M)
            if not match:
                match = re.search(r'^\s*(?:export\s+)?TIGER_UI_ACCOUNT_IDS=[\"\']?([A-Za-z0-9_.-]+)[\"\']?\s*(?:#.*)?$', text, re.M)
            if match:
                value = match.group(1)
    value = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ExportError("Cannot identify one account string ID. Pass --string-id TIGER_YOUR_ACCOUNT "
                          "(required when TIGER_UI_ACCOUNT_IDS contains multiple accounts).")
    return value


def make_client(args, string_id):
    try:
        from tigeropen.common.consts import Language
        from tigeropen.common.util.signature_utils import read_private_key
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        from tigeropen.trade.trade_client import TradeClient
    except ImportError:
        raise ExportError("Tiger SDK is missing. Install it with: python3 -m pip install tigeropen==3.6.0") from None
    # Mirror the listener's environment aliases without executing its startup code.
    for source, target in (("TIGER_ID", "TIGEROPEN_TIGER_ID"), ("TIGER_ACCOUNT", "TIGEROPEN_ACCOUNT"),
                           ("TIGER_SECRET_KEY", "TIGEROPEN_SECRET_KEY"), ("TIGER_LICENSE", "TIGEROPEN_LICENSE"),
                           ("TIGER_PRIVATE_KEY", "TIGEROPEN_PRIVATE_KEY"), ("TIGER_TOKEN", "TIGEROPEN_TOKEN")):
        if os.getenv(source):
            os.environ[target] = os.environ[source]
    properties = (args.properties or os.getenv("TIGEROPEN_PROPS_PATH") or os.getenv("TIGER_PROPERTIES_FILE")
                  or os.getenv("TIGER_PROPS_PATH") or str(ROOT / "tiger_openapi_config.properties"))
    properties = Path(properties).expanduser().resolve()
    if args.properties and not properties.exists():
        raise ExportError(f"Tiger properties path does not exist: {properties}")
    os.environ["TIGEROPEN_PROPS_PATH"] = str(properties)  # Explicit --properties wins over SDK env precedence.
    config = TigerOpenClientConfig(props_path=str(properties))
    if os.getenv("TIGER_PRIVATE_KEY_PATH"):
        config.private_key = read_private_key(os.path.expanduser(os.environ["TIGER_PRIVATE_KEY_PATH"]))
    elif not config.private_key and (ROOT / "tiger_private_key.pem").exists():
        config.private_key = read_private_key(str(ROOT / "tiger_private_key.pem"))
    account_map = {}
    for item in os.getenv("TIGER_ACCOUNT_MAP", "").split(","):
        if ":" in item:
            key, value = item.split(":", 1)
            account_map[key.strip()] = value.strip()
    config.account = args.account or account_map.get(string_id) or config.account
    if not config.account or not config.tiger_id or not config.private_key:
        raise ExportError("Missing Tiger account, tiger_id, or private key. Supply --properties PATH "
                          "or the same TIGER_* environment settings as the listener.")
    config.language = Language.en_US
    config.timeout = 30
    return TradeClient(config), str(config.account)


def scalar(value):
    if isinstance(value, Enum):
        return value.name
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ExportError("Unexpected non-scalar order field; export stopped.")


def utc_time(value):
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat(timespec="milliseconds") if value else ""


def order_row(order, account, string_id):
    if str(order.account) != account:
        raise ExportError("Tiger returned an order for a different account; export stopped.")
    row = {"string_id": string_id, "account": account}
    row.update({key: scalar(getattr(order.contract, key, None)) for key in CONTRACT_FIELDS})
    if row["sec_type"] != "STK" or row["market"] not in ("US", None, ""):
        raise ExportError("Tiger returned an order outside the US stock filter; export stopped.")
    row["market"] = "US"  # Market is also enforced by the API query.
    # Whitelist: SDK Order objects also contain secret_key. Never serialize __dict__ / to_dict().
    row.update({key: scalar(getattr(order, key, None)) for key in ORDER_FIELDS})
    for key in ("id", "order_id", "parent_id", "contract_id"):
        row[key] = str(row[key]) if row[key] is not None else ""
    if not row["id"] or not row["order_time"]:
        raise ExportError("An order has no ID or creation timestamp; cannot verify export completeness.")
    for key in TIME_FIELDS:
        row[key + "_utc"] = utc_time(row[key])
    return row


def retrieve_orders(client, account, string_id, as_of_ms, pause=time.sleep):
    rows, pages = {}, []

    def fetch_window(end_time):
        token, tokens = "", set()
        window_count = 0
        for page_number in range(1, 10001):
            response = client.get_orders(account=account, sec_type="STK", market="US", limit=PAGE_SIZE,
                                         is_brief=False, sort_by="LATEST_CREATED", seg_type="SEC",
                                         end_time=end_time, page_token=token)
            if response is None or not hasattr(response, "result"):
                raise ExportError("Tiger did not return a paginated response. Install tigeropen==3.6.0.")
            orders = response.result
            if orders is None:
                raise ExportError("Tiger returned an invalid order page; export stopped.")
            for order in orders:
                row = order_row(order, account, string_id)
                if int(row["order_time"]) >= end_time:
                    raise ExportError("Tiger ignored the requested date boundary; export stopped.")
                rows[row["id"]] = row
            window_count += len(orders)
            token = response.next_page_token
            pages.append({"end_time": end_time, "page": page_number, "received": len(orders)})
            print(f"Page {len(pages)}: {len(orders)} orders; {len(rows)} unique orders total.", flush=True)
            if not token:
                if len(orders) >= PAGE_SIZE:
                    raise ExportError("Tiger returned a full page without a continuation token. "
                                      "Completeness cannot be verified; no partial CSV will be emailed.")
                return window_count
            if token in tokens or not orders:
                raise ExportError("Tiger pagination did not progress; no partial CSV will be emailed.")
            tokens.add(token)
            pause(0.6)
        raise ExportError("Pagination limit reached; no partial CSV will be emailed.")

    # No lower time bound and no status/source filter: include manual and API orders.
    fetch_window(as_of_ms)
    while rows:
        previous_oldest = min(int(row["order_time"]) for row in rows.values())
        pause(0.6)
        if not fetch_window(previous_oldest):
            break
        if min(int(row["order_time"]) for row in rows.values()) >= previous_oldest:
            raise ExportError("Older-history check did not progress; export stopped.")
    return sorted(rows.values(), key=lambda row: (int(row["order_time"]), row["id"])), pages


def save_export(rows, pages, account, string_id, as_of, output_dir):
    import io
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    payload = text.getvalue().encode("utf-8-sig")
    filename = f"{string_id}_US_stock_orders_{as_of:%Y-%m-%dT%H%M%S%fZ}.csv"
    csv_path = output_dir / filename
    atomic_write(csv_path, payload)
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        check = list(csv.DictReader(f))
    if len(check) != len(rows) or len({row["id"] for row in check}) != len(rows):
        raise ExportError("CSV validation failed; email not sent.")
    summary = {
        "string_id": string_id, "account": account, "exported_at_utc": as_of.isoformat(),
        "order_count": len(rows), "earliest_order_utc": rows[0]["order_time_utc"] if rows else None,
        "latest_order_utc": rows[-1]["order_time_utc"] if rows else None,
        "statuses": dict(Counter(row["status"] for row in rows)), "pages": pages,
        "csv_filename": filename, "csv_sha256": hashlib.sha256(payload).hexdigest(),
        "coverage": "All US STK orders returned by the API, with no start date or status filter. "
                    "Pagination exhausted; history since account opening cannot be independently verified.",
        "email_status": "not_sent",
    }
    save_summary(csv_path, summary)
    return csv_path, summary


def save_summary(csv_path, summary):
    atomic_write(csv_path.with_suffix(".json"), (json.dumps(summary, indent=2) + "\n").encode())


def build_message(csv_path, summary, sender, recipient):
    if not valid_address(recipient):
        raise ExportError("--to must be a single valid email address.")
    msg = EmailMessage()
    msg["From"], msg["To"] = sender, recipient
    msg["Subject"] = f"{summary['string_id']} — US stock order history ({summary['order_count']:,} orders)"
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid()
    span = (f"{summary['earliest_order_utc']} to {summary['latest_order_utc']}"
            if summary["order_count"] else "No US stock orders returned.")
    msg.set_content(
        f"Attached: {summary['order_count']:,} US stock orders for {summary['string_id']} "
        f"(Tiger account {summary['account']}).\n\n"
        f"Order dates (UTC): {span}\nExported at: {summary['exported_at_utc']}\n"
        f"Export host: {socket.gethostname()}\n"
        f"Statuses: {json.dumps(summary['statuses'], sort_keys=True)}\n\n"
        f"{summary['coverage']}\n"
        "Includes all returned order sources, including mobile and OpenAPI.\n"
        "Readable timestamp columns use UTC; original timestamps are Unix milliseconds.\n"
        "Import ID columns as Text in spreadsheets to preserve long numeric identifiers.\n"
        "Quantities and quantity_scale/filled_scale fields are preserved as returned by Tiger.\n"
    )
    msg.add_attachment(csv_path.read_bytes(), maintype="text", subtype="csv", filename=csv_path.name)
    return msg


def send_email(message, settings):
    context = ssl.create_default_context()
    connection = None
    try:
        if settings["security"] == "ssl":
            connection = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=60, context=context)
        else:
            connection = smtplib.SMTP(settings["host"], settings["port"], timeout=60)
            connection.ehlo()
            connection.starttls(context=context)
            connection.ehlo()
        if settings["username"]:
            connection.login(settings["username"], settings["password"])
        rejected = connection.send_message(message)
        if rejected:
            raise ExportError("SMTP rejected the recipient; the CSV remains on disk.")
    finally:
        if connection is not None:
            # A failed QUIT after successful DATA must not turn a sent email into a reported failure.
            try:
                connection.quit()
            except Exception:
                connection.close()


def main(argv=None):
    args = parse_args(argv)
    os.umask(0o077)
    logging.disable(logging.CRITICAL)  # SDK request/order logging may expose authentication fields.
    if args.configure_email:
        configure_email(args.email_config)
        return 0
    if not valid_address(args.to):
        raise ExportError("--to must be a single valid email address.")
    settings = None if args.export_only else smtp_settings(load_email_config(args.email_config))
    string_id = resolve_string_id(args)
    client, account = make_client(args, string_id)
    as_of = datetime.now(timezone.utc)
    print(f"Retrieving US stock orders: {string_id}, Tiger account {account}.", flush=True)
    rows, pages = retrieve_orders(client, account, string_id, int(as_of.timestamp() * 1000))
    csv_path, summary = save_export(rows, pages, account, string_id, as_of, args.output_dir.resolve())
    print(f"Saved {len(rows):,} orders to {csv_path}", flush=True)
    if not args.export_only:
        message = build_message(csv_path, summary, settings["sender"], args.to)
        summary.update(email_status="sending", email_to=args.to, message_id=message["Message-ID"])
        save_summary(csv_path, summary)
        try:
            send_email(message, settings)
        except Exception as exc:
            summary["email_status"] = "failed_or_unconfirmed"
            save_summary(csv_path, summary)
            raise ExportError(f"Email failed or delivery is unconfirmed ({type(exc).__name__}). "
                              f"CSV saved at {csv_path}. Check SMTP settings and the recipient inbox "
                              "before rerunning; sending is not retried automatically.") from None
        summary["email_status"] = "accepted_by_smtp"
        save_summary(csv_path, summary)
        print(f"SMTP accepted email to {args.to} (Message-ID: {message['Message-ID']}).")
    return 0


if __name__ == "__main__":
    # Allow ./retrieve-all-orders.py on boxes where Tiger lives only in KTrader/.venv.
    venv_python = ROOT / ".venv/bin/python"
    if (importlib.util.find_spec("tigeropen") is None and venv_python.exists()
            and str(Path(sys.executable).absolute()) != str(venv_python)
            and not os.getenv("KTRADER_ORDER_EXPORT_REEXEC")):
        os.environ["KTRADER_ORDER_EXPORT_REEXEC"] = "1"
        os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])
    try:
        sys.exit(main())
    except ExportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Do not print arbitrary SDK exceptions: they may embed signed request bodies.
        code = getattr(exc, "code", None)
        suffix = f", code={code}" if isinstance(code, int) else ""
        print(f"Error: {type(exc).__name__}{suffix}. Check Tiger/SMTP configuration and connectivity.", file=sys.stderr)
        sys.exit(1)
