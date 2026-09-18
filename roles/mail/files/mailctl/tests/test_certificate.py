from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_certificate

from mailctl.core import certificate
from mailctl.core.certificate import Certificate
from mailctl.core.dns_check import Status
from mailctl.core.errors import MailctlError

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def test_read_gives_the_names_and_the_end_of_a_certificate(tmp_path):
    path = make_certificate(tmp_path / "fullchain.pem", "server.hosting.example", "mail.example.nl", "*.Shop.nl", days=30)

    read = certificate.read(path)

    assert read.names == ("server.hosting.example", "mail.example.nl", "*.shop.nl")
    assert abs(read.expires - (datetime.now(UTC) + timedelta(days=30))) < timedelta(minutes=5)


def test_read_explains_an_empty_certificate_file(tmp_path):
    path = tmp_path / "fullchain.pem"
    path.touch()

    with pytest.raises(MailctlError, match="is empty: this server has no certificate yet"):
        certificate.read(path)


def test_read_explains_a_missing_certificate(tmp_path):
    with pytest.raises(MailctlError, match="There is no certificate at"):
        certificate.read(tmp_path / "fullchain.pem")


def test_parse_reads_days_padded_with_a_space():
    parsed = certificate.parse("notAfter=Nov  3 08:05:09 2026 GMT\nX509v3 Subject Alternative Name: \n    DNS:a.nl\n")

    assert parsed == Certificate(("a.nl",), datetime(2026, 11, 3, 8, 5, 9, tzinfo=UTC))


def test_parse_explains_output_without_an_end_date():
    with pytest.raises(MailctlError, match="didn't show when the certificate expires"):
        certificate.parse("notAfter=2026-11-03 08:05:09Z\n")


@pytest.mark.parametrize(
    "host, covered",
    [
        ("mail.example.nl", True),
        ("Mail.Example.NL", True),
        ("mail.shop.nl", True),
        ("shop.nl", False),
        ("a.mail.shop.nl", False),
        ("mail.other.nl", False),
    ],
)
def test_covers_matches_names_and_one_level_of_wildcard(host, covered):
    assert certificate.covers(Certificate(("mail.example.nl", "*.shop.nl"), NOW), host) is covered


def server_check(names=("server.hosting.example",), expires=NOW + timedelta(days=60)):
    return certificate.check_server(Certificate(names, expires), "server.hosting.example", NOW)


def test_a_valid_certificate_passes_and_shows_its_end():
    result = server_check()

    assert (result.status, result.detail) == (Status.OK, "Valid until 2026-11-17.")


def test_an_expired_certificate_is_a_problem():
    result = server_check(expires=NOW - timedelta(days=1))

    assert result.status is Status.FAIL
    assert "expired on 2026-09-17" in result.detail


def test_a_certificate_that_isnt_renewed_in_time_is_a_warning():
    result = server_check(expires=NOW + timedelta(days=10))

    assert result.status is Status.WARN
    assert "certbot renew" in result.detail


def test_a_certificate_without_the_hostname_is_a_problem():
    result = server_check(names=("other.hosting.example",))

    assert result.status is Status.FAIL
    assert "doesn't include server.hosting.example" in result.detail


def test_the_domain_check_wants_the_mail_host_in_the_certificate():
    held = Certificate(("server.hosting.example", "mail.example.nl"), NOW)

    assert certificate.check_domain(held, "example.nl").status is Status.OK
    missing = certificate.check_domain(held, "other.nl")
    assert missing.status is Status.FAIL
    assert "doesn't include mail.other.nl" in missing.detail
