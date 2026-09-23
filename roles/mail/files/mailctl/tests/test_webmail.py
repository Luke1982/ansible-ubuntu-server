from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from ipaddress import ip_address

import pytest
from conftest import FakeCommand, make_certificate
from test_openlitespeed import HTTPD_CONFIG

from mailctl.core import certificate, files, openlitespeed, system, webmail
from mailctl.core.errors import MailctlError
from mailctl.core.dns_check import LookupFailed, Status
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
    return replace(config, webmail_root=tmp_path / "www" / "webmail",
                   sogo_resources=tmp_path / "sogo" / "resources")


@pytest.fixture
def certbot(fake_command):
    return fake_command("certbot", FAKE_CERTBOT)


@pytest.fixture
def lswsctrl(config):
    """OpenLiteSpeed, set up by hand in WebAdmin: listeners and a site of its own (see test_openlitespeed)."""
    (config.ols_root / "conf").mkdir(parents=True)
    openlitespeed.config_file(config.ols_root).write_text(HTTPD_CONFIG)
    (config.ols_root / "bin").mkdir()
    return FakeCommand(config.ols_root / "bin", "lswsctrl", "")


class FakeWebServer:
    """Serves the test file, except for the names and addresses in refused, where nothing answers, and those in
    taken, where another site does. Records (address, name) of requests."""

    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.refused: set[str] = set()
        self.taken: set[str] = set()

    def serves(self, address, name, file_name, token):
        self.requests.append((str(address), name))
        if {name, str(address)} & self.taken:
            return certificate.ANOTHER_SITE
        return certificate.NO_ANSWER if {name, str(address)} & self.refused else certificate.SERVED


@pytest.fixture
def served(monkeypatch):
    web_server = FakeWebServer()
    monkeypatch.setattr(certificate, "serves", web_server.serves)
    monkeypatch.setattr(webmail, "SERVE_TIMEOUT", 0)
    return web_server


# Every test gets these; the ones that look at them ask for them by name.
pytestmark = pytest.mark.usefixtures("certbot", "lswsctrl", "served")


def site(config, name):
    """The settings of a site, where WebAdmin keeps a virtual host's."""
    return (vhosts(config) / name / "vhconf.conf").read_text()


def vhosts(config):
    return config.ols_root / "conf" / "vhosts"


def httpd_config(config):
    return openlitespeed.config_file(config.ols_root).read_text()


def mapped(config, listener):
    """The names the listener maps to webmail sites."""
    return {host: names for host, names in openlitespeed.maps(openlitespeed.read(config.ols_root), listener).items()
            if host.startswith("webmail.")}


def sync(config, *domains, resolver=HERE):
    return webmail.sync(config, domains, SERVER_IPS, resolver)


def check(config, domain="example.nl", resolver=HERE, now=None):
    return webmail.check(config, domain, SERVER_IPS, resolver, now or datetime.now().astimezone())


def certify(config, name, *, days=90):
    """A real certificate where certbot leaves an empty one, so it can be read."""
    return make_certificate(config.letsencrypt_dir / "live" / name / "fullchain.pem", name, days=days)


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
    for listener in ("HTTP", "HTTPS", "HTTPS6"):
        assert mapped(config, listener) == {"webmail.example.nl": ["webmail.example.nl"]}
    assert "note                    Managed by mailctl: webmail" in httpd_config(config)


def test_the_site_serves_sogo_over_https_for_its_own_name(config):
    sync(config, "example.nl")

    settings = site(config, "webmail.example.nl")

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
        assert expected in settings, expected


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
    result = sync(config, "example.nl", resolver=FakeResolver(records))

    assert list(result.outcomes) == [Outcome("webmail.example.nl", State.WAITING, detail)]
    assert not result.changed
    assert webmail.sites(config) == []
    assert certbot.calls == [] and lswsctrl.calls == [] and served.requests == []
    assert httpd_config(config) == HTTPD_CONFIG


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
    assert httpd_config(config) == HTTPD_CONFIG
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
    settings = site(config, "webmail.other.nl")
    assert "acme-challenge" in settings
    assert "vhssl" not in settings and "extprocessor" not in settings
    assert mapped(config, "HTTP") == {"webmail.other.nl": ["webmail.other.nl"]}
    assert mapped(config, "HTTPS") == mapped(config, "HTTPS6") == {}


def test_a_refused_certificate_doesnt_stop_the_other_domains(config, fake_command):
    fake_command("certbot", 'case "$*" in *webmail.other.nl*) echo "  Detail: refused" >&2; exit 1;; esac\n'
                            + FAKE_CERTBOT)

    result = sync(config, "example.nl", "other.nl")

    assert [(outcome.host, outcome.state) for outcome in result.outcomes] == [
        ("webmail.example.nl", State.NEW), ("webmail.other.nl", State.FAILED),
    ]
    assert list(mapped(config, "HTTPS")) == ["webmail.example.nl"]


def test_the_certificate_is_tried_again_on_the_next_run(config, fake_command):
    fake_command("certbot", REFUSING_CERTBOT)
    sync(config, "other.nl")
    fake_command("certbot", FAKE_CERTBOT)

    result = sync(config, "other.nl")

    assert list(result.outcomes) == [Outcome("webmail.other.nl", State.NEW)]
    assert list(mapped(config, "HTTPS")) == ["webmail.other.nl"]


def test_no_certificate_is_requested_while_the_site_isnt_served(config, certbot, served):
    served.refused.add("webmail.example.nl")

    result = sync(config, "example.nl")

    assert list(result.outcomes) == [Outcome(
        "webmail.example.nl", State.FAILED,
        "OpenLiteSpeed doesn't serve webmail.example.nl on port 80 at 203.0.113.5, 2001:db8::5."
        " Run the Ansible playbook: it adds the webmail sites to OpenLiteSpeed's configuration.",
    )]
    assert certbot.calls == []


def test_an_address_where_nothing_answers_is_left_to_lets_encrypts_own_fallback(config, certbot, served):
    """OpenLiteSpeed listening on IPv4 only is common. Let's Encrypt tries the other address when a connection
    isn't accepted at all, so the certificate is asked for, and the address is reported."""
    served.refused.add("2001:db8::5")

    result = sync(config, "example.nl")

    assert result.outcomes[0].state is State.NEW
    assert "Nothing answers for webmail.example.nl on port 80 at 2001:db8::5" in result.outcomes[0].detail
    assert [call[:2] for call in certbot.calls] == [["certonly", "--webroot"]]


def test_an_address_where_another_site_answers_stops_the_certificate(config, certbot, served):
    """Let's Encrypt doesn't fall back from an answer it doesn't like: it fails the validation."""
    served.taken.add("2001:db8::5")

    result = sync(config, "example.nl")

    assert result.outcomes[0].state is State.FAILED
    assert "Another site answers for webmail.example.nl on port 80 at 2001:db8::5" in result.outcomes[0].detail
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


def test_sites_are_the_virtual_hosts_with_mailctls_note_and_others_are_left_alone(config):
    sync(config, "example.nl", "other.nl")

    assert webmail.sites(config) == ["webmail.example.nl", "webmail.other.nl"]
    assert sorted(path.name for path in vhosts(config).iterdir()) == ["webmail.example.nl", "webmail.other.nl"]

    sync(config)

    assert httpd_config(config) == HTTPD_CONFIG


@pytest.mark.parametrize("hand_made", [
    "virtualhost webmail.example.nl {\n  vhRoot                  /var/www/roundcube/\n}\n",
    "listener Web {\n  address                 *:80\n  map                     shop webmail.example.nl\n}\n",
])
def test_a_site_for_the_name_that_mailctl_doesnt_manage_is_left_alone(config, certbot, hand_made):
    """Like a site set up by hand for Roundcube."""
    openlitespeed.config_file(config.ols_root).write_text(HTTPD_CONFIG + hand_made)

    result = sync(config, "example.nl")

    assert list(result.outcomes) == [Outcome(
        "webmail.example.nl", State.FAILED,
        "OpenLiteSpeed already has a site for webmail.example.nl, which mailctl leaves alone."
        " Remove it in WebAdmin to get webmail for example.nl.",
    )]
    assert httpd_config(config) == HTTPD_CONFIG + hand_made
    assert certbot.calls == []


@pytest.mark.parametrize(("address", "problem"), [
    ("*:80", "no HTTP listener on port 80"), ("*:443", "no HTTPS listener on port 443"),
])
def test_a_sync_needs_openlitespeed_listening_on_both_ports(config, address, problem):
    openlitespeed.config_file(config.ols_root).write_text(
        HTTPD_CONFIG.replace(f"address                 {address}", "address                 *:8088")
        .replace("address                 [::]:443", "address                 [::]:8443")
    )

    with pytest.raises(MailctlError, match=problem):
        sync(config, "example.nl")


def test_the_sites_settings_are_written_and_deleted_as_openlitespeeds_user(config, monkeypatch):
    """OpenLiteSpeed's conf directory belongs to lsadm, and mailctl runs as root: a link lsadm planted there must not
    make root write or delete anything lsadm couldn't. The config itself is written without following links."""
    as_user = []

    @contextmanager
    def recording(name):
        as_user.append(name)
        yield
        as_user.append(None)

    touched = []
    for module, function in ((files, "replace"), (system, "remove_tree")):
        original = getattr(module, function)
        monkeypatch.setattr(module, function, lambda path, *rest, original=original: (
            touched.append((path, as_user[-1] if as_user else None)), original(path, *rest))[1])
    monkeypatch.setattr(system, "as_user", recording)

    sync(config, "example.nl", "other.nl")
    sync(config, "other.nl")

    under_conf = [(path, user) for path, user in touched if (config.ols_root / "conf") in path.parents]
    assert {path.parent.name for path, _ in under_conf} == {"webmail.example.nl", "webmail.other.nl", "vhosts"}
    assert all(user == config.ols_user for _, user in under_conf)
    assert "$SERVER_ROOT/conf/vhosts/webmail.other.nl/vhconf.conf" in httpd_config(config)

def test_domains_with_capitals_from_the_old_helper_script_get_their_site_in_lower_case(config):
    result = sync(config, "Example.NL")

    assert [outcome.host for outcome in result.outcomes] == ["webmail.example.nl"]
    assert webmail.sites(config) == ["webmail.example.nl"]


def test_names_that_arent_domains_never_reach_openlitespeeds_configuration(config):
    """The old helper script stored any text it was given."""
    result = sync(config, "not a domain", "evil.nl\n}\nextprocessor x {", "a" * 250 + ".nl")

    assert result.outcomes == ()
    assert webmail.sites(config) == []
    assert httpd_config(config) == HTTPD_CONFIG


def test_a_removed_site_takes_openlitespeeds_copy_of_its_settings_along(config):
    """OpenLiteSpeed 1.9 writes a .txt copy of every config file it reads."""
    sync(config, "example.nl", "other.nl")
    for name in ("webmail.example.nl", "webmail.other.nl"):
        (vhosts(config) / name / "vhconf.conf.txt").write_text("copy\n")

    sync(config, "other.nl")

    assert sorted(path.name for path in vhosts(config).iterdir()) == ["webmail.other.nl"]
    assert sorted(path.name for path in (vhosts(config) / "webmail.other.nl").iterdir()) == [
        "vhconf.conf", "vhconf.conf.txt",
    ]


def test_check_says_where_the_webmail_of_a_live_site_is(config):
    sync(config, "example.nl")
    certify(config, "webmail.example.nl")

    found = check(config)

    assert found.status is Status.OK
    assert found.detail == "Webmail is at https://webmail.example.nl."


def test_check_says_which_records_keep_a_site_whose_name_no_longer_points_here(config):
    sync(config, "example.nl")

    found = check(config, resolver=FakeResolver({}))

    assert found.status is Status.WARN
    assert found.detail.startswith("webmail.example.nl has no A or AAAA record.")
    assert "takes the site away" in found.detail
    assert [(record.type, record.name) for record in found.fixes] == [
        ("A", "webmail.example.nl"), ("AAAA", "webmail.example.nl"),
        ("SRV", "_caldavs._tcp.example.nl"), ("SRV", "_carddavs._tcp.example.nl"),
    ]


def test_check_says_nothing_about_a_domain_without_a_site(config):
    assert check(config) is None


def test_check_says_a_site_without_a_certificate_doesnt_serve_webmail_yet(config, fake_command):
    fake_command("certbot", REFUSING_CERTBOT)
    sync(config, "example.nl")

    found = check(config)

    assert found.status is Status.WARN
    assert found.detail.startswith("webmail.example.nl has no certificate yet")
    assert "mailctl webmail sync" in found.detail


def test_check_says_to_renew_an_expired_certificate(config):
    sync(config, "example.nl")
    certify(config, "webmail.example.nl", days=1)

    found = check(config, now=datetime.now().astimezone() + timedelta(days=2))

    assert found.status is Status.WARN
    assert "expired on" in found.detail
    assert found.detail.endswith("Renew it with: certbot renew")


def test_check_warns_when_the_lookup_for_the_webmail_name_fails(config):
    sync(config, "example.nl")

    found = check(config, resolver=FakeResolver({}, failing={"webmail.example.nl"}))

    assert found.status is Status.WARN
    assert found.detail == "The A lookup for webmail.example.nl failed: timed out"
