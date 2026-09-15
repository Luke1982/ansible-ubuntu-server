from ipaddress import ip_address

import pytest

from mailctl.core import dns_check
from mailctl.core.dns_check import DnsRecord, LookupFailed, Status

SERVER_IPS = {ip_address("203.0.113.5")}
DKIM_VALUE = "v=DKIM1; h=sha256; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOC"


class FakeResolver:
    def __init__(self, txt, mx, addresses, failing=()):
        self._records = {"txt": txt, "mx": mx, "addresses": addresses}
        self._failing = failing

    def txt(self, name):
        return self._lookup("txt", name)

    def mx(self, name):
        return self._lookup("mx", name)

    def addresses(self, name):
        return {ip_address(address) for address in self._lookup("addresses", name)}

    def _lookup(self, kind, name):
        if name in self._failing:
            raise LookupFailed(f"The lookup for {name} timed out.")
        return self._records[kind].get(name, [])


def resolver(failing=(), **overrides):
    """A resolver for a correctly set up example.nl; keyword arguments replace records per kind."""
    records = {
        "txt": {
            "example.nl": ["v=spf1 mx ~all"],
            "mail._domainkey.example.nl": [DKIM_VALUE],
            "_dmarc.example.nl": ["v=DMARC1; p=quarantine"],
        },
        "mx": {"example.nl": ["mail.example.nl"]},
        "addresses": {"mail.example.nl": ["203.0.113.5"]},
    }
    for kind, changes in overrides.items():
        records[kind] = {**records[kind], **changes}
    return FakeResolver(**records, failing=failing)


MAIL_HOST_RECORDS = (DnsRecord("MX", "example.nl", "10 mail.example.nl"), DnsRecord("A", "mail.example.nl", "203.0.113.5"))


def check(dns, name, *, server_ips=SERVER_IPS, dkim_value=DKIM_VALUE):
    checks = dns_check.check_domain("example.nl", server_ips=server_ips, dkim_value=dkim_value, resolver=dns)
    return next(result for result in checks if result.name == name)


def test_a_correctly_set_up_domain_passes_every_check():
    checks = dns_check.check_domain("example.nl", server_ips=SERVER_IPS, dkim_value=DKIM_VALUE, resolver=resolver())

    assert [result.name for result in checks] == ["MX", "SPF", "DKIM", "DMARC"]
    assert all(result.status is Status.OK and result.fixes == () for result in checks)


def test_recommended_records_deliver_mail_to_the_mail_host_at_the_servers_public_addresses():
    server_ips = {ip_address("93.184.216.34"), ip_address("2606:2800:220:1::5"), ip_address("10.0.0.5")}

    assert dns_check.recommended_records("example.nl", server_ips, DKIM_VALUE) == [
        DnsRecord("MX", "example.nl", "10 mail.example.nl"),
        DnsRecord("A", "mail.example.nl", "93.184.216.34"),
        DnsRecord("AAAA", "mail.example.nl", "2606:2800:220:1::5"),
        DnsRecord("TXT", "example.nl", "v=spf1 mx ~all"),
        DnsRecord("TXT", "mail._domainkey.example.nl", DKIM_VALUE),
        DnsRecord("TXT", "_dmarc.example.nl", "v=DMARC1; p=quarantine"),
    ]


def test_the_mail_host_of_a_subdomain_is_in_the_subdomain():
    records = dns_check.recommended_records("shop.example.nl", SERVER_IPS, None)

    assert records[:2] == [
        DnsRecord("MX", "shop.example.nl", "10 mail.shop.example.nl"),
        DnsRecord("A", "mail.shop.example.nl", "203.0.113.5"),
    ]


def test_recommended_records_leave_out_the_address_records_when_the_servers_addresses_are_unknown():
    records = dns_check.recommended_records("example.nl", set(), None)

    assert [record.type for record in records] == ["MX", "TXT", "TXT"]


def test_missing_mx_fails_with_the_records_to_publish():
    result = check(resolver(mx={"example.nl": []}), "MX")

    assert result.status is Status.FAIL
    assert result.fixes == MAIL_HOST_RECORDS


def test_mx_pointing_elsewhere_fails_with_the_records_to_publish():
    dns = resolver(mx={"example.nl": ["mx.provider.nl"]}, addresses={"mx.provider.nl": ["198.51.100.7"]})

    result = check(dns, "MX")

    assert result.status is Status.FAIL
    assert "mx.provider.nl" in result.detail
    assert result.fixes == MAIL_HOST_RECORDS


def test_mx_reaching_this_server_under_another_name_is_a_warning():
    dns = resolver(mx={"example.nl": ["server.hosting.example"]}, addresses={"server.hosting.example": ["203.0.113.5"]})

    result = check(dns, "MX")

    assert result.status is Status.WARN
    assert "server.hosting.example" in result.detail
    assert "mail.example.nl" in result.detail
    assert result.fixes == MAIL_HOST_RECORDS


def test_mx_to_the_mail_host_fails_when_the_mail_host_isnt_this_server():
    result = check(resolver(addresses={"mail.example.nl": ["198.51.100.7"]}), "MX")

    assert result.status is Status.FAIL
    assert result.fixes == (DnsRecord("A", "mail.example.nl", "203.0.113.5"),)


def test_mx_accepts_the_mail_host_written_with_capitals():
    dns = resolver(mx={"example.nl": ["Mail.Example.NL"]}, addresses={"Mail.Example.NL": ["203.0.113.5"]})

    assert check(dns, "MX").status is Status.OK


def test_mx_passes_when_a_backup_mx_is_this_server():
    dns = resolver(mx={"example.nl": ["mx.provider.nl", "mail.example.nl"]}, addresses={"mx.provider.nl": ["198.51.100.7"]})

    assert check(dns, "MX").status is Status.OK


@pytest.mark.parametrize(
    "record, status",
    [
        ("v=spf1 ip4:203.0.113.0/24 -all", Status.OK),
        ("v=spf1 ip4:198.51.100.1 -all", Status.FAIL),
        ("v=spf1 -ip4:203.0.113.5 +all", Status.FAIL),
        ("v=spf1 ~ip4:203.0.113.5 all", Status.FAIL),
        ("v=spf1 a -all", Status.OK),
        ("v=spf1 a:relay.example.nl/24 -all", Status.OK),
        ("v=spf1 a:relay.example.nl -all", Status.FAIL),
        ("v=spf1 mx -all", Status.OK),
        ("v=spf1 mx:other.nl -all", Status.FAIL),
        ("v=spf1 include:_spf.provider.nl -all", Status.OK),
        ("v=spf1 include:_spf.elsewhere.nl -all", Status.FAIL),
        ("v=spf1 redirect=_spf.provider.nl", Status.OK),
        ("v=spf1 exp=%{d}.explain.example.nl ip4:203.0.113.5 -all", Status.OK),
        ("V=SPF1 IP4:203.0.113.5 -ALL", Status.OK),
        ("v=spf1 ip4:203.0.113.5 ptr -all", Status.OK),
    ],
)
def test_spf_decides_whether_this_server_may_send(record, status):
    dns = resolver(
        txt={
            "example.nl": [record],
            "_spf.provider.nl": ["v=spf1 ip4:203.0.113.5 -all"],
            "_spf.elsewhere.nl": ["v=spf1 ip4:198.51.100.0/24 -all"],
        },
        addresses={"example.nl": ["203.0.113.5"], "relay.example.nl": ["203.0.113.9"]},
    )

    assert check(dns, "SPF").status is status


# Each record ends in +all, so the check only fails or warns when the evaluation really stops at the reason given.
@pytest.mark.parametrize(
    "record, status, reason",
    [
        ("v=spf1 ip:203.0.113.5 +all", Status.FAIL, "contains 'ip:203.0.113.5', which isn't valid SPF"),
        ("v=spf1 ip4:203.0.113.500 +all", Status.FAIL, "invalid address: 203.0.113.500"),
        ("v=spf1 include:missing.nl +all", Status.FAIL, "refers to missing.nl, which has no SPF record"),
        ("v=spf1 ptr +all", Status.WARN, "uses 'ptr'"),
        ("v=spf1 exists:spf.example.nl +all", Status.WARN, "uses 'exists'"),
        ("v=spf1 exists:%{i}.spf.example.nl +all", Status.WARN, "uses macros"),
        ("v=spf1 redirect=%{d}._spf.example.nl", Status.WARN, "uses macros"),
    ],
)
def test_spf_that_cant_be_evaluated_explains_why(record, status, reason):
    result = check(resolver(txt={"example.nl": [record]}), "SPF")

    assert result.status is status
    assert reason in result.detail


def test_spf_checks_private_addresses_when_the_server_has_no_public_one():
    dns = resolver(txt={"example.nl": ["v=spf1 ip4:10.0.0.5 -all"]})

    assert check(dns, "SPF", server_ips={ip_address("10.0.0.5")}).status is Status.OK


def test_the_system_resolver_joins_the_strings_of_a_long_txt_record(monkeypatch):
    class Answer:
        strings = (b"v=DKIM1; k=rsa; ", b"p=MIIBIjANBgkqh")

    monkeypatch.setattr(dns_check.SystemResolver, "_query", lambda self, name, kind: [Answer()])

    assert dns_check.SystemResolver().txt("mail._domainkey.example.nl") == ["v=DKIM1; k=rsa; p=MIIBIjANBgkqh"]


@pytest.mark.parametrize("records", [[], ["v=spf1 mx -all", "v=spf1 a -all"]])
def test_spf_needs_exactly_one_record(records):
    result = check(resolver(txt={"example.nl": ["google-site-verification=abc", *records]}), "SPF")

    assert result.status is Status.FAIL
    assert result.fixes == (DnsRecord("TXT", "example.nl", "v=spf1 mx ~all"),)


def test_spf_ignores_other_txt_records():
    dns = resolver(txt={"example.nl": ["google-site-verification=abc", "v=spf1 mx ~all"]})

    assert check(dns, "SPF").status is Status.OK


def test_spf_stops_after_ten_dns_lookups():
    chain = {f"{n}.chain.nl": [f"v=spf1 include:{n + 1}.chain.nl -all"] for n in range(12)}
    dns = resolver(txt={"example.nl": ["v=spf1 include:0.chain.nl -all"], **chain})

    result = check(dns, "SPF")

    assert result.status is Status.FAIL
    assert "10 DNS lookups" in result.detail


def test_spf_must_allow_every_address_of_the_server():
    dns = resolver(txt={"example.nl": ["v=spf1 ip4:203.0.113.5 -all"]})

    result = check(dns, "SPF", server_ips={ip_address("203.0.113.5"), ip_address("2001:db8::5")})

    assert result.status is Status.FAIL
    assert "2001:db8::5" in result.detail


def test_spf_ignores_private_addresses_of_the_server():
    dns = resolver(txt={"example.nl": ["v=spf1 ip4:93.184.216.34 -all"]})

    result = check(dns, "SPF", server_ips={ip_address("93.184.216.34"), ip_address("10.0.0.5")})

    assert result.status is Status.OK


def test_dkim_matches_a_published_key_that_contains_spaces():
    dns = resolver(txt={"mail._domainkey.example.nl": ["v=DKIM1; k=rsa; p=MIIBIjANBgkqh kiG9w0BAQEFAAOC"]})

    assert check(dns, "DKIM").status is Status.OK


def test_dkim_with_a_different_published_key_fails_with_the_record_to_publish():
    result = check(resolver(txt={"mail._domainkey.example.nl": ["v=DKIM1; k=rsa; p=OTHERKEY"]}), "DKIM")

    assert result.status is Status.FAIL
    assert result.fixes == (DnsRecord("TXT", "mail._domainkey.example.nl", DKIM_VALUE),)


def test_dkim_without_a_published_record_fails():
    result = check(resolver(txt={"mail._domainkey.example.nl": []}), "DKIM")

    assert result.status is Status.FAIL
    assert result.fixes == (DnsRecord("TXT", "mail._domainkey.example.nl", DKIM_VALUE),)


def test_dkim_without_a_key_on_this_server_fails_with_the_command_to_create_one():
    result = check(resolver(), "DKIM", dkim_value=None)

    assert result.status is Status.FAIL
    assert "mailctl dkim create example.nl" in result.detail
    assert result.fixes == ()


def test_dmarc_shows_the_policy():
    assert check(resolver(), "DMARC").detail == "Policy: quarantine."


@pytest.mark.parametrize("records", [[], ["v=DMARC1x; p=reject"], ["p=reject; v=DMARC1"]])
def test_missing_dmarc_is_a_warning_with_the_record_to_publish(records):
    result = check(resolver(txt={"_dmarc.example.nl": records}), "DMARC")

    assert result.status is Status.WARN
    assert result.fixes == (DnsRecord("TXT", "_dmarc.example.nl", "v=DMARC1; p=quarantine"),)


def test_two_dmarc_records_are_a_problem():
    result = check(resolver(txt={"_dmarc.example.nl": ["v=DMARC1; p=reject", "v=DMARC1; p=none"]}), "DMARC")

    assert result.status is Status.FAIL


def test_a_subdomain_without_dmarc_follows_its_parent_domain():
    checks = dns_check.check_domain("shop.example.nl", server_ips=SERVER_IPS, dkim_value=None, resolver=resolver())

    dmarc = next(result for result in checks if result.name == "DMARC")
    assert (dmarc.status, dmarc.detail) == (Status.OK, "Policy: quarantine, set for example.nl.")


def test_a_failed_lookup_is_a_warning():
    result = check(resolver(failing=("_dmarc.example.nl",)), "DMARC")

    assert result.status is Status.WARN
    assert "timed out" in result.detail
