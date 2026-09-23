import pytest

from domainctl.core import certificates, template
from domainctl.core.sites import Site
from serverctl import certbot, openlitespeed
from serverctl.errors import CtlError

SITE = Site("example", "example.nl", ("www.example.nl",))


@pytest.fixture
def server(monkeypatch):
    """Records the restarts and every certbot run, and lets a test make certbot fail."""
    class Server:
        restarts = 0
        runs = []
        fails = None

        def restart(self, root):
            self.restarts += 1

        def obtain(self, letsencrypt_dir, email, cert_name, webroot, names, dry_run=False):
            self.runs.append((cert_name, tuple(names), dry_run, str(webroot)))
            if self.fails and (self.fails == "both" or (self.fails == "dry") == dry_run):
                raise CtlError("Let's Encrypt refused it.")

    server = Server()
    server.runs = []
    monkeypatch.setattr(openlitespeed, "restart", server.restart)
    monkeypatch.setattr(certbot, "obtain", server.obtain)
    return server


def test_a_test_run_comes_before_the_real_one(config, server):
    certificates.obtain(config, SITE)
    assert [(run[0], run[1], run[2]) for run in server.runs] == [
        ("example", ("example.nl", "www.example.nl"), True),
        ("example", ("example.nl", "www.example.nl"), False),
    ]


def test_the_certificate_is_named_after_the_user_not_the_domain(config, server):
    """The template looks it up as /etc/letsencrypt/live/$VH_NAME/, and $VH_NAME is the Linux user."""
    certificates.obtain(config, SITE)
    assert all(run[0] == "example" for run in server.runs)


def test_certbot_proves_the_names_from_the_site_s_own_web_root(config, server):
    certificates.obtain(config, SITE)
    assert all(run[3] == str(config.docroot_of("example")) for run in server.runs)


def test_the_redirect_is_off_while_certbot_runs(config, server, monkeypatch):
    seen = []
    monkeypatch.setattr(certbot, "obtain",
                        lambda *args, **kwargs: seen.append(template.redirect_is_on(template.read(config))))
    certificates.obtain(config, SITE)
    assert seen == [False, False]


def test_the_redirect_is_back_on_afterwards(config, server):
    certificates.obtain(config, SITE)
    assert template.redirect_is_on(template.read(config))


def test_the_redirect_is_back_on_even_when_certbot_fails(config, server):
    """Leaving it off would serve every other site on this server without encryption."""
    server.fails = "both"
    with pytest.raises(CtlError, match="Let's Encrypt refused it"):
        certificates.obtain(config, SITE)
    assert template.redirect_is_on(template.read(config))


def test_a_failed_test_run_never_reaches_the_real_one(config, server):
    """A failed validation counts against Let's Encrypt's limits; a test run does not."""
    server.fails = "dry"
    with pytest.raises(CtlError):
        certificates.obtain(config, SITE)
    assert [run[2] for run in server.runs] == [True]


def test_openlitespeed_is_restarted_for_each_change_to_the_redirect(config, server):
    certificates.obtain(config, SITE)
    assert server.restarts == 2


def test_nothing_is_restarted_when_the_template_did_not_change(config, server, monkeypatch):
    monkeypatch.setattr(template, "set_redirect", lambda config, on: False)
    certificates.obtain(config, SITE)
    assert server.restarts == 0


def test_a_failure_to_switch_the_redirect_back_on_is_added_to_certbot_s_own(config, server, monkeypatch):
    server.fails = "both"
    calls = []

    def set_redirect(config, on):
        calls.append(on)
        if on:
            raise CtlError("The template is gone.")
        return True

    monkeypatch.setattr(template, "set_redirect", set_redirect)
    with pytest.raises(CtlError) as failure:
        certificates.obtain(config, SITE)
    assert "Let's Encrypt refused it" in failure.value.message
    assert "could not be switched back on" in failure.value.message
    assert calls == [False, True]


def test_a_failure_to_switch_it_back_on_is_raised_when_certbot_worked(config, server, monkeypatch):
    monkeypatch.setattr(template, "set_redirect",
                        lambda config, on: (_ for _ in ()).throw(CtlError("gone")) if on else True)
    with pytest.raises(CtlError, match="gone"):
        certificates.obtain(config, SITE)


def test_exists_and_path_follow_the_user_name(config):
    assert certificates.path(config, "example") == config.letsencrypt_dir / "live" / "example" / "fullchain.pem"
    assert certificates.exists(config, "example") is False
    live = config.letsencrypt_dir / "live" / "example"
    live.mkdir(parents=True)
    (live / "fullchain.pem").write_text("x")
    (live / "privkey.pem").write_text("x")
    assert certificates.exists(config, "example") is True


def test_the_lock_is_held_for_the_whole_certbot_run(config, server, lock_file, monkeypatch):
    """Otherwise 'domainctl sync' could switch the redirect back on while Let's Encrypt is still checking."""
    import fcntl
    import os

    locked_out = []

    def check_lock(*args, **kwargs):
        descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            locked_out.append(False)
        except OSError:
            locked_out.append(True)
        finally:
            os.close(descriptor)

    monkeypatch.setattr(certbot, "obtain", check_lock)
    certificates.obtain(config, SITE)
    assert locked_out == [True, True]


def test_the_lock_is_free_again_afterwards(config, server, lock_file):
    import fcntl
    import os

    certificates.obtain(config, SITE)
    descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def test_the_lock_is_free_again_when_certbot_fails(config, server, lock_file):
    import fcntl
    import os

    server.fails = "both"
    with pytest.raises(CtlError):
        certificates.obtain(config, SITE)
    descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
