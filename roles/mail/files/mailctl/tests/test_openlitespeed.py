import os
import stat

import pytest

from mailctl.core import openlitespeed
from mailctl.core.errors import MailctlError
from mailctl.core.openlitespeed import Member, Template, VirtualHost

# Laid out like WebAdmin writes it, with the listeners of a server set up by hand.
HTTPD_CONFIG = """\
serverName                web01
user                      nobody
group                     nogroup

extprocessor lsphp {
  type                    lsapi
  address                 uds://tmp/lshttpd/lsphp.sock
  path                    lsphp83/bin/lsphp
}

scripthandler  {
  add                     lsapi:lsphp php
}

rewrite  {
  enable                  1
  # Braces in rules don't open or close blocks.
  rules                   <<<END_rules
RewriteRule ^/old/(.{1,3})$ /new/$1 [R=301,L]
RewriteRule ^/x}$ /y [L]
  END_rules
}

virtualhost shop {
  vhRoot                  /var/www/shop/
  configFile              $SERVER_ROOT/conf/vhosts/shop/vhconf.conf
}

listener HTTP {
  address                 *:80
  secure                  0
  map                     shop shop.example.nl
}

listener HTTPS {
  address                 *:443
  secure                  1
  keyFile                 /etc/letsencrypt/live/web01.example.nl/privkey.pem
  certFile                /etc/letsencrypt/live/web01.example.nl/fullchain.pem
  map                     shop shop.example.nl
}

listener HTTPS6{
  address                 [::]:443
  secure                  1
}

listener Admin {
  address                 *:7080
  secure                  1
}

vhTemplate centralConfigLog {
  templateFile            conf/templates/ccl.conf
  listeners               HTTP
}
"""

HTTPS = Template("mailautodiscover", "conf/templates/mailautodiscover.conf", ("HTTP", "HTTPS", "HTTPS6"), "Managed")
HTTP_ONLY = Template("mailautodiscover-http", "conf/templates/mailautodiscover-http.conf", ("HTTP",), "Managed, HTTP")
MEMBER = Member("autodiscover.example.nl", "autodiscover.example.nl", ("autoconfig.example.nl",))
WEBMAIL = VirtualHost("webmail.example.nl", "/var/www/webmail/", "/etc/mailctl/webmail/webmail.example.nl.conf",
                      "Managed by mailctl")


def lines(text=HTTPD_CONFIG):
    return text.splitlines()


def test_blocks_skip_braces_in_multi_line_values():
    found = [(block.kind, block.name, block.depth) for block in openlitespeed.blocks(lines())]

    assert found == [
        ("extprocessor", "lsphp", 0), ("scripthandler", "", 0), ("rewrite", "", 0), ("virtualhost", "shop", 0),
        ("listener", "HTTP", 0), ("listener", "HTTPS", 0), ("listener", "HTTPS6", 0), ("listener", "Admin", 0),
        ("vhtemplate", "centralConfigLog", 0),
    ]


@pytest.mark.parametrize("text", ["listener HTTP {\n  address *:80\n", "}\n", "rewrite {\n  rules <<<END\n}\n"])
def test_a_config_mailctl_cant_follow_is_an_error(text):
    with pytest.raises(MailctlError, match="can't read OpenLiteSpeed's config"):
        openlitespeed.blocks(lines(text))


def test_value_reads_a_blocks_own_setting():
    config = lines()
    https = next(block for block in openlitespeed.blocks(config) if block.name == "HTTPS")

    assert openlitespeed.value(config, https, "address") == "*:443"
    assert openlitespeed.value(config, https, "SECURE") == "1"
    assert openlitespeed.value(config, https, "missing") is None


def test_listeners_are_found_by_port_and_encryption():
    assert openlitespeed.listeners(lines()) == (["HTTP"], ["HTTPS", "HTTPS6"])


def test_with_member_adds_the_template_and_the_member():
    config = openlitespeed.with_member(lines(), MEMBER, HTTPS)

    assert config[:len(lines())] == lines()
    assert "\n".join(config[len(lines()):]) == """
vhTemplate mailautodiscover {
  templateFile            conf/templates/mailautodiscover.conf
  listeners               HTTP, HTTPS, HTTPS6
  note                    Managed

  member autodiscover.example.nl {
    vhDomain              autodiscover.example.nl
    vhAliases             autoconfig.example.nl
  }
}"""
    assert openlitespeed.members(config, "mailautodiscover") == ["autodiscover.example.nl"]


def test_with_member_changes_nothing_the_second_time():
    config = openlitespeed.with_member(lines(), MEMBER, HTTPS)

    assert openlitespeed.with_member(config, MEMBER, HTTPS) == config


def test_with_member_moves_the_member_between_templates():
    waiting = openlitespeed.with_member(lines(), MEMBER, HTTP_ONLY, (HTTPS,))
    other = Member("autodiscover.other.nl", "autodiscover.other.nl")
    waiting = openlitespeed.with_member(waiting, other, HTTP_ONLY, (HTTPS,))

    live = openlitespeed.with_member(waiting, MEMBER, HTTPS, (HTTP_ONLY,))

    assert openlitespeed.members(live, "mailautodiscover") == ["autodiscover.example.nl"]
    assert openlitespeed.members(live, "mailautodiscover-http") == ["autodiscover.other.nl"]
    assert "\n\n\n" not in "\n".join(live)


def test_with_member_brings_the_templates_listeners_up_to_date():
    config = openlitespeed.with_member(lines(), MEMBER, HTTP_ONLY)
    renamed = Template(HTTP_ONLY.name, HTTP_ONLY.file, ("Web", "Web6"), HTTP_ONLY.note)

    config = openlitespeed.with_member(config, MEMBER, renamed)

    template = next(block for block in openlitespeed.blocks(config) if block.name == HTTP_ONLY.name)
    assert openlitespeed.value(config, template, "listeners") == "Web, Web6"
    assert sum(line.strip().startswith("listeners") for line in config[template.start:template.end]) == 1


def test_without_member_leaves_other_members_and_templates():
    config = openlitespeed.with_member(lines(), MEMBER, HTTPS)
    config = openlitespeed.with_member(config, Member("autodiscover.other.nl", "autodiscover.other.nl"), HTTPS)

    config = openlitespeed.without_member(config, MEMBER.name, HTTPS.name)

    assert openlitespeed.members(config, HTTPS.name) == ["autodiscover.other.nl"]
    assert openlitespeed.without_member(config, "autodiscover.gone.nl", "missing-template") == config


def test_write_replaces_the_config_keeping_its_mode_and_the_previous_version(tmp_path):
    path = openlitespeed.config_file(tmp_path)
    path.parent.mkdir()
    path.write_text(HTTPD_CONFIG)
    path.chmod(0o640)

    openlitespeed.write(tmp_path, ["serverName web01"])

    assert path.read_text() == "serverName web01\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.with_name("httpd_config.conf.mailctl.bak").read_text() == HTTPD_CONFIG
    assert sorted(os.listdir(path.parent)) == ["httpd_config.conf", "httpd_config.conf.mailctl.bak"]


def test_read_explains_a_missing_config(tmp_path):
    with pytest.raises(MailctlError, match="There is no OpenLiteSpeed config at"):
        openlitespeed.read(tmp_path)


def test_blocks_follow_openlitespeeds_own_reading():
    config = lines("""\
rewrite  {
  rules                   <<<END_RULES
RewriteRule ^/a{2}$ /b [L]
  end_rules
} # the end of rewrite
listener HTTP {
  address\t*:80
}
""")

    assert [(block.kind, block.name) for block in openlitespeed.blocks(config)] == [("rewrite", ""), ("listener", "HTTP")]
    assert openlitespeed.listeners(config) == (["HTTP"], [])


def test_a_multi_line_template_setting_is_replaced_whole():
    config = lines("""\
vhTemplate mailautodiscover {
  templateFile            conf/templates/mailautodiscover.conf
  listeners               HTTP
  note                    <<<END_note
Written by hand
  in two lines
  END_note

  member autodiscover.example.nl {
    vhDomain              autodiscover.example.nl
    note                  inside the member
  }
}
""")

    updated = openlitespeed.with_member(config, MEMBER, HTTPS)

    text = "\n".join(updated)
    assert "Written by hand" not in text and "END_note" not in text
    assert "    note                  inside the member" in text
    template = openlitespeed.blocks(updated)[0]
    assert openlitespeed.value(updated, template, "note") == "Managed"
    assert openlitespeed.value(updated, template, "listeners") == "HTTP, HTTPS, HTTPS6"


def test_write_doesnt_follow_links_planted_in_the_config_directory(tmp_path):
    path = openlitespeed.config_file(tmp_path)
    path.parent.mkdir()
    path.write_text(HTTPD_CONFIG)
    victim = tmp_path / "authorized_keys"
    victim.write_text("ssh-ed25519 AAAA admin\n")
    path.with_name("httpd_config.conf.mailctl.bak").symlink_to(victim)

    openlitespeed.write(tmp_path, ["serverName web01"])

    assert victim.read_text() == "ssh-ed25519 AAAA admin\n"
    backup = path.with_name("httpd_config.conf.mailctl.bak")
    assert not backup.is_symlink() and backup.read_text() == HTTPD_CONFIG


def test_a_config_that_is_a_symbolic_link_is_refused(tmp_path):
    (tmp_path / "elsewhere.conf").write_text(HTTPD_CONFIG)
    path = openlitespeed.config_file(tmp_path)
    path.parent.mkdir()
    path.symlink_to(tmp_path / "elsewhere.conf")

    with pytest.raises(MailctlError, match="is a symbolic link"):
        openlitespeed.read(tmp_path)


def test_with_virtual_host_adds_it_like_webadmin_does():
    config = openlitespeed.with_virtual_host(lines(), WEBMAIL)

    assert config[:len(lines())] == lines()
    assert "\n".join(config[len(lines()):]) == """
virtualhost webmail.example.nl {
  vhRoot                  /var/www/webmail/
  configFile              /etc/mailctl/webmail/webmail.example.nl.conf
  allowSymbolLink         0
  enableScript            0
  restrained              1
  note                    Managed by mailctl
}"""
    assert openlitespeed.virtual_hosts(config) == {"shop": None, "webmail.example.nl": "Managed by mailctl"}


def test_with_virtual_host_changes_nothing_the_second_time_and_brings_settings_up_to_date():
    config = openlitespeed.with_virtual_host(lines(), WEBMAIL)
    assert openlitespeed.with_virtual_host(config, WEBMAIL) == config

    moved = VirtualHost(WEBMAIL.name, "/srv/webmail/", WEBMAIL.config_file, WEBMAIL.note)
    config = openlitespeed.with_virtual_host(config, moved)

    host = next(block for block in openlitespeed.blocks(config) if block.name == WEBMAIL.name)
    assert openlitespeed.value(config, host, "vhRoot") == "/srv/webmail/"
    assert sum(line.strip().startswith("vhRoot") for line in config[host.start:host.end]) == 1


def test_without_virtual_host_leaves_the_others():
    config = openlitespeed.with_virtual_host(lines(), WEBMAIL)

    config = openlitespeed.without_virtual_host(config, WEBMAIL.name)

    assert config == lines()
    assert openlitespeed.without_virtual_host(config, "gone") == config


def test_maps_are_the_names_each_listener_gives_its_virtual_hosts():
    config = lines().copy()
    config[config.index("  map                     shop shop.example.nl")] = (
        "  map                     shop shop.example.nl, www.shop.example.nl"
    )

    assert openlitespeed.maps(config, "HTTP") == {"shop": ["shop.example.nl", "www.shop.example.nl"]}
    assert openlitespeed.maps(config, "HTTPS6") == {}
    assert openlitespeed.maps(config, "missing") == {}


def test_with_map_adds_the_name_once_and_without_map_takes_it_away():
    config = openlitespeed.with_map(lines(), "HTTPS6", WEBMAIL.name, WEBMAIL.name)

    assert openlitespeed.maps(config, "HTTPS6") == {WEBMAIL.name: [WEBMAIL.name]}
    listener = next(block for block in openlitespeed.blocks(config) if block.name == "HTTPS6")
    assert config[listener.end - 1] == "  map                     webmail.example.nl webmail.example.nl"
    assert openlitespeed.with_map(config, "HTTPS6", WEBMAIL.name, WEBMAIL.name) == config

    assert openlitespeed.without_map(config, "HTTPS6", WEBMAIL.name) == lines()
    assert openlitespeed.maps(openlitespeed.without_map(lines(), "HTTP", "shop"), "HTTP") == {}
