"""Filters as they come from another server, and as webmail keeps them."""

import json

import pytest

from mailctl.core import sogofilters
from mailctl.core.sieveparse import Unreadable, parse

ROUNDCUBE = """require ["fileinto","mailbox","imap4flags"];
# rule:[Family]
if anyof (header :contains "from" "oma@example.nl", header :is "subject" "Family")
{
    fileinto :create "Family";
    stop;
}
# rule:[Bills]
if allof (address :is ["to","cc"] "bills@example.nl", not header :contains "subject" "reminder")
{
    fileinto "Bills";
    addflag "\\\\Seen";
}
"""


def translated(script=ROUNDCUBE, name="roundcube"):
    return sogofilters.translate(script, name)


def test_a_rule_becomes_a_filter_webmail_can_show():
    filters, left = translated()

    assert left == []
    assert filters[0] == {
        "name": "Family", "match": "any", "active": 1,
        "rules": [{"field": "from", "operator": "contains", "value": "oma@example.nl"},
                  {"field": "subject", "operator": "is", "value": "Family"}],
        "actions": [{"method": "fileinto", "argument": "Family"}, {"method": "stop"}],
    }


def test_to_and_cc_together_are_one_field_and_not_is_one_operator():
    rules = translated()[0][1]["rules"]

    assert rules[0] == {"field": "to_or_cc", "operator": "is", "value": "bills@example.nl"}
    assert rules[1] == {"field": "subject", "operator": "contains_not", "value": "reminder"}
    assert translated()[0][1]["actions"][1] == {"method": "addflag", "argument": "\\Seen"}


def test_a_rule_without_a_name_is_named_after_the_script_it_came_from():
    filters, _ = translated('if header :is "subject" "x" { discard; }', "old-server")

    assert filters[0]["name"] == "old-server 1"


@pytest.mark.parametrize("script, reason", [
    ('if header :is "x-spam-flag" "YES" { discard; }', "the header x-spam-flag"),
    ('if size :over 100K { discard; }', "the test size"),
    ('if header :is "subject" "x" { vacation "away"; }', "the action vacation"),
    ('if exists "references" { discard; }', "the test exists"),
    ('if header :is ["subject", "from"] "x" { discard; }', "2 headers"),
])
def test_what_webmails_filters_cant_express_is_reported_and_left_alone(script, reason):
    filters, left = translated(script, "old")

    assert filters == []
    assert reason in left[0], left


def test_the_part_of_a_script_that_does_translate_still_comes_over():
    """An else has no place in webmail's filters, but the rule before it does."""
    filters, left = translated('if header :is "subject" "x" { fileinto "a"; } else { discard; }', "old")

    assert [one["name"] for one in filters] == ["old 1"]
    assert "else" in left[0]


def test_a_script_that_isnt_the_usual_shape_is_reported_whole():
    filters, left = translated("if header :is {oops", "old")

    assert filters == [] and "old:" in left[0]


def test_filters_are_written_back_as_the_script_dovecot_runs():
    filters, _ = translated()

    script = sogofilters.render(filters)

    assert 'require ["fileinto", "imap4flags", "mailbox"];' in script
    assert 'if anyof (header :contains "From" "oma@example.nl", header :is "Subject" "Family")' in script
    assert 'fileinto :create "Family";' in script
    assert 'if allof (header :is ["To", "Cc"] "bills@example.nl", not header :contains "Subject" "reminder")' in script


def test_rendering_and_reading_again_gives_the_same_filters():
    filters, _ = translated()

    again, left = sogofilters.translate(sogofilters.render(filters), "again")

    assert left == []
    assert [one["rules"] for one in again] == [one["rules"] for one in filters]
    assert [one["actions"] for one in again] == [one["actions"] for one in filters]


def test_a_filter_that_is_switched_off_is_left_out_of_the_script():
    filters, _ = translated()
    filters[0]["active"] = 0

    assert "Family" not in sogofilters.render(filters)


def test_the_parser_says_when_a_script_stops_in_the_middle():
    with pytest.raises(Unreadable):
        parse('if header :is "subject" "x" {')


def test_webmails_filters_are_kept_beside_its_other_settings(database):
    database.execute("INSERT INTO sogo.sogo_user_profile (c_uid, c_defaults) VALUES (%s, %s)",
                     "info@example.nl", json.dumps({"SOGoMailLabelsColors": {"$label1": ["Belangrijk", "#FF0000"]}}))
    filters, _ = translated()

    sogofilters.write(database, "info@example.nl", filters)

    stored = json.loads(database.value("SELECT c_defaults FROM sogo.sogo_user_profile WHERE c_uid = %s",
                                       "info@example.nl"))
    assert stored["SOGoMailLabelsColors"] == {"$label1": ["Belangrijk", "#FF0000"]}
    assert sogofilters.read(database, "info@example.nl") == filters


def test_an_account_webmail_has_never_seen_gets_a_profile_of_its_own(database):
    filters, _ = translated()

    sogofilters.write(database, "new@example.nl", filters)

    assert sogofilters.read(database, "new@example.nl") == filters
    assert sogofilters.read(database, "nobody@example.nl") == []


def test_adopt_puts_the_rules_in_webmail_and_writes_the_script_it_runs(database, fake_command):
    doveadm = fake_command("doveadm")

    adopted = sogofilters.adopt(database, "info@example.nl", [("roundcube", ROUNDCUBE)])

    assert [one["name"] for one in adopted.added] == ["Family", "Bills"]
    assert adopted.already == 0 and adopted.left == []
    assert sogofilters.read(database, "info@example.nl") == adopted.added
    assert ["sieve", "put", "-u", "info@example.nl", "sogo"] in [call[:5] for call in doveadm.calls]
    assert ["sieve", "activate", "-u", "info@example.nl", "sogo"] in [call[:5] for call in doveadm.calls]


def test_adopt_leaves_a_filter_webmail_already_has_alone(database, fake_command):
    fake_command("doveadm")
    sogofilters.adopt(database, "info@example.nl", [("roundcube", ROUNDCUBE)])

    adopted = sogofilters.adopt(database, "info@example.nl", [("roundcube", ROUNDCUBE)])

    assert adopted.added == [] and adopted.already == 2
    assert len(sogofilters.read(database, "info@example.nl")) == 2


def test_adopt_writes_nothing_when_no_rule_fits_webmails_filters(database, fake_command):
    doveadm = fake_command("doveadm")

    adopted = sogofilters.adopt(database, "info@example.nl", [("away", 'vacation :days 1 "Away";\n')])

    assert adopted.added == [] and "vacation" in adopted.left[0]
    assert sogofilters.read(database, "info@example.nl") == []
    assert doveadm.calls == []
