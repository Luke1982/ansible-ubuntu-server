from ipaddress import ip_address

import pytest

from mailctl.core import dns_check, zone
from mailctl.core.zone import Entry

IPV4, IPV6 = "93.184.216.34", "2606:2800:220:1::5"
DKIM_VALUE = "v=DKIM1; h=sha256; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOC"
RECORDS = dns_check.recommended_records("example.nl", {ip_address(IPV4), ip_address(IPV6)}, DKIM_VALUE)

# Everything mailctl publishes, as TransIP would hold it. Only the IPv4 address: see _host_records.
MAIL_ENTRIES = [
    Entry("@", 3600, "MX", "10 mail.example.nl."),
    Entry("mail", 3600, "A", IPV4),
    Entry("@", 3600, "TXT", "v=spf1 mx ~all"),
    Entry("mail._domainkey", 3600, "TXT", DKIM_VALUE),
    Entry("_dmarc", 3600, "TXT", "v=DMARC1; p=quarantine"),
    Entry("_imaps._tcp", 3600, "SRV", "0 1 993 mail.example.nl."),
    Entry("_imap._tcp", 3600, "SRV", "10 1 143 mail.example.nl."),
    Entry("_submissions._tcp", 3600, "SRV", "0 1 465 mail.example.nl."),
    Entry("_submission._tcp", 3600, "SRV", "10 1 587 mail.example.nl."),
    Entry("webmail", 3600, "A", IPV4),
    Entry("_caldavs._tcp", 3600, "SRV", "0 1 443 webmail.example.nl."),
    Entry("_carddavs._tcp", 3600, "SRV", "0 1 443 webmail.example.nl."),
]
WEBSITE = [Entry("@", 300, "A", "198.51.100.80"), Entry("www", 300, "CNAME", "@")]


SERVER_IPS = {ip_address(IPV4), ip_address(IPV6)}


def plan(*entries):
    # The server has both addresses and sends mail from both, while only the IPv4 one is published.
    return zone.plan("example.nl", list(entries), RECORDS, sender_ips=SERVER_IPS)


def without(entries, *types_and_names):
    return [entry for entry in entries if (entry.type, entry.name) not in types_and_names]


def test_an_empty_zone_gets_every_record():
    result = plan()

    assert result.remove == ()
    assert list(result.add) == MAIL_ENTRIES
    assert result.unchanged == 0


def test_a_zone_that_has_every_record_needs_no_changes():
    result = plan(*WEBSITE, *MAIL_ENTRIES)

    assert not result.changes
    assert result.unchanged == len(RECORDS)
    assert list(result.result) == [*WEBSITE, *MAIL_ENTRIES]


def test_transips_default_mail_records_are_replaced_and_the_rest_stays():
    transip_mail = [
        Entry("@", 300, "MX", "10 mx.transip.email."),
        Entry("@", 300, "TXT", "v=spf1 include:_spf.transip.email ~all"),
        Entry("transip-A._domainkey", 3600, "CNAME", "_dkim-A.transip.email."),
        Entry("_dmarc", 86400, "TXT", "v=DMARC1; p=reject;"),
        Entry("mail", 300, "CNAME", "@"),
    ]

    result = plan(*WEBSITE, *transip_mail)

    assert set(result.remove) == {transip_mail[0], transip_mail[1], transip_mail[4]}
    # Mail servers the SPF record allowed can still send; the DMARC policy is the owner's.
    assert Entry("@", 3600, "TXT", f"v=spf1 ip4:{IPV4} ip6:{IPV6} include:_spf.transip.email ~all") in result.add
    assert transip_mail[3] in result.result
    assert transip_mail[2] in result.result
    assert all(entry in result.result for entry in WEBSITE)
    assert not any(entry in result.result for entry in result.remove)


def test_other_txt_records_at_the_domain_stay():
    verification = Entry("@", 300, "TXT", "google-site-verification=abc")

    result = plan(verification, *without(MAIL_ENTRIES, ("TXT", "@")))

    assert verification in result.result
    assert result.add == (Entry("@", 3600, "TXT", "v=spf1 mx ~all"),)


@pytest.mark.parametrize(
    "spf",
    ["v=spf1 mx -all", "v=spf1 +mx include:_spf.google.com ~all", f"v=spf1 ip4:93.184.216.0/24 ip6:{IPV6} -all",
     '"v=spf1 mx " "-all"'],
)
def test_an_spf_record_that_allows_the_mail_host_stays(spf):
    result = plan(*without(MAIL_ENTRIES, ("TXT", "@")), Entry("@", 300, "TXT", spf))

    assert not result.changes


@pytest.mark.parametrize(
    "spf, published",
    [
        ("v=spf1 -all", f"v=spf1 ip4:{IPV4} ip6:{IPV6} -all"),
        (f"v=spf1 ip4:{IPV4} -all", f"v=spf1 ip6:{IPV6} ip4:{IPV4} -all"),
        ("v=spf1 -mx a ~all", f"v=spf1 ip4:{IPV4} ip6:{IPV6} -mx a ~all"),
        ("v=spf1 ~all mx", f"v=spf1 ip4:{IPV4} ip6:{IPV6} ~all mx"),
        ("v=spf1", f"v=spf1 ip4:{IPV4} ip6:{IPV6}"),
        ('" v=spf1 include:_spf.google.com ~all"', f"v=spf1 ip4:{IPV4} ip6:{IPV6} include:_spf.google.com ~all"),
    ],
)
def test_an_spf_record_that_doesnt_allow_the_mail_host_gets_its_addresses(spf, published):
    old = Entry("@", 300, "TXT", spf)

    result = plan(*without(MAIL_ENTRIES, ("TXT", "@")), old)

    assert result.remove == (old,)
    assert result.add == (Entry("@", 3600, "TXT", published),)


def test_several_spf_records_are_replaced_by_one():
    old = [Entry("@", 300, "TXT", "v=spf1 a -all"), Entry("@", 300, "TXT", "v=spf1 mx -all")]

    result = plan(*without(MAIL_ENTRIES, ("TXT", "@")), *old)

    assert result.remove == tuple(old)
    assert result.add == (Entry("@", 3600, "TXT", "v=spf1 mx ~all"),)


def test_of_several_spf_records_the_recommended_one_stays():
    old = [Entry("@", 300, "TXT", "v=spf1 a -all"), Entry("@", 300, "TXT", "v=spf1 mx ~all")]

    result = plan(*without(MAIL_ENTRIES, ("TXT", "@")), *old)

    assert result.remove == (old[0],)
    assert result.add == ()


def test_a_dmarc_policy_stays_but_several_are_replaced():
    one = Entry("_dmarc", 300, "TXT", "v=DMARC1; p=none; rua=mailto:dmarc@example.nl")
    assert not plan(*without(MAIL_ENTRIES, ("TXT", "_dmarc")), one).changes

    two = [one, Entry("_dmarc", 300, "TXT", "v=DMARC1; p=reject")]
    result = plan(*without(MAIL_ENTRIES, ("TXT", "_dmarc")), *two)
    assert result.remove == tuple(two)
    assert result.add == (Entry("_dmarc", 3600, "TXT", "v=DMARC1; p=quarantine"),)


def test_a_dmarc_record_delegated_with_a_cname_stays():
    delegated = Entry("_dmarc", 300, "CNAME", "example-nl._dmarc.reports.example.")

    assert not plan(*without(MAIL_ENTRIES, ("TXT", "_dmarc")), delegated).changes


def test_the_mail_host_loses_its_other_addresses_and_cnames():
    old = [Entry("mail", 300, "A", "198.51.100.80"), Entry("Mail", 300, "CNAME", "@"), Entry("mail", 300, "TXT", "x")]

    result = plan(*without(MAIL_ENTRIES, ("A", "mail"), ("AAAA", "mail")), *old)

    assert result.remove == tuple(old[:2])
    assert result.add == (Entry("mail", 3600, "A", IPV4),)
    assert old[2] in result.result


def test_records_written_differently_are_the_same():
    written_differently = [
        Entry("@", 300, "MX", "10 mail"),
        Entry("MAIL", 300, "A", "93.184.216.34"),
        Entry("_imaps._tcp", 300, "SRV", "0 1 993 Mail.Example.NL."),
        Entry("_imap._tcp", 300, "SRV", "10 1 143 mail"),
        Entry("mail._domainkey", 300, "TXT", '"v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0" "BAQEFAAOC"'),
    ]
    replaced = {("MX", "@"), ("A", "mail"), ("SRV", "_imaps._tcp"), ("SRV", "_imap._tcp"), ("TXT", "mail._domainkey")}

    assert not plan(*without(MAIL_ENTRIES, *replaced), *written_differently).changes


def test_spf_allows_the_address_mail_goes_out_from_even_though_no_name_points_there():
    """Mail leaves over IPv6 from a server that has it, whatever the names point to, and an SPF record without
    that address has every such message refused."""
    result = plan(*without(MAIL_ENTRIES, ("TXT", "@")), Entry("@", 300, "TXT", "v=spf1 -all"))

    assert result.add == (Entry("@", 3600, "TXT", f"v=spf1 ip4:{IPV4} ip6:{IPV6} -all"),)


def test_an_ipv6_record_left_at_a_published_name_is_taken_away():
    """A name is published to be reached; publishing again cleans up an address that was published before."""
    result = plan(*MAIL_ENTRIES, Entry("mail", 300, "AAAA", IPV6), Entry("webmail", 300, "AAAA", IPV6))

    assert result.remove == (Entry("mail", 300, "AAAA", IPV6), Entry("webmail", 300, "AAAA", IPV6))
    assert result.add == ()


def test_another_dkim_key_or_a_cname_for_it_is_replaced():
    old = [Entry("mail._domainkey", 300, "TXT", "v=DKIM1; p=OTHER"), Entry("mail._domainkey", 300, "CNAME", "x.nl.")]

    result = plan(*without(MAIL_ENTRIES, ("TXT", "mail._domainkey")), *old)

    assert result.remove == tuple(old)
    assert result.add == (Entry("mail._domainkey", 3600, "TXT", DKIM_VALUE),)


def test_srv_records_of_another_provider_are_replaced():
    old = Entry("_submission._tcp", 300, "SRV", "0 1 587 smtp.provider.nl.")

    result = plan(*without(MAIL_ENTRIES, ("SRV", "_submission._tcp")), old)

    assert result.remove == (old,)
    assert result.add == (Entry("_submission._tcp", 3600, "SRV", "10 1 587 mail.example.nl."),)


def test_a_subdomain_uses_names_relative_to_it():
    records = dns_check.recommended_records("shop.example.nl", {ip_address(IPV4)}, None)

    result = zone.plan("shop.example.nl", [], records)

    assert result.add[:2] == (Entry("@", 3600, "MX", "10 mail.shop.example.nl."), Entry("mail", 3600, "A", IPV4))


def test_absolute_writes_names_out_in_full():
    assert zone.absolute("example.nl", "@") == "example.nl"
    assert zone.absolute("example.nl", "_imaps._tcp") == "_imaps._tcp.example.nl"


def test_autodetect_records_of_a_previous_provider_are_removed():
    old = [
        Entry("autoconfig", 300, "CNAME", "autoconfig.provider.nl."),
        Entry("autodiscover", 300, "A", "198.51.100.25"),
        Entry("_autodiscover._tcp", 300, "SRV", "0 0 443 autodiscover.provider.nl."),
    ]
    other = Entry("autodiscover", 300, "TXT", "kept")

    result = plan(*MAIL_ENTRIES, *old, other)

    assert result.remove == tuple(old)
    assert result.add == ()
    assert other in result.result


def test_a_subdomain_in_its_parents_zone_uses_names_relative_to_the_parent():
    records = dns_check.recommended_records("shop.example.nl", {ip_address(IPV4)}, None)
    current = [Entry("@", 300, "MX", "10 mx.provider.nl."), Entry("autoconfig.shop", 300, "CNAME", "x.nl."),
               Entry("shop", 300, "MX", "10 mail.shop")]

    result = zone.plan("shop.example.nl", current, records, zone="example.nl")

    assert result.remove == (current[1],)
    assert current[0] in result.result
    assert Entry("mail.shop", 3600, "A", IPV4) in result.add
    assert Entry("_imaps._tcp.shop", 3600, "SRV", "0 1 993 mail.shop.example.nl.") in result.add
    assert Entry("_dmarc.shop", 3600, "TXT", "v=DMARC1; p=quarantine") in result.add
    assert result.unchanged == 1  # the MX record, written relative to example.nl
