"""A Nextcloud's own virtual host, and how officectl knows which Nextclouds are on this server."""

import pytest

from officectl.core import tenants
from officectl.core.tenants import Tenant
from serverctl import openlitespeed
from serverctl.errors import CtlError


def test_a_nextcloud_is_named_by_its_slug(config):
    assert tenants.of(config, "acme") == Tenant("acme", "ncacme", "acme.office.example.nl")
    assert tenants.of(config, "acme").url == "https://acme.office.example.nl"


def test_a_name_with_a_dot_in_it_is_refused(config):
    with pytest.raises(CtlError) as refused:
        tenants.slug("acme.office.example.nl", config)
    assert "one word" in refused.value.message


@pytest.mark.parametrize("given", ["Acme", "acme", "acme-2"])
def test_a_name_is_taken_as_it_would_be_written(given, config):
    assert tenants.slug(given, config) == given.lower()


@pytest.mark.parametrize("given", ["-acme", "acme_2", "a" * 30, "acme!", ""])
def test_a_name_that_is_no_name_is_refused(given, config):
    with pytest.raises(CtlError):
        tenants.slug(given, config)


def test_saving_one_adds_its_virtual_host_and_its_name(config):
    assert tenants.save(config, tenants.of(config, "acme"), False)
    found = openlitespeed.read(config.ols_root)
    assert openlitespeed.virtual_hosts(found)["ncacme"] == tenants.NOTE
    assert openlitespeed.maps(found, "http") == {"ncacme": ["acme.office.example.nl"]}
    assert openlitespeed.maps(found, "https") == {}


def test_it_is_served_over_https_once_it_has_a_certificate(config):
    tenants.save(config, tenants.of(config, "acme"), True)
    assert openlitespeed.maps(openlitespeed.read(config.ols_root), "https") == \
        {"ncacme": ["acme.office.example.nl"]}


def test_saving_it_again_changes_nothing(config):
    tenants.save(config, tenants.of(config, "acme"), True)
    assert not tenants.save(config, tenants.of(config, "acme"), True)


def test_the_nextclouds_on_this_server_are_read_back_from_openlitespeed(config):
    tenants.save(config, tenants.of(config, "shop"), False)
    tenants.save(config, tenants.of(config, "acme"), False)
    assert tenants.list_tenants(config) == [tenants.of(config, "acme"), tenants.of(config, "shop")]


def test_one_is_found_by_its_name_its_user_or_the_address_it_is_served_at(config):
    tenants.save(config, tenants.of(config, "acme"), False)
    for given in ("acme", "ncacme", "acme.office.example.nl"):
        assert tenants.find(config, given) == tenants.of(config, "acme")
    assert tenants.find(config, "shop") is None


def test_a_site_somebody_else_made_is_not_one_of_them(config):
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text() + "\nvirtualhost ncacme {\n  vhRoot /home/ncacme/\n}\n")
    assert tenants.list_tenants(config) == []
    clash = tenants.elsewhere(config, tenants.of(config, "acme"), openlitespeed.read(config.ols_root))
    assert "leaves alone" in clash


def test_a_name_another_site_already_answers_for_is_reported(config):
    path = config.ols_root / "conf" / "httpd_config.conf"
    path.write_text(path.read_text().replace(
        "  secure                  0\n", "  secure                  0\n  map other acme.office.example.nl\n"))
    with pytest.raises(CtlError) as refused:
        tenants.save(config, tenants.of(config, "acme"), False)
    assert "already served by other" in refused.value.message


def test_its_settings_give_it_a_php_pool_of_its_own(config):
    text = tenants.settings(config, tenants.of(config, "acme"), False)
    assert "extprocessor ncacme-php" in text
    assert "uds://tmp/lshttpd/ncacme-php.sock" in text
    assert "PHP_LSAPI_CHILDREN=10" in text
    assert f"path                    {config.lsphp()}" in text


def test_its_settings_read_nextcloud_s_own_htaccess(config):
    assert "autoLoadHtaccess        1" in tenants.settings(config, tenants.of(config, "acme"), False)


def test_its_settings_send_visitors_to_https_but_leave_the_challenge_folder(config):
    text = tenants.settings(config, tenants.of(config, "acme"), False)
    assert "RewriteRule .* https://%{HTTP_HOST}%{REQUEST_URI} [L,R=301]" in text
    assert "context /.well-known/acme-challenge/" in text


def test_its_settings_point_at_its_own_certificate_once_there_is_one(config):
    assert "vhssl" not in tenants.settings(config, tenants.of(config, "acme"), False)
    assert f"{config.letsencrypt_dir}/live/acme.office.example.nl/privkey.pem" in \
        tenants.settings(config, tenants.of(config, "acme"), True)


def test_removing_one_takes_its_virtual_host_and_its_name_away(config):
    tenants.save(config, tenants.of(config, "acme"), True)
    assert tenants.remove(config, tenants.of(config, "acme"))
    assert tenants.list_tenants(config) == []
    assert openlitespeed.maps(openlitespeed.read(config.ols_root), "https") == {}


def test_the_address_on_the_error_pages_is_left_out_when_there_is_none(config):
    from dataclasses import replace
    assert "adminEmails" in tenants.settings(config, tenants.of(config, "acme"), False)
    without = replace(config, letsencrypt_email="")
    assert "adminEmails" not in tenants.settings(without, tenants.of(without, "acme"), False)
