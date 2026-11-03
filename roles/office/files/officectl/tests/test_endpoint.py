"""Collabora's own site in OpenLiteSpeed: the only way in to coolwsd."""

import re

import pytest

from officectl.core import endpoint
from serverctl import openlitespeed
from serverctl.errors import CtlError


def lines(config):
    return openlitespeed.read(config.ols_root)


def test_publishing_it_adds_the_virtual_host_and_the_name_on_port_80(config):
    assert endpoint.publish(config, False)
    found = lines(config)
    assert openlitespeed.virtual_hosts(found)["office.example.nl"] == endpoint.NOTE
    assert openlitespeed.maps(found, "http") == {"office.example.nl": ["office.example.nl"]}


def test_without_a_certificate_it_is_not_served_over_https(config):
    endpoint.publish(config, False)
    assert openlitespeed.maps(lines(config), "https") == {}


def test_with_a_certificate_it_is_served_over_https_too(config):
    endpoint.publish(config, True)
    assert openlitespeed.maps(lines(config), "https") == {"office.example.nl": ["office.example.nl"]}


def test_publishing_it_again_changes_nothing(config):
    endpoint.publish(config, True)
    assert not endpoint.publish(config, True)


def test_it_runs_no_scripts_of_its_own(config):
    endpoint.publish(config, False)
    text = "\n".join(lines(config))
    assert re.search(r"enableScript\s+0", text)
    assert re.search(r"setUIDMode\s+0", text)  # it has no user of its own


def test_its_settings_hand_everything_to_coolwsd(config):
    text = endpoint.settings(config, False)
    assert "type                    proxy" in text and "127.0.0.1:9980" in text
    assert "handler                 coolwsd" in text


def test_its_settings_let_a_document_be_edited_over_a_websocket(config):
    assert "websocket /cool/" in endpoint.settings(config, False)


def test_its_settings_leave_the_challenge_folder_on_plain_http(config):
    text = endpoint.settings(config, False)
    assert "context /.well-known/acme-challenge/" in text
    assert "!^/\\.well-known/acme-challenge/" in text


def test_its_settings_point_at_the_certificate_once_there_is_one(config):
    assert "vhssl" not in endpoint.settings(config, False)
    with_certificate = endpoint.settings(config, True)
    assert f"{config.letsencrypt_dir}/live/office.example.nl/fullchain.pem" in with_certificate


def test_the_settings_are_written_where_webadmin_keeps_them(config):
    endpoint.publish(config, False)
    assert endpoint.site_file(config) == config.ols_root / "conf/vhosts/office.example.nl/vhconf.conf"
    assert "Collabora Online at office.example.nl" in endpoint.site_file(config).read_text()


def test_a_site_of_that_name_somebody_else_made_is_left_alone(config):
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text() + "\nvirtualhost office.example.nl {\n  vhRoot /var/www/\n}\n")
    with pytest.raises(CtlError) as refused:
        endpoint.publish(config, False)
    assert "officectl didn't make" in refused.value.message


def test_another_site_that_answers_for_the_name_is_reported(config):
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text().replace("  secure                  0\n",
                                             "  secure                  0\n  map other office.example.nl\n"))
    with pytest.raises(CtlError) as refused:
        endpoint.publish(config, False)
    assert "already answers for office.example.nl" in refused.value.message


def test_a_server_without_a_listener_on_port_80_is_reported(config):
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text("serverName                web03\n")
    with pytest.raises(CtlError) as refused:
        endpoint.publish(config, False)
    assert "no HTTP listener" in refused.value.message
