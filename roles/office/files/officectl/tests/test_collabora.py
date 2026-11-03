"""Collabora's own settings: the few values officectl keeps, and which Nextclouds may use it."""

import pytest

from officectl.core import collabora
from serverctl.errors import CtlError


def hosts_of(config):
    return collabora.allowed_hosts(collabora.read(config.coolwsd_config))


def test_the_settings_say_collabora_is_behind_a_proxy(config):
    assert collabora.configure(config, [])
    text = config.coolwsd_config.read_text()
    assert "<enable type=\"bool\"" in text and ">false</enable>" in text
    assert ">true</termination>" in text


def test_it_listens_on_the_loopback_only(config):
    collabora.configure(config, [])
    text = config.coolwsd_config.read_text()
    assert ">loopback</listen>" in text and ">IPv4</proto>" in text


def test_it_calls_itself_by_the_name_it_is_served_under(config):
    collabora.configure(config, [])
    assert ">office.example.nl</server_name>" in config.coolwsd_config.read_text()


def test_only_the_listed_nextclouds_may_use_it(config):
    collabora.configure(config, [])
    assert 'mode="groups"' in config.coolwsd_config.read_text()


def test_a_nextcloud_is_listed_with_its_dots_escaped(config):
    r"""Collabora reads the host as a regular expression, where a dot stands for any character."""
    collabora.configure(config, ["acme.office.example.nl"])
    assert hosts_of(config) == [r"https://acme\.office\.example\.nl:443"]


def test_every_nextcloud_is_listed(config):
    collabora.configure(config, ["acme.office.example.nl", "shop.office.example.nl"])
    assert len(hosts_of(config)) == 2


def test_one_that_is_gone_is_taken_off_the_list(config):
    collabora.configure(config, ["acme.office.example.nl", "shop.office.example.nl"])
    assert collabora.configure(config, ["acme.office.example.nl"])
    assert hosts_of(config) == [r"https://acme\.office\.example\.nl:443"]


def test_the_list_is_the_one_it_is_given(config):
    """A host somebody added by hand is not kept: officectl keeps the list, and says so."""
    collabora.configure(config, ["acme.office.example.nl"])
    collabora.configure(config, [])
    assert hosts_of(config) == []


def test_running_it_again_changes_nothing(config):
    collabora.configure(config, ["acme.office.example.nl"])
    before = config.coolwsd_config.read_text()
    assert not collabora.configure(config, ["acme.office.example.nl"])
    assert config.coolwsd_config.read_text() == before


def test_the_order_of_the_nextclouds_does_not_matter(config):
    collabora.configure(config, ["acme.office.example.nl", "shop.office.example.nl"])
    assert not collabora.configure(config, ["shop.office.example.nl", "acme.office.example.nl"])


def test_collabora_s_own_comments_and_examples_are_left_where_they_are(config):
    collabora.configure(config, ["acme.office.example.nl"])
    text = config.coolwsd_config.read_text()
    assert "scheme://hostname:port" in text
    assert 'More "group"s possible here' in text
    assert "nxml-child-indent" in text.splitlines()[0]


def test_nothing_else_in_the_file_changes(config):
    before = config.coolwsd_config.read_text()
    collabora.configure(config, ["acme.office.example.nl"])
    after = config.coolwsd_config.read_text()
    assert before.count("<") - 4 <= after.count("<")  # the group and host that were added
    assert "<filesystem allow=\"false\"/>" in after


def test_a_setting_the_file_has_no_line_for_is_added(config, coolwsd_config):
    coolwsd_config.write_text("<config>\n\t<ssl>\n\t\t<enable>true</enable>\n\t</ssl>\n</config>\n")
    collabora.configure(config, [])
    text = coolwsd_config.read_text()
    assert ">loopback</listen>" in text and ">office.example.nl</server_name>" in text
    assert ">true</termination>" in text


def test_the_file_stays_valid_xml_whatever_is_added(config, coolwsd_config):
    coolwsd_config.write_text("<config>\n</config>\n")
    collabora.configure(config, ["acme.office.example.nl"])
    assert hosts_of(config) == [r"https://acme\.office\.example\.nl:443"]


def test_settings_that_are_not_xml_are_not_touched(config, coolwsd_config):
    coolwsd_config.write_text("<config>\n\t<ssl>\n")
    with pytest.raises(CtlError) as refused:
        collabora.configure(config, [])
    assert "isn't valid XML" in refused.value.message
    assert coolwsd_config.read_text() == "<config>\n\t<ssl>\n"


def test_settings_that_are_not_there_are_reported(config, coolwsd_config):
    coolwsd_config.unlink()
    with pytest.raises(CtlError) as refused:
        collabora.configure(config, [])
    assert "no Collabora settings" in refused.value.message


def test_the_file_keeps_the_mode_it_had(config):
    config.coolwsd_config.chmod(0o640)
    collabora.configure(config, ["acme.office.example.nl"])
    assert config.coolwsd_config.stat().st_mode & 0o777 == 0o640


def test_a_nextcloud_officectl_did_not_make_keeps_being_let_in(config):
    """An older Nextcloud, set up by hand at a name of its own, is named in the settings."""
    from dataclasses import replace
    settings = replace(config, other_nextclouds="nc.example.nl")
    collabora.configure(settings, ["acme.office.example.nl"])
    assert hosts_of(settings) == [r"https://acme\.office\.example\.nl:443", r"https://nc\.example\.nl:443"]


def test_several_of_them_are_read_from_one_line(config):
    from dataclasses import replace
    settings = replace(config, other_nextclouds=" nc.example.nl , cloud.example.com ")
    assert settings.other_hosts() == ["nc.example.nl", "cloud.example.com"]
    collabora.configure(settings, [])
    assert len(hosts_of(settings)) == 2


def test_taking_the_last_nextcloud_away_leaves_the_one_officectl_did_not_make(config):
    from dataclasses import replace
    settings = replace(config, other_nextclouds="nc.example.nl")
    collabora.configure(settings, ["acme.office.example.nl"])
    collabora.configure(settings, [])
    assert hosts_of(settings) == [r"https://nc\.example\.nl:443"]
