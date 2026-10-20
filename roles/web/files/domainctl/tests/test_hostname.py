"""domainctl hostname: the site of the server's own name, and the certificate Postfix and Dovecot present."""

from pathlib import Path

import pytest
from test_cli import HERE, _Resolver, certificate, run, server  # noqa: F401

from domainctl.core import hostsite
from serverctl import certbot, openlitespeed, system
from serverctl.errors import CtlError
from serverctl.openlitespeed import Member, Template


@pytest.fixture
def asked(server, monkeypatch):
    """The hostname points here, and certbot only records what it is asked and leaves a certificate behind."""
    monkeypatch.setattr(_Resolver, "answers", {server.hostname: HERE})
    monkeypatch.setattr(hostsite, "reload_mail", lambda: ["Postfix", "Dovecot"])
    calls = []

    def obtain_each(directory, email, cert_name, roots, key_type, dry_run=False):
        calls.append((cert_name, dict(roots), key_type, dry_run))
        if not dry_run:
            certificate(server, cert_name, list(roots))

    monkeypatch.setattr(certbot, "obtain_each", obtain_each)
    return calls


def _renewal(server, **roots):
    path = server.letsencrypt_dir / "renewal" / f"{server.hostname}.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[renewalparams]\nauthenticator = webroot\nkey_type = rsa\n[[webroot_map]]\n" +
                    "".join(f"{name} = {root}\n" for name, root in roots.items()))


def test_hostname_makes_its_site_and_gets_the_certificate(server, asked):
    result = run("hostname")

    assert result.exit_code == 0, result.output
    proof = {server.hostname: hostsite.webroot(server)}
    assert asked == [(server.hostname, proof, "rsa", True), (server.hostname, proof, "rsa", False)]
    lines = openlitespeed.read(server.ols_root)
    assert openlitespeed.virtual_hosts(lines)["Server"] == hostsite.NOTE
    http, https = openlitespeed.listeners(lines)
    assert all(openlitespeed.maps(lines, listener)["Server"] == [server.hostname] for listener in http + https)
    assert f"live/{server.hostname}/fullchain.pem" in hostsite.site_file(server).read_text()
    assert (hostsite.webroot(server) / ".well-known/acme-challenge").is_dir()
    assert "Postfix presents it now" in result.output and "Dovecot presents it now" in result.output


def test_hostname_keeps_mailctls_names_and_takes_the_hostname_over(server, asked, monkeypatch):
    """A mail server whose certificate mailctl made: the hostname moves to this site, mail.DOMAIN stays put."""
    found = certificate(server, server.hostname, (server.hostname, "mail.example.nl"))
    monkeypatch.setattr(certbot, "read", lambda path: found)
    _renewal(server, **{server.hostname: "/var/www/mailcert", "mail.example.nl": "/var/www/mailcert"})
    mailnames = Template("mailnames", "conf/templates/mailnames.conf", ("http",), "Managed by mailctl")
    lines = openlitespeed.read(server.ols_root)
    openlitespeed.write(server.ols_root,
                        openlitespeed.with_member(lines, Member(server.hostname, server.hostname), mailnames))

    result = run("hostname")

    assert result.exit_code == 0, result.output
    assert "another site" in result.output
    assert asked[-1][1] == {server.hostname: hostsite.webroot(server), "mail.example.nl": Path("/var/www/mailcert")}
    assert openlitespeed.members(openlitespeed.read(server.ols_root), "mailnames") == []


def test_hostname_with_its_certificate_asks_for_nothing(server, asked, monkeypatch):
    found = certificate(server, server.hostname, (server.hostname,))
    monkeypatch.setattr(certbot, "read", lambda path: found)
    _renewal(server, **{server.hostname: hostsite.webroot(server)})

    result = run("hostname")

    assert result.exit_code == 0, result.output
    assert asked == []
    assert "has its certificate already" in result.output


def test_the_mail_roles_empty_files_make_way_for_the_first_certificate(server, asked, monkeypatch):
    live = server.letsencrypt_dir / "live" / server.hostname
    live.mkdir(parents=True)
    for name in ("fullchain.pem", "privkey.pem"):
        (live / name).touch()
    seen = []

    def refused(directory, email, cert_name, roots, key_type, dry_run=False):
        seen.append(sorted(path.name for path in live.iterdir()))
        raise CtlError("Let's Encrypt refused it.")

    monkeypatch.setattr(certbot, "obtain_each", refused)

    result = run("hostname")

    assert result.exit_code == 1
    assert seen == [[]], "certbot refuses a live directory with anything in it"
    assert sorted(path.name for path in live.iterdir()) == ["fullchain.pem", "privkey.pem"], "Dovecot needs them"
    assert certbot.is_placeholder(server.letsencrypt_dir, server.hostname)


def test_hostname_leaves_a_server_site_made_by_hand_alone(server, asked):
    path = openlitespeed.config_file(server.ols_root)
    path.write_text(path.read_text() + "\nvirtualhost Server {\n  vhRoot                  Server/\n}\n")

    result = run("hostname")

    assert result.exit_code == 1
    assert "didn't make" in result.output
    assert not hostsite.site_file(server).exists() and asked == []


def test_hostname_needs_the_name_to_point_here(server, asked, monkeypatch):
    monkeypatch.setattr(_Resolver, "answers", {})

    result = run("hostname", "--no-dns")

    assert result.exit_code == 1
    assert "Point" in result.output and asked == []


def test_postfix_and_dovecot_read_it_again_where_they_are_installed(monkeypatch):
    monkeypatch.setattr(hostsite.shutil, "which", lambda program: f"/usr/sbin/{program}")
    reloaded = []
    monkeypatch.setattr(system, "reload", reloaded.append)

    assert hostsite.reload_mail() == ["Postfix", "Dovecot"]
    assert reloaded == ["postfix", "dovecot"]
