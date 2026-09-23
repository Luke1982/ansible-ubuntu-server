"""TransIP's REST API (https://api.transip.nl/rest/docs.html): a domain's DNS entries and nameservers, and the
login and key a tool uses for it."""

import base64
import http.client
import json
import os
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from ipaddress import ip_address
from pathlib import Path

import dns.exception
import dns.message
import dns.query
import dns.rdatatype
import dns.resolver

from . import system
from .errors import CtlError

API = "https://api.transip.nl/v6"
TIMEOUT = 30  # seconds
# TransIP answers 409 while it's still saving the previous change to a zone. Reading is tried again; saving isn't,
# since the zone that would be saved was read before that change.
BUSY_RETRIES = 3
BUSY_WAIT = 2  # seconds
_NAMESERVER_DOMAINS = ("transip.net", "transip.nl", "transip.eu")
_PEM = re.compile(r"-----BEGIN ((?:RSA )?PRIVATE KEY)-----(.*?)-----END \1-----", re.DOTALL)

# (method, url, headers, body) -> (HTTP status, response body)
Send = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


@dataclass(frozen=True)
class Entry:
    """A record in TransIP's notation: the name relative to the zone, "@" for the zone itself."""
    name: str
    expire: int
    type: str
    content: str


@dataclass(frozen=True)
class Access:
    """Where a tool keeps the TransIP login and key, and the name it gives itself in messages and API tokens.

    Both tools on a server point at the same two files, so the login is entered once per server.
    """
    settings: Path
    key: Path
    hostname: str
    tool: str = "serverctl"

    @property
    def enter_again(self) -> str:
        return f"Enter the login and key again with: {self.tool} dns credentials"

    @property
    def refused_hint(self) -> str:
        return ("Check the login and the key, and that this server's addresses are on the API whitelist in "
                f"TransIP's control panel. {self.enter_again}")


@dataclass(frozen=True)
class Credentials:
    login: str
    key: Path
    # Tokens that work from any address, for a key made without "only accept IP addresses from the whitelist".
    global_key: bool = False


class NotInAccount(CtlError):
    """The domain isn't in the TransIP account."""


class LoginRefused(CtlError):
    """TransIP refused the login and key."""


def saved_credentials(access: Access) -> Credentials | None:
    """The login and key saved on this server, or None before they're entered."""
    if not access.settings.exists() or not access.key.exists():
        return None
    try:
        settings = json.loads(access.settings.read_text())
    except (OSError, ValueError) as error:
        raise CtlError(f"Can't read {access.settings}: {error}.", hint=access.enter_again) from None
    login, global_key = (settings.get(name) if isinstance(settings, dict) else None for name in ("login", "global_key"))
    if not isinstance(login, str) or not isinstance(global_key, bool):
        raise CtlError(f"{access.settings} isn't what was saved there.", hint=access.enter_again)
    return Credentials(login, access.key, global_key)


def save_credentials(access: Access, login: str, key: str, send: Send | None = None) -> Credentials:
    """Saves the login and key once TransIP accepts them. Tokens that only work from the addresses on the API
    whitelist are tried first, as in TransIP's own library; a key made without that requirement needs tokens that
    work from anywhere, so those are tried next."""
    directory = access.key.parent
    directory.mkdir(parents=True, exist_ok=True)
    candidate = _write_private(directory, normalise_key(key))
    try:
        system.run("openssl", "pkey", "-in", str(candidate), "-noout")
        accepted = _accepted(Credentials(login, candidate), access, send)
        os.replace(candidate, access.key)
    finally:
        candidate.unlink(missing_ok=True)
    settings = _write_private(directory, json.dumps({"login": login, "global_key": accepted.global_key}) + "\n")
    access.settings.parent.mkdir(parents=True, exist_ok=True)
    os.replace(settings, access.settings)
    return replace(accepted, key=access.key)


def normalise_key(text: str) -> str:
    """The private key the way openssl reads it, also when line breaks got lost or added in copying it."""
    found = _PEM.search(text)
    if not found or "ENCRYPTED" in found.group(2):
        raise CtlError("That isn't a private key without a passphrase, like TransIP's control panel makes.",
                           hint="It starts with -----BEGIN PRIVATE KEY-----.")
    kind, body = found.group(1), "".join(found.group(2).split())
    lines = [body[start:start + 64] for start in range(0, len(body), 64)]
    return "\n".join([f"-----BEGIN {kind}-----", *lines, f"-----END {kind}-----"]) + "\n"


def _accepted(credentials: Credentials, access: Access, send: Send | None) -> Credentials:
    first = None
    for candidate in (credentials, replace(credentials, global_key=True)):
        try:
            Client(candidate, access=access, read_only=True, send=send).check()
            return candidate
        except LoginRefused as refused:
            first = first or refused
    raise first


def _write_private(directory: Path, content: str) -> Path:
    """A new file only root can read, to rename into place."""
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".transip-")
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
    return Path(name)


class Client:
    def __init__(self, credentials: Credentials, *, access: Access, read_only: bool, send: Send | None = None) -> None:
        self._credentials = credentials
        self._access = access
        self._read_only = read_only
        self._send = send or _send
        self._token: str | None = None

    @property
    def login(self) -> str:
        return self._credentials.login

    def check(self) -> None:
        """Logs in and makes a request: TransIP may give out a token that only works from whitelisted addresses,
        and refuse it when it's used."""
        self._request("GET", "/api-test")

    def dns_entries(self, domain: str) -> list[Entry]:
        response = self._request("GET", f"/domains/{domain}/dns", domain=domain)
        try:
            entries = [
                Entry(entry["name"], int(entry["expire"]), entry["type"], entry["content"])
                for entry in response["dnsEntries"]
            ]
        except (KeyError, TypeError, ValueError):
            raise _not_understood() from None
        # The whole zone is sent back when it changes, so each entry must be read exactly.
        if not all(isinstance(value, str) for entry in entries for value in (entry.name, entry.type, entry.content)):
            raise _not_understood()
        return entries

    def replace_dns_entries(self, domain: str, entries: tuple[Entry, ...]) -> None:
        """Replaces the whole zone in one request, so a change is never half made."""
        body = {"dnsEntries": [
            {"name": entry.name, "expire": entry.expire, "type": entry.type, "content": entry.content}
            for entry in entries
        ]}
        self._request("PUT", f"/domains/{domain}/dns", body, domain=domain)

    def nameservers(self, domain: str) -> list[str]:
        response = self._request("GET", f"/domains/{domain}/nameservers", domain=domain)
        try:
            return [str(nameserver["hostname"]) for nameserver in response["nameservers"]]
        except (KeyError, TypeError):
            raise _not_understood() from None

    def _request(self, method: str, path: str, body: dict | None = None, *, domain: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        for attempt in range(BUSY_RETRIES + 1):
            status, response = self._call(method, path, headers, body)
            if status != 409 or method != "GET" or attempt == BUSY_RETRIES:
                break
            time.sleep(BUSY_WAIT)
        if status in (401, 403):
            raise LoginRefused(f"TransIP refused a request from {self.login}: {_error(response, status)}",
                               hint=self._access.refused_hint)
        if status == 404 and domain:
            raise NotInAccount(
                f"TransIP: {_error(response, status)}",
                hint=f"{domain} isn't in the TransIP account {self.login}, so its records have to be published "
                     f"wherever its DNS is managed.",
            )
        if status >= 400:
            raise _failure(status, response)
        return _json(response) if response.strip() else {}

    def _access_token(self) -> str:
        """A token for this command, asked for on first use and signed with the private key."""
        if self._token is None:
            nonce = secrets.token_hex(16)
            body = {
                "login": self.login,
                "nonce": nonce,
                "read_only": self._read_only,
                "expiration_time": "30 minutes",
                "label": f"{self._access.tool} on {self._access.hostname} {nonce[:8]}",
                "global_key": self._credentials.global_key,
            }
            # The signature covers the exact bytes sent.
            payload = json.dumps(body).encode()
            key = str(self._credentials.key)
            signature = system.run_binary("openssl", "dgst", "-sha512", "-sign", key, stdin=payload)
            status, response = self._call("POST", "/auth", {"Signature": base64.b64encode(signature).decode()}, payload)
            if status in (400, 401, 403):
                raise LoginRefused(f"TransIP refused to log in as {self.login}: {_error(response, status)}",
                                   hint=self._access.refused_hint)
            if status >= 400:
                raise _failure(status, response)
            token = _json(response).get("token")
            if not isinstance(token, str):
                raise CtlError("TransIP's answer to the login has no token.")
            self._token = token
        return self._token

    def _call(self, method: str, path: str, headers: dict[str, str], body: dict | bytes | None) -> tuple[int, bytes]:
        payload = json.dumps(body).encode() if isinstance(body, dict) else body
        headers = {**headers, "Content-Type": "application/json", "Accept": "application/json"}
        return self._send(method, API + path, headers, payload)


def nameserver_addresses(zone: str, name: str, timeout: float = 5.0) -> list[set]:
    """The A and AAAA records each of the zone's nameservers gives for the name. They're asked directly, so no cache
    answers with what it saw before the record was published."""
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    answers = []
    try:
        for nameserver in resolver.resolve(zone, "NS"):
            address = resolver.resolve(nameserver.target, "A")[0].address
            found = set()
            for kind in ("A", "AAAA"):
                response = dns.query.udp(dns.message.make_query(name, kind), address, timeout=timeout)
                found |= {ip_address(item.address) for rrset in response.answer
                          if rrset.rdtype in (dns.rdatatype.A, dns.rdatatype.AAAA) for item in rrset}
            answers.append(found)
    except (dns.exception.DNSException, OSError) as error:
        raise CtlError(f"Couldn't ask the nameservers of {zone} about {name}: {error}") from None
    return answers


def uses_transip_nameservers(nameservers: list[str]) -> bool:
    """Whether the zone at TransIP is the one the internet sees."""
    return bool(nameservers) and all(
        name.lower().rstrip(".").endswith(tuple(f".{domain}" for domain in _NAMESERVER_DOMAINS)) for name in nameservers
    )


def _send(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason = getattr(error, "reason", error)
        raise CtlError(f"Can't reach TransIP's API: {reason}.") from None


def _json(response: bytes) -> dict:
    try:
        data = json.loads(response)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise _not_understood()
    return data


def _not_understood() -> CtlError:
    return CtlError("TransIP's API gave an answer that isn't understood.")


def _error(response: bytes, status: int) -> str:
    """TransIP's explanation of a failed request."""
    try:
        message = json.loads(response).get("error")
    except (ValueError, AttributeError):
        message = None
    return str(message) if message else f"HTTP status {status}"


def _failure(status: int, response: bytes) -> CtlError:
    if status == 429:
        return CtlError("TransIP's rate limit is reached.", hint="Try again in 15 minutes.")
    if status == 409:
        return CtlError(f"TransIP: {_error(response, status)}",
                            hint="TransIP is still saving an earlier change. Nothing was changed; try again in a minute.")
    return CtlError(f"TransIP: {_error(response, status)}")
