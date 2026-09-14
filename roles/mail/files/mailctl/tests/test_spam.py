from pathlib import Path

import pytest

from mailctl.core import spam
from mailctl.core.errors import MailctlError
from mailctl.core.spam import Preference, Scope

SERVER = Scope("$GLOBAL", "the whole server")
DOMAIN = Scope("%example.nl", "domain example.nl")
ACCOUNT = Scope("info@example.nl", "info@example.nl")


def stored(database):
    return database.rows("SELECT username, preference, value FROM spamassassin.userpref ORDER BY prefid")


def test_scope_chain_runs_from_the_server_down_to_the_target():
    assert spam.scope_chain("info@example.nl") == [SERVER, DOMAIN, ACCOUNT]
    assert spam.scope_chain("example.nl") == [SERVER, DOMAIN]
    assert spam.scope_chain("server") == [SERVER]


def test_known_setting_normalises_the_name():
    assert spam.known_setting(" Required_Score ") == "required_score"

    with pytest.raises(MailctlError, match="Unknown spam setting 'rewrite_header'"):
        spam.known_setting("rewrite_header")


@pytest.mark.parametrize(
    "setting, value, expected",
    [("required_score", " 4.5 ", "4.5"), ("required_score", "-2", "-2"), ("welcomelist_from", " *@Partner.NL ", "*@partner.nl")],
)
def test_normalise_gives_the_value_to_store(setting, value, expected):
    assert spam.normalise(setting, value) == expected


@pytest.mark.parametrize(
    "setting, value, problem",
    [
        ("required_score", "high", "isn't a number"),
        ("required_score", "nan", "isn't a number"),
        ("required_score", "1e3", "isn't a number"),
        ("blocklist_from", "not a pattern", "isn't an address or pattern"),
        ("blocklist_from", "spam@" + "x" * 100 + ".nl", "at most 100 characters"),
        ("rewrite_header", "Subject", "Unknown spam setting"),
    ],
)
def test_normalise_rejects_bad_input(setting, value, problem):
    with pytest.raises(MailctlError, match=problem):
        spam.normalise(setting, value)


def test_set_value_replaces_a_single_value_and_says_whether_it_changed(database):
    assert spam.set_value(database, DOMAIN, "required_score", "5")
    assert spam.set_value(database, DOMAIN, "required_score", "4.5")
    assert not spam.set_value(database, DOMAIN, "required_score", "4.5")

    assert stored(database) == [{"username": "%example.nl", "preference": "required_score", "value": "4.5"}]


def test_set_value_adds_to_a_list_once(database):
    assert spam.set_value(database, ACCOUNT, "welcomelist_from", "*@partner.nl")
    assert not spam.set_value(database, ACCOUNT, "welcomelist_from", "*@partner.nl")
    assert spam.set_value(database, ACCOUNT, "welcomelist_from", "boss@bank.nl")

    assert [row["value"] for row in stored(database)] == ["*@partner.nl", "boss@bank.nl"]


def test_set_value_numbers_rows_after_the_highest_prefid(database):
    database.execute("INSERT INTO spamassassin.userpref VALUES ('$GLOBAL', 'report_safe', '0', 41)")

    spam.set_value(database, SERVER, "required_score", "5")

    assert database.value("SELECT prefid FROM spamassassin.userpref WHERE preference = 'required_score'") == 42


def test_show_marks_overridden_values_and_keeps_every_list_value(database):
    spam.set_value(database, ACCOUNT, "required_score", "4")
    spam.set_value(database, SERVER, "required_score", "5")
    spam.set_value(database, ACCOUNT, "welcomelist_from", "*@partner.nl")
    spam.set_value(database, SERVER, "welcomelist_from", "*@bank.nl")
    spam.set_value(database, Scope("%other.nl", "domain other.nl"), "required_score", "9")
    database.execute("INSERT INTO spamassassin.userpref VALUES ('$GLOBAL', 'report_safe', '0', 99)")

    assert spam.show(database, spam.scope_chain("info@example.nl")) == [
        Preference("report_safe", "0", SERVER, in_effect=True),
        Preference("required_score", "5", SERVER, in_effect=False),
        Preference("required_score", "4", ACCOUNT, in_effect=True),
        Preference("welcomelist_from", "*@bank.nl", SERVER, in_effect=True),
        Preference("welcomelist_from", "*@partner.nl", ACCOUNT, in_effect=True),
    ]


def test_show_places_hand_made_rows_that_the_database_matches_in_another_case_or_with_accents(database):
    database.execute(
        "INSERT INTO spamassassin.userpref VALUES ('Info@Example.nl', 'required_score', '3', 1),"
        " ('$global', 'required_score', '6', 2), ('%exämple.nl', 'required_score', '4', 3)"
    )

    assert spam.show(database, spam.scope_chain("info@example.nl")) == [
        Preference("required_score", "6", SERVER, in_effect=False),
        Preference("required_score", "4", DOMAIN, in_effect=False),
        Preference("required_score", "3", ACCOUNT, in_effect=True),
    ]


def test_show_accepts_a_hand_made_row_that_starts_with_an_invisible_character(database):
    # Under utf8mb4_unicode_ci, a zero-width space is ignored when comparing, so the row matches $GLOBAL.
    database.execute("ALTER TABLE spamassassin.userpref MODIFY username varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL")
    database.execute("INSERT INTO spamassassin.userpref VALUES (%s, 'required_score', '6', 1)", "\u200b$GLOBAL")

    assert spam.show(database, spam.scope_chain("server")) == [Preference("required_score", "6", SERVER, in_effect=True)]


def test_show_for_the_server_leaves_out_domain_and_account_settings(database):
    spam.set_value(database, SERVER, "required_score", "5")
    spam.set_value(database, DOMAIN, "required_score", "4")

    assert spam.show(database, spam.scope_chain("server")) == [Preference("required_score", "5", SERVER, in_effect=True)]


def test_unset_value_removes_one_list_value_or_the_whole_setting(database):
    spam.set_value(database, ACCOUNT, "welcomelist_from", "*@a.nl")
    spam.set_value(database, ACCOUNT, "welcomelist_from", "*@b.nl")
    spam.set_value(database, ACCOUNT, "required_score", "4")

    assert spam.unset_value(database, ACCOUNT, "welcomelist_from", "*@a.nl") == 1
    assert spam.unset_value(database, ACCOUNT, "required_score") == 1
    assert spam.unset_value(database, ACCOUNT, "required_score") == 0
    assert [row["value"] for row in stored(database)] == ["*@b.nl"]


def test_count_values(database):
    spam.set_value(database, ACCOUNT, "welcomelist_from", "*@a.nl")
    spam.set_value(database, ACCOUNT, "welcomelist_from", "*@b.nl")

    assert spam.count_values(database, ACCOUNT, "welcomelist_from") == 2
    assert spam.count_values(database, DOMAIN, "welcomelist_from") == 0


def test_spamassassin_reads_the_settings_from_the_server_down_to_the_account(database):
    """The query SpamAssassin is configured with must order the rows the way show() assumes, whatever the collation."""
    # Under utf8mb4_unicode_ci, sorting on the username puts the domain's settings before the server's.
    database.execute("ALTER TABLE spamassassin.userpref MODIFY username varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL")
    template = Path(__file__).resolve().parents[3] / "templates" / "spamassassin-sql.cf.j2"
    query = next(line for line in template.read_text().splitlines() if line.startswith("user_scores_sql_custom_query"))
    query = query.split(None, 1)[1].replace("_TABLE_", "spamassassin.userpref")
    query = query.replace("_USERNAME_", "'info@example.nl'").replace("_DOMAIN_", "'example.nl'")
    spam.set_value(database, ACCOUNT, "required_score", "3")
    spam.set_value(database, SERVER, "required_score", "5")
    spam.set_value(database, DOMAIN, "required_score", "4")
    by_username = database.rows("SELECT value FROM spamassassin.userpref ORDER BY username")

    assert [row["value"] for row in by_username] != ["5", "4", "3"]
    assert [row["value"] for row in database.rows(query)] == ["5", "4", "3"]


def test_forget_removes_the_settings_of_one_username(database):
    spam.set_value(database, ACCOUNT, "required_score", "4")
    spam.set_value(database, DOMAIN, "required_score", "4")

    spam.forget(database, spam.domain_username("example.nl"))

    assert [row["username"] for row in stored(database)] == ["info@example.nl"]
