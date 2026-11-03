"""The PHP that autoconfig.DOMAIN and autodiscover.DOMAIN serve, run the way a mail program asks it."""

import http.client
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[2] / "mailautodiscover"
OUTLOOK = "http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a"
MOBILE = "http://schemas.microsoft.com/exchange/autodiscover/mobilesync/responseschema/2006"
DOMAIN = "example.nl"


def request_for(schema: str, address: str = "info@example.nl") -> bytes:
    return (f"<Autodiscover><Request><EMailAddress>{address}</EMailAddress>"
            f"<AcceptableResponseSchema>{schema}</AcceptableResponseSchema></Request></Autodiscover>").encode()


@pytest.fixture(scope="module")
def site():
    """PHP's own web server on the site's files: the scripts read the Host header and the request body, which a
    command line can't give them."""
    if not shutil.which("php"):
        pytest.skip("php isn't installed")
    with socket.socket() as found:
        found.bind(("127.0.0.1", 0))
        port = found.getsockname()[1]
    server = subprocess.Popen(["php", "-S", f"127.0.0.1:{port}", "-t", str(SITE)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        server.kill()
        pytest.skip("php's web server didn't start")
    yield f"http://127.0.0.1:{port}"
    server.terminate()
    server.wait(timeout=5)


def ask(site: str, schema: str, domain: str = DOMAIN) -> tuple[int, str]:
    """Posts a request as a mail program does, to the name the program asked for."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(f"{site}/autodiscover.php", data=request_for(schema), method="POST")
    request.add_header("Host", f"autodiscover.{domain}")
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as answer:
        return answer.code, answer.read().decode()


def fetch(site: str, path: str, domain: str = DOMAIN) -> tuple[int, str]:
    """Asks for a page without following a redirect, and gives back where it was sent."""
    host, port = site.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        connection.request("GET", path, headers={"Host": f"autodiscover.{domain}"})
        answer = connection.getresponse()
        answer.read()
        return answer.status, answer.getheader("Location") or ""
    finally:
        connection.close()


def test_outlook_gets_the_imap_and_sending_servers(site):
    status, body = ask(site, OUTLOOK)

    assert status == 200
    assert f"<Server>mail.{DOMAIN}</Server>" in body
    assert "<Type>IMAP</Type>" in body and "<Type>SMTP</Type>" in body
    assert "<LoginName>info@example.nl</LoginName>" in body


def test_a_phone_gets_the_activesync_address_of_the_webmail_site(site):
    """ActiveSync is served by webmail.DOMAIN; mail.DOMAIN has no web site at all, so a phone left to guess it
    would ask a name that answers 404."""
    status, body = ask(site, MOBILE)

    assert status == 200
    assert MOBILE in body
    assert f"<Url>https://webmail.{DOMAIN}/Microsoft-Server-ActiveSync</Url>" in body
    assert "<EMailAddress>info@example.nl</EMailAddress>" in body


def test_a_request_for_a_name_that_isnt_a_domain_is_refused(site):
    status, _ = ask(site, MOBILE, domain="..")

    assert status == 404


def test_a_schema_we_dont_know_is_left_alone(site):
    status, _ = ask(site, "http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2020z")

    assert status == 404


def test_a_plain_http_request_is_sent_to_the_https_address(site):
    """Outlook asks over plain HTTP first, with nothing in the request. Answered with the settings, it has no
    login name to go on and tries the part before the @, which fails and gets the customer banned; sent to the
    HTTPS address, it posts the address there and gets the login name back."""
    status, location = fetch(site, "/autodiscover.php")

    assert status == 302
    assert location == f"https://autodiscover.{DOMAIN}/autodiscover/autodiscover.xml"
