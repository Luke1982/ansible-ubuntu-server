"""Validation and normalisation of domain names and e-mail addresses.

The maximum lengths are those of the database columns: 50 characters for a domain, 100 for an address.
"""

import re

from .errors import MailctlError

MAX_DOMAIN_LENGTH = 50
MAX_ADDRESS_LENGTH = 100
MAX_LOCAL_PART_LENGTH = 64  # the limit of the e-mail standard

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
# The last label needs a letter, so IP addresses aren't taken for domains.
_DOMAIN = re.compile(rf"(?:{_LABEL}\.)+(?=[a-z0-9-]*[a-z]){_LABEL}")
# No %: SpamAssassin's settings use a leading % for domains.
_LOCAL_PART = re.compile(r"[a-z0-9_+-]+(?:\.[a-z0-9_+-]+)*")


def valid_domain(name: str) -> bool:
    return len(name) <= MAX_DOMAIN_LENGTH and _DOMAIN.fullmatch(name) is not None


def domain(value: str) -> str:
    name = value.strip().lower()
    if len(name) > MAX_DOMAIN_LENGTH:
        raise MailctlError(f"'{value.strip()}' is too long: a domain can have at most {MAX_DOMAIN_LENGTH} characters.")
    if not valid_domain(name):
        raise MailctlError(f"'{value.strip()}' isn't a valid domain name.", hint="Use a name like example.nl.")
    return name


def address(value: str) -> str:
    candidate = value.strip().lower()
    if len(candidate) > MAX_ADDRESS_LENGTH:
        raise MailctlError(
            f"'{value.strip()}' is too long: an address can have at most {MAX_ADDRESS_LENGTH} characters."
        )
    local_part, _, domain_part = candidate.rpartition("@")
    if len(local_part) > MAX_LOCAL_PART_LENGTH or not _LOCAL_PART.fullmatch(local_part) or not valid_domain(domain_part):
        raise MailctlError(f"'{value.strip()}' isn't a valid e-mail address.", hint="Use an address like info@example.nl.")
    return candidate


def split(address: str) -> tuple[str, str]:
    """Splits a valid address into its local part and domain."""
    local_part, _, domain_part = address.rpartition("@")
    return local_part, domain_part


def is_address(value: str) -> bool:
    return "@" in value
