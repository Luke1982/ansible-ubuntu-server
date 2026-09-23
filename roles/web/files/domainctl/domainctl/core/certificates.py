"""Getting a site's Let's Encrypt certificate, with the redirect to HTTPS out of the way.

The template sends every visitor from HTTP to HTTPS, and a new site has no certificate yet, so Let's Encrypt
would be redirected to a site it cannot connect to. The redirect is therefore switched off in the template while
certbot proves the site, and switched back on the moment it is done, whether it worked or not.

That redirect is shared by every site on the server, so this window is kept as short as it can be: one test run,
one real run, and back. A test run comes first because a failed validation counts against Let's Encrypt's limits
while a test run does not.

The certificate is named after the site's Linux user, not its domain, because the template looks it up as
/etc/letsencrypt/live/$VH_NAME/.
"""

from serverctl import certbot, openlitespeed
from serverctl.errors import CtlError

from ..config import Config
from . import template
from .sites import Site


def path(config: Config, user: str):
    return certbot.live(config.letsencrypt_dir, user) / "fullchain.pem"


def exists(config: Config, user: str) -> bool:
    return certbot.exists(config.letsencrypt_dir, user)


def obtain(config: Config, site: Site) -> None:
    """Gets or extends the site's certificate. Raises CtlError with what Let's Encrypt said when it refuses."""
    with _redirect_off(config):
        certbot.obtain(config.letsencrypt_dir, config.letsencrypt_email, site.user,
                       config.docroot_of(site.user), site.names, dry_run=True)
        certbot.obtain(config.letsencrypt_dir, config.letsencrypt_email, site.user,
                       config.docroot_of(site.user), site.names)


def delete(config: Config, user: str) -> bool:
    return certbot.delete(config.letsencrypt_dir, user)


class _redirect_off:
    """Switches the redirect to HTTPS off for the block, and on again afterwards, restarting OpenLiteSpeed for
    each change. Leaving it off would serve every site on this server without encryption.

    The lock on OpenLiteSpeed's configuration is held for the whole block, certbot included, so a "domainctl
    sync" running at the same time waits instead of switching the redirect back on halfway through.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = openlitespeed.locked()

    def __enter__(self) -> None:
        self._lock.__enter__()
        try:
            if template.set_redirect(self._config, False):
                openlitespeed.restart(self._config.ols_root)
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise

    def __exit__(self, kind, value, traceback) -> bool:
        try:
            if template.set_redirect(self._config, True):
                openlitespeed.restart(self._config.ols_root)
        except CtlError as problem:
            if value is None:
                raise
            # Raising here would hide why certbot failed, so both reasons go into one message.
            raise CtlError(f"{getattr(value, 'message', value)} The redirect to HTTPS could not be switched back "
                           f"on either: {problem.message}",
                           hint="Switch it on by hand: run the Ansible playbook for this server.") from None
        finally:
            self._lock.__exit__(kind, value, traceback)
        return False
