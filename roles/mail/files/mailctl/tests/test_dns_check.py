from ipaddress import ip_address

import dns.name
import pytest

from mailctl.core import dns_check
from mailctl.core.dns_check import DnsRecord, LookupFailed, Srv, Status

SERVER_IPS = {ip_address("203.0.113.5")}
DKIM_VALUE = "v=DKIM1; h=sha256; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOC"


class FakeResolver:
    def __init__(self, txt, mx, addresses, srv, ptr, failing=()):
        self._records = {"txt": txt, "mx": mx, "addresses": addresses, "srv": srv, "ptr": ptr}
        self._failing = failing

    def txt(self, name):
        return self._lookup("txt", name)

    def mx(self, name):
        return self._lookup("mx", name)

    def addresses(self, name):
        return {ip_address(address) for address in self._lookup("addresses", name)}

    def srv(self, name):
        return [Srv(*(int(part) for part in value.split()[:3]), value.split()[3]) for value in self._lookup("srv", name)]

    def ptr(self, address):
        return self._lookup("ptr", str(address))

    def _lookup(self, kind, name):
        if name in self._failing:
            raise LookupFailed(f"The lookup for {name} timed out.")
        return self._records[kind].get(name, [])


def resolver(failing=(), **overrides):
    """A resolver for a correctly set up example.nl on server.hosting.example; keyword arguments replace records
    per kind."""
    records = {
        "txt": {
            "example.nl": ["v=spf1 mx ~all"],
            "mail._domainkey.example.nl": [DKIM_VALUE],
            "_dmarc.example.nl": ["v=DMARC1; p=quarantine"],
        },
        "mx": {"example.nl": ["mail.example.nl"]},
        "addresses": {"mail.example.nl": ["203.0.113.5"], "server.hosting.example": ["203.0.113.5"]},
        "srv": {
            "_imaps._tcp.example.nl": ["0 1 993 mail.example.nl"],
            "_imap._tcp.example.nl": ["10 1 143 mail.example.nl"],
            "_submissions._tcp.example.nl": ["0 1 465 mail.example.nl"],
            "_submission._tcp.example.nl": ["10 1 587 mail.example.nl"],
        },
        "ptr": {"203.0.113.5": ["server.hosting.example"]},
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

    assert [result.name for result in checks] == ["MX", "SPF", "DKIM", "DMARC", "SRV"]
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
        DnsRecord("SRV", "_imaps._tcp.example.nl", "0 1 993 mail.example.nl"),
        DnsRecord("SRV", "_imap._tcp.example.nl", "10 1 143 mail.example.nl"),
        DnsRecord("SRV", "_submissions._tcp.example.nl", "0 1 465 mail.example.nl"),
        DnsRecord("SRV", "_submission._tcp.example.nl", "10 1 587 mail.example.nl"),
    ]


def test_the_mail_host_of_a_subdomain_is_in_the_subdomain():
    records = dns_check.recommended_records("shop.example.nl", SERVER_IPS, None)

    assert records[:2] == [
        DnsRecord("MX", "shop.example.nl", "10 mail.shop.example.nl"),
        DnsRecord("A", "mail.shop.example.nl", "203.0.113.5"),
    ]


def test_recommended_records_leave_out_the_address_records_when_the_servers_addresses_are_unknown():
    records = dns_check.recommended_records("example.nl", set(), None)

    assert [record.type for record in records] == ["MX", "TXT", "TXT", "SRV", "SRV", "SRV", "SRV"]


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


def test_the_system_resolver_reads_srv_records_and_the_target_for_no_service(monkeypatch):
    class Offered:
        priority, weight, port, target = 0, 1, 993, dns.name.from_text("mail.example.nl.")

    class NotOffered:
        priority, weight, port, target = 0, 0, 0, dns.name.root

    monkeypatch.setattr(dns_check.SystemResolver, "_query", lambda self, name, kind: [Offered(), NotOffered()])

    assert dns_check.SystemResolver().srv("_imaps._tcp.example.nl") == [
        Srv(0, 1, 993, "mail.example.nl"), Srv(0, 0, 0, "."),
    ]


def test_the_system_resolver_looks_up_reverse_dns_in_the_reverse_zone(monkeypatch):
    class Answer:
        target = dns.name.from_text("server.hosting.example.")

    queries = []
    monkeypatch.setattr(dns_check.SystemResolver, "_query", lambda self, name, kind: queries.append((name, kind)) or [Answer()])

    assert dns_check.SystemResolver().ptr(ip_address("203.0.113.5")) == ["server.hosting.example"]
    assert queries == [("5.113.0.203.in-addr.arpa.", "PTR")]


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


def test_srv_records_missing_for_some_services_are_a_warning_with_those_records():
    result = check(resolver(srv={"_imap._tcp.example.nl": [], "_submission._tcp.example.nl": []}), "SRV")

    assert result.status is Status.WARN
    assert "_imap._tcp.example.nl, _submission._tcp.example.nl" in result.detail
    assert [record.name for record in result.fixes] == ["_imap._tcp.example.nl", "_submission._tcp.example.nl"]


def test_no_srv_records_at_all_is_a_warning():
    services = ("_imaps", "_imap", "_submissions", "_submission")
    result = check(resolver(srv={f"{service}._tcp.example.nl": [] for service in services}), "SRV")

    assert result.status is Status.WARN
    assert "There are no SRV records" in result.detail
    assert len(result.fixes) == 4


@pytest.mark.parametrize("published", [["0 1 993 imap.provider.nl"], ["0 1 143 mail.example.nl"], ["0 0 0 ."]])
def test_srv_records_pointing_elsewhere_are_a_problem(published):
    result = check(resolver(srv={"_imaps._tcp.example.nl": published}), "SRV")

    assert result.status is Status.FAIL
    assert result.detail.startswith("_imaps._tcp.example.nl sends mail programs to ")
    assert "instead of mail.example.nl port 993" in result.detail
    assert result.fixes == (DnsRecord("SRV", "_imaps._tcp.example.nl", "0 1 993 mail.example.nl"),)


def test_srv_records_with_other_priorities_and_capitals_pass():
    dns = resolver(srv={"_imaps._tcp.example.nl": ["5 0 993 Mail.Example.NL", "20 0 993 backup.provider.nl"]})

    assert check(dns, "SRV").status is Status.OK


def server_checks(dns, server_ips=SERVER_IPS):
    return {result.name: result for result in dns_check.check_server("Server.Hosting.Example", server_ips=server_ips,
                                                                      resolver=dns)}


def test_a_server_whose_name_and_addresses_point_to_each_other_passes():
    checks = server_checks(resolver())

    assert [(name, result.status) for name, result in checks.items()] == [
        ("Hostname", Status.OK), ("Reverse DNS", Status.OK),
    ]


def test_a_hostname_missing_an_address_of_the_server_fails_with_the_records_to_publish():
    checks = server_checks(resolver(), server_ips={ip_address("203.0.113.5"), ip_address("2001:db8::5")})

    assert checks["Hostname"].status is Status.FAIL
    assert "2001:db8::5" in checks["Hostname"].detail
    assert checks["Hostname"].fixes == (DnsRecord("AAAA", "server.hosting.example", "2001:db8::5"),)


def test_reverse_dns_that_doesnt_point_to_the_hostname_fails():
    checks = server_checks(resolver(ptr={"203.0.113.5": ["5.113.0.203.provider.net"]}))

    assert checks["Reverse DNS"].status is Status.FAIL
    assert checks["Reverse DNS"].detail.startswith("The reverse DNS of 203.0.113.5 is 5.113.0.203.provider.net. ")
    assert "isn't server.hosting.example" in checks["Reverse DNS"].detail


def test_missing_reverse_dns_fails_for_every_address_without_it():
    server_ips = {ip_address("203.0.113.5"), ip_address("2001:db8::5")}
    checks = server_checks(resolver(ptr={"203.0.113.5": []}), server_ips=server_ips)

    assert checks["Reverse DNS"].status is Status.FAIL
    assert checks["Reverse DNS"].detail.startswith("203.0.113.5 has no reverse DNS; 2001:db8::5 has no reverse DNS. ")


def test_the_server_checks_leave_out_private_addresses():
    checks = server_checks(resolver(addresses={"server.hosting.example": ["93.184.216.34"]},
                                    ptr={"93.184.216.34": ["server.hosting.example"]}),
                           server_ips={ip_address("93.184.216.34"), ip_address("10.0.0.5")})

    assert all(result.status is Status.OK for result in checks.values())


def test_a_failed_server_lookup_is_a_warning():
    checks = server_checks(resolver(failing=("203.0.113.5",)))

    assert checks["Reverse DNS"].status is Status.WARN
