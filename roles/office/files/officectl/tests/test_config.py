"""The settings file Ansible writes, and what officectl makes of it."""

import json

import pytest

from officectl import config as configuration
from officectl.config import Config
from serverctl.errors import CtlError


def settings(tmp_path, **values):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hostname": "web03.example.nl", "office_host": "office.example.nl", **values}))
    return path


def test_the_two_settings_without_a_default_are_read(tmp_path):
    found = configuration.load(settings(tmp_path))
    assert (found.hostname, found.office_host) == ("web03.example.nl", "office.example.nl")


def test_a_number_written_as_text_is_a_number(tmp_path):
    """Ansible writes every value in config.json as text unless it is told otherwise."""
    found = configuration.load(settings(tmp_path, coolwsd_port="9980", php_children="12"))
    assert (found.coolwsd_port, found.php_children) == (9980, 12)


def test_a_number_that_is_no_number_is_refused(tmp_path):
    with pytest.raises(CtlError) as refused:
        configuration.load(settings(tmp_path, coolwsd_port="nine"))
    assert "Invalid value for coolwsd_port" in refused.value.message


def test_a_path_is_read_as_a_path(tmp_path):
    found = configuration.load(settings(tmp_path, office_root="/var/www/office"))
    assert found.office_root.name == "office"


def test_a_setting_that_is_missing_is_reported(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hostname": "web03.example.nl"}))
    with pytest.raises(CtlError) as refused:
        configuration.load(path)
    assert "is missing: office_host" in refused.value.message


def test_a_setting_nobody_knows_is_reported(tmp_path):
    with pytest.raises(CtlError) as refused:
        configuration.load(settings(tmp_path, office_colour="blue"))
    assert "Unknown setting" in refused.value.message and "office_colour" in refused.value.message


def test_a_file_that_is_not_there_says_what_writes_it(tmp_path):
    with pytest.raises(CtlError) as refused:
        configuration.load(tmp_path / "nothing.json")
    assert "Run the Ansible playbook" in refused.value.hint


def test_a_file_that_is_not_json_is_reported(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("office_host: office.example.nl\n")
    with pytest.raises(CtlError) as refused:
        configuration.load(path)
    assert "isn't valid JSON" in refused.value.message


def test_a_nextcloud_s_name_user_and_address_come_from_its_slug():
    found = Config(hostname="web03.example.nl", office_host="office.example.nl")
    assert found.user_of("acme") == "ncacme"
    assert found.host_of("acme") == "acme.office.example.nl"
    assert found.docroot_of("ncacme").as_posix() == "/home/ncacme/public_html/www"
    assert found.data_of("ncacme").as_posix() == "/home/ncacme/data"


def test_where_its_php_is_looked_for():
    found = Config(hostname="web03.example.nl", office_host="office.example.nl")
    assert found.lsphp().as_posix() == "/usr/local/lsws/lsphp85/bin/lsphp"
    assert found.php().as_posix() == "/usr/local/lsws/lsphp85/bin/php"
    assert found.php_settings_dir("ncacme").as_posix() == "/usr/local/lsws/lsphp85/etc/php/8.5/ncacme.d"


def test_the_address_a_nextcloud_sends_its_mail_from():
    found = Config(hostname="web03.example.nl", office_host="office.example.nl")
    assert found.sender() == "nextcloud@example.nl"
    assert Config(hostname="web03.example.nl", office_host="office.example.nl",
                  mail_domain="majorlabel.nl").sender() == "nextcloud@majorlabel.nl"
