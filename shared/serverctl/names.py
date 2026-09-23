"""Validation and normalisation of domain names, e-mail addresses and Linux user names.

The default maximum lengths are those of mailctl's database columns: 50 characters for a domain, 100 for an
address. A tool without that limit passes its own, up to DNS's own maximum of 253 characters for a name.
"""

import re

from .errors import CtlError

MAX_DOMAIN_LENGTH = 50
MAX_DNS_NAME_LENGTH = 253  # the limit of the DNS standard
MAX_USER_NAME_LENGTH = 32  # the limit of Linux's user database
MAX_ADDRESS_LENGTH = 100
MAX_LOCAL_PART_LENGTH = 64  # the limit of the e-mail standard

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
# The last label needs a letter, so IP addresses aren't taken for domains.
_DOMAIN = re.compile(rf"(?:{_LABEL}\.)+(?=[a-z0-9-]*[a-z]){_LABEL}")
# No %: SpamAssassin's settings use a leading % for domains.
_LOCAL_PART = re.compile(r"[a-z0-9_+-]+(?:\.[a-z0-9_+-]+)*")
# What adduser accepts: a letter or digit first, then letters, digits, hyphens and underscores.
_USER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


def valid_domain(name: str, max_length: int = MAX_DOMAIN_LENGTH) -> bool:
    return len(name) <= max_length and _DOMAIN.fullmatch(name) is not None


def domain(value: str, max_length: int = MAX_DOMAIN_LENGTH) -> str:
    name = value.strip().lower()
    if len(name) > max_length:
        raise CtlError(f"'{value.strip()}' is too long: a domain can have at most {max_length} characters.")
    if not valid_domain(name, max_length):
        raise CtlError(f"'{value.strip()}' isn't a valid domain name.", hint="Use a name like example.nl.")
    return name


def address(value: str) -> str:
    candidate = value.strip().lower()
    if len(candidate) > MAX_ADDRESS_LENGTH:
        raise CtlError(
            f"'{value.strip()}' is too long: an address can have at most {MAX_ADDRESS_LENGTH} characters."
        )
    local_part, _, domain_part = candidate.rpartition("@")
    if len(local_part) > MAX_LOCAL_PART_LENGTH or not _LOCAL_PART.fullmatch(local_part) or not valid_domain(domain_part):
        raise CtlError(f"'{value.strip()}' isn't a valid e-mail address.", hint="Use an address like info@example.nl.")
    return candidate


def split(address: str) -> tuple[str, str]:
    """Splits a valid address into its local part and domain."""
    local_part, _, domain_part = address.rpartition("@")
    return local_part, domain_part


def is_address(value: str) -> bool:
    return "@" in value


def valid_user_name(name: str) -> bool:
    return len(name) <= MAX_USER_NAME_LENGTH and _USER_NAME.fullmatch(name) is not None


def user_name(value: str) -> str:
    """A Linux user name, lower case. Raises CtlError when it isn't one."""
    name = value.strip().lower()
    if not valid_user_name(name):
        raise CtlError(
            f"'{value.strip()}' isn't a valid user name.",
            hint=f"Use letters, digits, - and _, starting with a letter or digit, "
                 f"at most {MAX_USER_NAME_LENGTH} characters.",
        )
    return name


def user_name_for(domain_name: str) -> str:
    """The user name a domain gets by default: its labels without the public suffix, joined, and cut to length.

    example.nl becomes "example", shop.example.co.uk becomes "shopexample". It is only a suggestion; the caller
    checks whether the name is free and asks for another when it isn't.
    """
    labels = domain_name.split(".")
    # The last label is always a suffix; a two-letter one before it usually is too (co.uk, com.au).
    keep = labels[:-1]
    if len(keep) > 1 and len(keep[-1]) <= 2:
        keep = keep[:-1]
    name = "".join(re.sub(r"[^a-z0-9]", "", label) for label in keep)
    return name[:MAX_USER_NAME_LENGTH].lstrip("-_") or "site"
