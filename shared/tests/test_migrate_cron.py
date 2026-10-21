import os

import pytest

from migrate import migrate

SITES = [
    {"name": "shop.example.nl", "docroot": "/var/www/shop"},
    {"name": "blog.example.nl", "docroot": "/var/www/shop/blog"},
]


def _server(crontabs, failures=None):
    def server(*args, stdin=None):
        assert args[:2] == ("crontab", "-l")
        user = args[3]
        if failures and user in failures:
            raise failures[user]
        if user in crontabs:
            return crontabs[user]
        raise migrate.CommandFailed("crontab failed: no crontab for %s" % user)
    return server


def _passwd(tmp_path, *names):
    path = tmp_path / "passwd"
    path.write_text("".join("%s:x:1000:1000::/home/%s:/bin/bash\n" % (n, n) for n in names))
    return str(path)


def _jobs(tmp_path, crontabs, cron_d=None, notes=None, failures=None, sites=SITES):
    directory = tmp_path / "cron.d"
    directory.mkdir()
    for name, text in (cron_d or {}).items():
        (directory / name).write_text(text)
    return migrate.cron_jobs(_server(crontabs, failures), sites, notes, passwd=_passwd(tmp_path, "shopper"),
                             cron_dir=str(directory))


def test_a_job_is_matched_to_the_site_its_paths_point_into(tmp_path):
    jobs = _jobs(tmp_path, {"shopper": "*/5 * * * * /usr/bin/php7.4 /var/www/shop/cron.php\n"})
    assert jobs[0]["site"] == "shop.example.nl"
    assert jobs[0]["user"] == "shopper"
    assert jobs[0]["interpreter"] == "/usr/bin/php7.4"
    assert jobs[0]["problem"] is None


def test_a_site_inside_another_sites_folder_is_not_mistaken_for_it(tmp_path):
    jobs = _jobs(tmp_path, {"shopper": "0 * * * * php /var/www/shop/blog/wp-cron.php\n"})
    assert jobs[0]["site"] == "blog.example.nl"


def test_a_job_that_points_nowhere_known_is_kept_with_no_site(tmp_path):
    jobs = _jobs(tmp_path, {"shopper": "0 3 * * * /usr/bin/php /opt/tools/backup.php\n"})
    assert len(jobs) == 1 and jobs[0]["site"] is None


def test_the_php_of_a_panel_is_recognised_as_the_interpreter(tmp_path):
    jobs = _jobs(tmp_path, {}, {"plesk": "*/10 * * * * shopper /opt/plesk/php/8.1/bin/php /var/www/shop/x.php\n"
                                         "0 1 * * * shopper cd /var/www/shop && /usr/local/bin/ea-php74 y.php\n"})
    assert [job["interpreter"] for job in jobs] == ["/opt/plesk/php/8.1/bin/php", "/usr/local/bin/ea-php74"]
    assert jobs[0]["user"] == "shopper" and jobs[0]["site"] == "shop.example.nl"
    assert jobs[1]["site"] == "shop.example.nl"


def test_a_job_with_an_unknown_interpreter_gets_a_problem_and_is_kept(tmp_path):
    jobs = _jobs(tmp_path, {"shopper": "0 3 * * * /usr/local/bin/mystery /var/www/shop/a\n"})
    assert jobs[0]["interpreter"] is None and jobs[0]["problem"]


def test_comments_and_empty_lines_are_not_jobs(tmp_path):
    text = "# m h dom mon dow command\n\nMAILTO=me@example.nl\nPATH=/usr/bin\n@daily php /var/www/shop/d.php\n"
    jobs = _jobs(tmp_path, {"shopper": text}, {"x": "# nothing\nSHELL=/bin/sh\n\n"})
    assert [job["line"] for job in jobs] == ["@daily php /var/www/shop/d.php"]
    assert jobs[0]["site"] == "shop.example.nl"


def test_a_missing_crontab_is_no_jobs_and_no_note(tmp_path):
    notes = []
    assert _jobs(tmp_path, {}, notes=notes) == [] and len(notes) == 1 and "/etc/crontab" in notes[0]


def test_a_crontab_that_cannot_be_read_is_a_note_naming_the_user(tmp_path):
    notes = []
    failures = {"shopper": migrate.CommandFailed("crontab failed: must be privileged to use -u")}
    _jobs(tmp_path, {}, notes=notes, failures=failures)
    assert len(notes) == 2 and "shopper" in notes[1]


def test_a_server_without_the_crontab_command_says_so(tmp_path):
    notes = []
    failures = {name: migrate.CommandFailed("crontab isn't installed", missing=True) for name in ("shopper", "root")}
    assert _jobs(tmp_path, {}, notes=notes, failures=failures) == []
    assert len(notes) == 2 and "shopper" in notes[1]


def test_the_owner_of_a_sites_folder_is_checked_even_when_passwd_is_unreadable(tmp_path):
    notes = []
    seen = []

    def server(*args, stdin=None):
        seen.append(args[3])
        raise migrate.CommandFailed("crontab failed: no crontab for x")
    root = tmp_path / "site"
    root.mkdir()
    migrate.cron_jobs(server, [{"name": "s", "docroot": str(root)}], notes, passwd=str(tmp_path / "nope"),
                      cron_dir=str(tmp_path / "none"))
    assert "root" in seen and len(seen) >= 1 and any("nope" in note for note in notes)


def test_an_at_word_job_keeps_its_whole_command(tmp_path):
    jobs = _jobs(tmp_path, {}, {"p": "@reboot shopper /usr/bin/php8.1 /var/www/shop/boot.php --now\n"})
    assert jobs[0]["interpreter"] == "/usr/bin/php8.1" and jobs[0]["user"] == "shopper"


def test_settings_above_a_job_travel_with_it(tmp_path):
    text = 'MAILTO=""\nPATH=/opt/php/bin\n0 * * * * php /var/www/shop/a.php\n'
    jobs = _jobs(tmp_path, {"shopper": text})
    assert jobs[0]["settings"] == ['MAILTO=""', "PATH=/opt/php/bin"]


def test_a_line_too_short_to_be_a_job_is_kept_with_a_problem(tmp_path):
    jobs = _jobs(tmp_path, {"shopper": "@daily\n* * * * *\n"}, {"p": "* * * * * shopper\n"})
    assert len(jobs) == 3 and all(job["problem"] and job["interpreter"] is None for job in jobs)


def test_a_file_name_or_redirect_that_looks_like_php_is_not_the_interpreter(tmp_path):
    text = "0 1 * * * mysqldump x > /backup/sh\n0 2 * * * tar czf /b/x.tgz /var/www/php\n"
    jobs = _jobs(tmp_path, {"shopper": text})
    assert [job["interpreter"] for job in jobs] == [None, None] and all(job["problem"] for job in jobs)


def test_a_wrapper_is_looked_through_to_the_program(tmp_path):
    text = ("* * * * * flock -n /tmp/x.lock /usr/bin/php8.1 /var/www/shop/a.php\n"
            "* * * * * nice -n 10 PATH=/x /usr/bin/php7.4 b.php 2>&1\n"
            "* * * * * cd /var/www/shop && timeout 30 php c.php | tee log\n")
    jobs = _jobs(tmp_path, {"shopper": text})
    assert [job["interpreter"] for job in jobs] == ["/usr/bin/php8.1", "/usr/bin/php7.4", "php"]


def test_cron_d_files_that_cron_ignores_are_left_out_and_said_so(tmp_path):
    notes = []
    line = "* * * * * shopper php /var/www/shop/a.php\n"
    jobs = _jobs(tmp_path, {}, {"good": line, "old.dpkg-old": line, "x.bak": line}, notes)
    assert len(jobs) == 1
    assert any("old.dpkg-old" in note and "x.bak" in note for note in notes)


def test_a_note_says_etc_crontab_was_not_read(tmp_path):
    notes = []
    _jobs(tmp_path, {}, notes=notes)
    assert any("/etc/crontab" in note for note in notes)


# ---- What a job looks like on the new server --------------------------------

SITE_ITEMS = migrate.site_items(
    [{"name": "shop.example.nl", "aliases": [], "docroot": "/var/www/shop", "wordpress": True,
      "database": "shopdb", "db_user": "u", "db_host": "localhost", "size": 1, "problem": None}],
    owner_of=lambda path: "www")
NEW_ROOT = "/home/shopexample/public_html/www"
PLESK = ("PATH=/opt/plesk/php/8.1/bin:/usr/bin\n"
         "*/15 * * * * /opt/plesk/php/8.1/bin/php -q /var/www/shop/wp-cron.php >> /dev/null 2>&1\n")


def _item(tmp_path, text, sites=SITE_ITEMS):
    return migrate.cron_items(_jobs(tmp_path, {"shopper": text}), sites)[0]


def test_a_plesk_job_gets_the_new_servers_php_and_web_root(tmp_path):
    item = _item(tmp_path, PLESK)
    assert item["problem"] is None and item["user"] == "shopexample"

    sent, said = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php")

    assert sent["line"] == ("*/15 * * * * /usr/bin/php -q %s/wp-cron.php >> /dev/null 2>&1" % NEW_ROOT)
    assert "/opt/plesk" not in sent["line"] and "/var/www/shop" not in sent["line"]
    assert sent["settings"] == ["PATH=/usr/bin"]
    assert any("/usr/bin/php" in line and NEW_ROOT in line for line in said)
    assert any(line.startswith("The PATH") for line in said)
    assert sent["id"] == item["id"]  # the marker on the new server stays the same job


def test_a_job_that_uses_something_outside_the_site_is_shown_and_not_offered(tmp_path):
    item = _item(tmp_path, "0 3 * * * /usr/bin/php /opt/tools/clean.php --root=/var/www/shop\n")
    assert "/opt/tools/clean.php" in item["problem"] and "by hand" in item["problem"]
    assert migrate.offered([item], {item["id"]: {"state": "new"}}) == []


def test_a_job_of_a_site_whose_folder_is_unknown_is_not_offered(tmp_path):
    sites = migrate.site_items([{"name": "shop.example.nl", "aliases": [], "docroot": None, "wordpress": True,
                                 "problem": None}])
    item = _item(tmp_path, "*/5 * * * * php /var/www/shop/cron.php\n", sites)
    assert item["problem"] and "don't know where" in item["problem"]


def test_a_job_whose_site_needs_a_look_first_is_not_offered_either(tmp_path):
    sites = migrate.site_items([{"name": "shop.example.nl", "aliases": [], "docroot": "/var/www/shop",
                                 "wordpress": True, "problem": "Its wp-config.php can't be read."}])
    item = _item(tmp_path, "*/5 * * * * php /var/www/shop/cron.php\n", sites)
    assert item["problem"] == migrate.SITE_FIRST % "shop.example.nl"


def test_a_job_whose_site_has_not_moved_is_not_sent_and_says_why(tmp_path):
    item = _item(tmp_path, PLESK)
    asked = []

    class Connection(object):
        def ask(self, request):
            asked.append(request)
            return {"ok": True}

    report = migrate.run(Connection(), [item], lambda text: None, php="/usr/bin/php")
    assert asked == [] and report.moved == []
    assert len(report.skipped) == 1 and "Move the site first" in report.skipped[0][1]


def test_a_job_sent_after_its_site_carries_the_new_paths(tmp_path):
    item = _item(tmp_path, PLESK)
    site = SITE_ITEMS[0]
    sent = []

    class Connection(object):
        def ask(self, request):
            sent.append(request)
            return {"ok": True, "docroot": NEW_ROOT, "database": "shopdb"}

    extra = []
    report = migrate.run(Connection(), [site, item], lambda text: None, php="/usr/bin/php", extra_notes=extra,
                         move=lambda item, answer: None)
    installed = [r["item"]["line"] for r in sent if r["item"].get("kind") == "cron"]
    assert installed == ["*/15 * * * * /usr/bin/php -q %s/wp-cron.php >> /dev/null 2>&1" % NEW_ROOT]
    assert [i["id"] for i, _ in report.moved] == [site["id"], item["id"]]
    assert any(NEW_ROOT in line for line in extra)


def test_without_a_php_from_the_new_server_a_php_job_is_not_sent(tmp_path):
    item = _item(tmp_path, PLESK)
    with pytest.raises(migrate.MoveFailed) as failed:
        migrate.cron_for_new_server(item, NEW_ROOT, None)
    assert "didn't say which PHP" in str(failed.value)


def test_a_lock_file_in_tmp_travels_as_it_is(tmp_path):
    """/tmp is /tmp on any server, and the job makes the file itself: that is not a reason to skip the job."""
    item = _item(tmp_path, "@daily flock -n /tmp/x.lock /usr/bin/php /var/www/shop/x.php\n")
    assert item["problem"] is None
    sent, _ = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php")
    assert sent["line"] == "@daily flock -n /tmp/x.lock /usr/bin/php %s/x.php" % NEW_ROOT


def test_a_job_the_state_says_moved_is_not_offered_a_second_time(tmp_path):
    item = _item(tmp_path, PLESK)
    notes = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    notes.items[item["id"]] = {"status": "moved"}
    status = {item["id"]: {"state": "exists", "detail": "This job is already in the crontab of shopexample."}}

    assert migrate.offered([item], status, notes) == []
    assert "already moved from here" in migrate.items_board([item], status, notes)


def test_a_job_whose_crontab_was_cleared_is_offered_again_and_says_so(tmp_path):
    """The notes may not outlive the new server: a job that is no longer there is work that is not done."""
    item = _item(tmp_path, PLESK)
    notes = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    notes.items[item["id"]] = {"status": "moved"}
    status = {item["id"]: {"state": "new", "detail": "The crontab of shopexample doesn't have this job yet."}}

    assert migrate.offered([item], status, notes) == [item]
    text = " ".join(migrate.items_board([item], status, notes).split())
    assert migrate.GONE_THERE in text and "already moved from here" not in text


def test_a_job_the_new_server_already_had_is_not_reported_as_moved(tmp_path):
    item = _item(tmp_path, PLESK)
    notes = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    notes.items[SITE_ITEMS[0]["id"]] = {"status": "moved", "docroot": NEW_ROOT}  # its site moved in an earlier run

    class Connection(object):
        def ask(self, request):
            return {"ok": True, "already": True, "steps": ["The job was already in the crontab of shopexample."]}

    report = migrate.run(Connection(), [item], lambda text: None, php="/usr/bin/php",
                         notes=notes, status={item["id"]: {"state": "new"}},
                         extra_notes=[])
    assert report.moved == [] and [i["id"] for i, _ in report.already] == [item["id"]]
    text = migrate.report(report)
    assert "Was already there" in text and "Moved:" not in text
    assert "BOTH servers" in text  # both servers still run it until the DNS moves


WP_JOB = "*/5 * * * * cd /var/www/shop && /usr/local/bin/wp cron event run --due-now --path=/var/www/shop\n"


def test_a_wp_cli_job_gets_the_new_servers_wp_and_web_root(tmp_path):
    """Most of a WordPress site's own jobs run wp-cli, and the new server says where its own wp is."""
    item = _item(tmp_path, WP_JOB)
    assert item["problem"] is None

    sent, said = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php", "/usr/local/bin/wp")

    assert sent["line"] == ("*/5 * * * * cd %s && /usr/local/bin/wp cron event run --due-now --path=%s"
                            % (NEW_ROOT, NEW_ROOT))
    assert "/var/www/shop" not in sent["line"]
    assert any(NEW_ROOT in line for line in said)


def test_a_wp_cli_job_follows_the_new_servers_own_place_for_wp(tmp_path):
    item = _item(tmp_path, WP_JOB)
    sent, _ = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php", "/opt/bin/wp")
    assert "/opt/bin/wp cron event run" in sent["line"]


def test_without_wp_cli_on_the_new_server_the_job_is_not_sent(tmp_path):
    item = _item(tmp_path, WP_JOB)
    with pytest.raises(migrate.MoveFailed) as failed:
        migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php", None)
    assert "where its wp-cli is" in str(failed.value) and "Install wp-cli there" in str(failed.value)


def test_the_run_sends_a_wp_job_rewritten_with_what_the_new_server_said(tmp_path):
    item = _item(tmp_path, WP_JOB)
    notes = migrate.State(path=str(tmp_path / "state.json"), say=lambda text: None)
    notes.items[SITE_ITEMS[0]["id"]] = {"status": "moved", "docroot": NEW_ROOT}
    sent = []

    class Connection(object):
        def ask(self, request):
            sent.append(request)
            return {"ok": True, "already": False}

    report = migrate.run(Connection(), [item], lambda text: None, notes=notes, php="/usr/bin/php",
                         wp="/usr/local/bin/wp", status={item["id"]: {"state": "new"}})
    assert [i["id"] for i, _ in report.moved] == [item["id"]]
    assert NEW_ROOT in sent[0]["item"]["line"] and "/usr/local/bin/wp" in sent[0]["item"]["line"]


BARE = "*/15 * * * * php -q /var/www/shop/wp-cron.php\n"


def test_a_job_that_runs_php_by_name_gets_the_new_servers_php_written_out(tmp_path):
    """On a server whose only PHP is OpenLiteSpeed's lsphp, a job that runs "php" never runs at all."""
    item = _item(tmp_path, BARE)
    sent, said = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/local/lsws/lsphp83/bin/lsphp")
    assert sent["line"] == "*/15 * * * * /usr/local/lsws/lsphp83/bin/lsphp -q %s/wp-cron.php" % NEW_ROOT
    assert any("lsphp" in line for line in said)


def test_the_same_word_inside_a_path_is_not_taken_for_the_program(tmp_path):
    item = _item(tmp_path, "0 4 * * * cd /var/www/shop/php && php x.php\n")
    sent, _ = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php")
    assert sent["line"] == "0 4 * * * cd %s/php && /usr/bin/php x.php" % NEW_ROOT


@pytest.mark.parametrize("word, out", [("php7.4", "/usr/bin/php"), ("lsphp81", "/usr/bin/php"),
                                       ("ea-php74", "/usr/bin/php"), ("wp", "/usr/local/bin/wp")])
def test_a_bare_php_version_or_a_bare_wp_is_written_out_too(tmp_path, word, out):
    item = _item(tmp_path, "*/5 * * * * %s /var/www/shop/x.php\n" % word)
    sent, _ = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php", "/usr/local/bin/wp")
    assert sent["line"].split()[5] == out


def test_a_path_from_the_old_server_is_kept_and_named(tmp_path):
    """Dropping an entry would break a job that needed it in a way nobody could explain."""
    item = _item(tmp_path, "*/15 * * * * /usr/bin/php /var/www/shop/x.php\n")
    item["settings"] = ["PATH=/opt/plesk/bin:/usr/bin:/var/www/shop/bin"]

    sent, said = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php")

    assert sent["settings"] == ["PATH=/opt/plesk/bin:/usr/bin:%s/bin" % NEW_ROOT]
    named = [line for line in said if "kept as it is" in line]
    assert len(named) == 1 and "/opt/plesk/bin" in named[0]
    assert "/usr/bin" not in named[0].replace("/usr/bin:", "")  # a place every Linux has is not named
    assert "may not be there on the new server" in named[0]


def test_a_path_that_only_names_ordinary_places_is_said_nothing_about(tmp_path):
    item = _item(tmp_path, "*/15 * * * * /usr/bin/php /var/www/shop/x.php\n")
    item["settings"] = ["PATH=/usr/local/bin:/usr/bin:/bin", "MAILTO=me@example.nl", "SHELL=/bin/bash"]
    sent, said = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php")
    assert sent["settings"] == item["settings"]
    assert not [line for line in said if "PATH" in line]


def test_an_empty_path_is_kept_as_it_is(tmp_path):
    item = _item(tmp_path, "*/15 * * * * /usr/bin/php /var/www/shop/x.php\n")
    item["settings"] = ["PATH="]
    sent, said = migrate.cron_for_new_server(item, NEW_ROOT, "/usr/bin/php")
    assert sent["settings"] == ["PATH="] and not [line for line in said if "PATH" in line]


def test_what_is_being_moved_is_said_within_eighty_columns(capsys, tmp_path):
    item = _item(tmp_path, PLESK)
    assert len(migrate._title(item)) > 79  # a cron job's own line is longer than a terminal

    class Connection(object):
        def ask(self, request):
            return {"ok": True}

    migrate.run(Connection(), [item], lambda text: None, php="/usr/bin/php")
    out = capsys.readouterr().out
    assert "Moving 1 of 1" in out and all(len(line) <= 79 for line in out.splitlines())
