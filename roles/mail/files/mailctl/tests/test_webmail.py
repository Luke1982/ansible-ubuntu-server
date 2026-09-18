from dataclasses import replace
from ipaddress import ip_address

import pytest
from conftest import FakeCommand

from mailctl.core import webmail
from mailctl.core.dns_check import LookupFailed
from mailctl.core.webmail import Outcome, State

SERVER_IPS = {ip_address("203.0.113.5"), ip_address("2001:db8::5")}

# Makes or deletes the certificate files where certbot would, for --cert-name in --config-dir.
FAKE_CERTBOT = r"""
action=$1; shift
while [ $# -gt 0 ]; do
  case $1 in --config-dir) dir=$2; shift;; --cert-name) name=$2; shift;; esac
  shift
done
case $action in
  certonly) mkdir -p "$dir/live/$name" && touch "$dir/live/$name/fullchain.pem" "$dir/live/$name/privkey.pem";;
  delete) rm -r "$dir/live/$name";;
esac
"""
# How certbot reports a challenge Let's Encrypt couldn't fetch.
REFUSING_CERTBOT = r"""
cat >&2 <<'END'
Saving debug log to /var/log/letsencrypt/letsencrypt.log
Requesting a certificate for webmail.other.nl

Certbot failed to authenticate some domains (authenticator: webroot). The Certificate Authority reported these problems:
  Domain: webmail.other.nl
  Type:   unauthorized
  Detail: 203.0.113.5: Invalid response from http://webmail.other.nl/.well-known/acme-challenge/x: 404

Some challenges have failed.
END
exit 1
"""


class FakeResolver:
    """A and AAAA records per name; a name in failing times out."""

    def __init__(self, addresses: dict[str, list[str]], failing=()):
        self._addresses = addresses
        self._failing = failing

    def addresses(self, name):
        if name in self._failing:
            raise LookupFailed(f"The A lookup for {name} failed: timed out")
        return {ip_address(address) for address in self._addresses.get(name, [])}


HERE = FakeResolver({"webmail.example.nl": ["203.0.113.5", "2001:db8::5"], "webmail.other.nl": ["203.0.113.5"]})


@pytest.fixture
def config(config, tmp_path):
    return replace(
        config,
        ols_root=tmp_path / "lsws",
        webmail_root=tmp_path / "www" / "webmail",
        letsencrypt_dir=tmp_path / "letsencrypt",
        sogo_resources=tmp_path / "sogo" / "WebServerResources",
    )


@pytest.fixture
def certbot(fake_command):
    return fake_command("certbot", FAKE_CERTBOT)


@pytest.fixture
def lswsctrl(config):
    directory = config.ols_root / "bin"
    directory.mkdir(parents=True)
    return FakeCommand(directory, "lswsctrl", "")


class FakeWebServer:
    """Serves the test file, except for the names and addresses in refused. Records (address, name) of requests."""

    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.refused: set[str] = set()

    def serves(self, address, name, file_name, token):
        self.requests.append((str(address), name))
        return not {name, str(address)} & self.refused


@pytest.fixture
def served(monkeypatch):
    web_server = FakeWebServer()
    monkeypatch.setattr(webmail, "_serves", web_server.serves)
    monkeypatch.setattr(webmail, "SERVE_TIMEOUT", 0)
    return web_server


# Every test gets these; the ones that look at them ask for them by name.
pytestmark = pytest.mark.usefixtures("certbot", "lswsctrl", "served")


def conf(config, name):
    return (config.ols_root / "conf" / "webmail" / name).read_text()


def sync(config, *domains, resolver=HERE):
    return webmail.sync(config, domains, SERVER_IPS, resolver)


def test_a_domain_whose_webmail_name_points_here_gets_a_site_with_a_certificate(config, certbot, lswsctrl, served):

    result = sync(config, "example.nl")

    assert list(result.outcomes) == [Outcome("webmail.example.nl", State.NEW)]
    assert result.changed
    assert webmail.sites(config) == ["webmail.example.nl"]
    assert webmail.has_certificate(config, "webmail.example.nl")
    assert served.requests == [("203.0.113.5", "webmail.example.nl"), ("2001:db8::5", "webmail.example.nl")]
    assert certbot.calls == [[
        "certonly", "--webroot", "--webroot-path", str(config.webmail_root), "--cert-name", "webmail.example.nl",
        "--domains", "webmail.example.nl", "--agree-tos", "--register-unsafely-without-email",
        "--non-interactive", "--config-dir", str(config.letsencrypt_dir),
    ]]
    # Once to serve the challenge on port 80, once to switch the site to https.
    assert lswsctrl.calls == [["restart"], ["restart"]]
    assert "map webmail.example.nl webmail.example.nl" in conf(config, "http-maps.conf")
    assert "map webmail.example.nl webmail.example.nl" in conf(config, "https-maps.conf")
    assert "virtualhost webmail.example.nl {" in conf(config, "vhosts.conf")


def test_the_site_serves_sogo_over_https_for_its_own_name(config):
    sync(config, "example.nl")

    site = conf(config, "webmail.example.nl.conf")

    live = config.letsencrypt_dir / "live" / "webmail.example.nl"
    for expected in (
        f"keyFile                 {live}/privkey.pem",
        f"certFile                {live}/fullchain.pem",
        "address                 127.0.0.1:20000",
        "RewriteRule ^ https://webmail.example.nl%{REQUEST_URI} [R=301,L]",
        "RequestHeader set x-webobjects-server-url https://webmail.example.nl",
        "RequestHeader set x-webobjects-server-name webmail.example.nl",
        "RewriteRule ^/\\.well-known/(caldav|carddav)$ /SOGo/dav/ [R=301,L]",
        "RewriteRule ^ http://sogo/SOGo/Microsoft-Server-ActiveSync [P,L]",
        f"location                {config.sogo_resources}/",
        f"docRoot                 {config.webmail_root}/",
    ):
        assert expected in site, expected


def test_nothing_changes_for_a_live_site(config, certbot, lswsctrl):
    sync(config, "example.nl")

    result = sync(config, "example.nl")

    assert list(result.outcomes) == [Outcome("webmail.example.nl", State.LIVE)]
    assert not result.changed
    assert len(certbot.calls) == 1
    assert len(lswsctrl.calls) == 2


def test_certbot_registers_the_account_with_the_email_address_when_there_is_one(config, certbot):
    sync(replace(config, letsencrypt_email="admin@example.nl"), "example.nl")

    call = certbot.calls[0]
    assert call[call.index("--email") + 1] == "admin@example.nl"
    assert "--register-unsafely-without-email" not in call


@pytest.mark.parametrize(("records", "detail"), [
    ({}, "webmail.example.nl has no A or AAAA record."),
    ({"webmail.example.nl": ["198.51.100.9"]}, "webmail.example.nl points to 198.51.100.9, which isn't this server."),
    ({"webmail.example.nl": ["203.0.113.5", "2001:db8::99"]},
     "webmail.example.nl also points to 2001:db8::99, which isn't this server."),
])
def test_a_domain_whose_webmail_name_doesnt_point_here_waits_for_it(
    config, certbot, lswsctrl, served, records, detail
):
    sync(config)

    result = sync(config, "example.nl", resolver=FakeResolver(records))

    assert list(result.outcomes) == [Outcome("webmail.example.nl", State.WAITING, detail)]
    assert not result.changed
    assert webmail.sites(config) == []
    assert certbot.calls == [] and lswsctrl.calls == [["restart"]] and served.requests == []


def test_a_site_whose_name_no_longer_points_here_goes_with_its_certificate(config, certbot, lswsctrl):
    sync(config, "example.nl")

    result = sync(config, "example.nl", resolver=FakeResolver({"webmail.example.nl": ["198.51.100.9"]}))

    assert list(result.outcomes) == [Outcome(
        "webmail.example.nl", State.REMOVED, "webmail.example.nl points to 198.51.100.9, which isn't this server."
    )]
    assert result.changed
    assert webmail.sites(config) == []
    assert not webmail.has_certificate(config, "webmail.example.nl")
    assert certbot.calls[-1][:3] == ["delete", "--cert-name", "webmail.example.nl"]
    assert "webmail.example.nl" not in conf(config, "vhosts.conf") + conf(config, "http-maps.conf")
    # Twice to set the site up, once to take it away.
    assert len(lswsctrl.calls) == 3


def test_a_site_whose_domain_is_gone_goes_too(config):
    sync(config, "example.nl")

    result = sync(config)

    assert list(result.outcomes) == [
        Outcome("webmail.example.nl", State.REMOVED, "Its domain is no longer on this server.")
    ]
    assert webmail.sites(config) == []
    assert not webmail.has_certificate(config, "webmail.example.nl")


def test_a_failed_lookup_leaves_a_site_as_it_is(config, certbot, lswsctrl):
    sync(config, "example.nl")

    result = sync(config, "example.nl", resolver=FakeResolver({}, failing={"webmail.example.nl"}))

    assert list(result.outcomes) == [Outcome(
        "webmail.example.nl", State.UNCHECKED, "The A lookup for webmail.example.nl failed: timed out"
    )]
    assert not result.changed
    assert webmail.sites(config) == ["webmail.example.nl"]
    assert webmail.has_certificate(config, "webmail.example.nl")
    assert len(certbot.calls) == 1 and len(lswsctrl.calls) == 2


def test_a_failed_lookup_creates_no_site(config):
    result = sync(config, "example.nl", resolver=FakeResolver({}, failing={"webmail.example.nl"}))

    assert list(result.outcomes) == [Outcome(
        "webmail.example.nl", State.WAITING, "The A lookup for webmail.example.nl failed: timed out"
    )]
    assert webmail.sites(config) == []


def test_a_refused_certificate_leaves_a_site_that_only_answers_challenges(config, fake_command):
    fake_command("certbot", REFUSING_CERTBOT)

    result = sync(config, "other.nl")

    assert list(result.outcomes) == [Outcome(
        "webmail.other.nl", State.FAILED,
        "No certificate for webmail.other.nl: 203.0.113.5: Invalid response from"
        " http://webmail.other.nl/.well-known/acme-challenge/x: 404",
    )]
    assert result.changed
    site = conf(config, "webmail.other.nl.conf")
    assert "acme-challenge" in site
    assert "vhssl" not in site and "extprocessor" not in site
    assert "webmail.other.nl" in conf(config, "http-maps.conf")
    assert "webmail.other.nl" not in conf(config, "https-maps.conf")


def test_a_refused_certificate_doesnt_stop_the_other_domains(config, fake_command):
    fake_command("certbot", 'case "$*" in *webmail.other.nl*) echo "  Detail: refused" >&2; exit 1;; esac\n'
                            + FAKE_CERTBOT)

    result = sync(config, "example.nl", "other.nl")

    assert [(outcome.host, outcome.state) for outcome in result.outcomes] == [
        ("webmail.example.nl", State.NEW), ("webmail.other.nl", State.FAILED),
    ]
    maps = conf(config, "https-maps.conf")
    assert "webmail.example.nl" in maps and "webmail.other.nl" not in maps


def test_the_certificate_is_tried_again_on_the_next_run(config, fake_command):
    fake_command("certbot", REFUSING_CERTBOT)
    sync(config, "other.nl")
    fake_command("certbot", FAKE_CERTBOT)

    result = sync(config, "other.nl")

    assert list(result.outcomes) == [Outcome("webmail.other.nl", State.NEW)]
    assert "webmail.other.nl" in conf(config, "https-maps.conf")


def test_no_certificate_is_requested_while_the_site_isnt_served(config, certbot, served):
    served.refused.add("webmail.example.nl")

    result = sync(config, "example.nl")

    assert list(result.outcomes) == [Outcome(
        "webmail.example.nl", State.FAILED,
        "OpenLiteSpeed doesn't serve webmail.example.nl on port 80 at 203.0.113.5."
        " Run the Ansible playbook: it adds the webmail sites to OpenLiteSpeed's configuration.",
    )]
    assert certbot.calls == []


def test_every_address_of_the_site_must_be_served(config, certbot, served):
    """Let's Encrypt may use any of them; a listener for IPv4 only is a common reason for a refused certificate."""
    served.refused.add("2001:db8::5")

    result = sync(config, "example.nl")

    assert result.outcomes[0].state is State.FAILED
    assert "at 2001:db8::5." in result.outcomes[0].detail
    assert certbot.calls == []


def test_the_test_file_for_openlitespeed_is_removed_afterwards(config):
    sync(config, "example.nl")

    assert list((config.webmail_root / ".well-known" / "acme-challenge").iterdir()) == []


def test_remove_takes_away_a_domains_site_and_certificate(config, certbot, lswsctrl):
    sync(config, "example.nl", "other.nl")

    assert webmail.remove(config, "example.nl")

    assert webmail.sites(config) == ["webmail.other.nl"]
    assert not webmail.has_certificate(config, "webmail.example.nl")
    assert certbot.calls[-1][:3] == ["delete", "--cert-name", "webmail.example.nl"]
    assert lswsctrl.calls[-1] == ["restart"]
    assert not webmail.remove(config, "example.nl")


def test_sites_are_the_generated_site_files_only(config):
    sync(config, "example.nl")

    assert sorted(path.name for path in (config.ols_root / "conf" / "webmail").iterdir()) == [
        "http-maps.conf", "https-maps.conf", "vhosts.conf", "webmail.example.nl.conf",
    ]
    assert webmail.sites(config) == ["webmail.example.nl"]


def test_domains_with_capitals_from_the_old_helper_script_get_their_site_in_lower_case(config):
    result = sync(config, "Example.NL")

    assert [outcome.host for outcome in result.outcomes] == ["webmail.example.nl"]
    assert webmail.sites(config) == ["webmail.example.nl"]


def test_names_that_arent_domains_never_reach_openlitespeeds_configuration(config):
    """The old helper script stored any text it was given."""
    result = sync(config, "not a domain", "evil.nl\n}\nextprocessor x {", "a" * 250 + ".nl")

    assert result.outcomes == ()
    assert webmail.sites(config) == []
    assert "evil" not in conf(config, "vhosts.conf")
