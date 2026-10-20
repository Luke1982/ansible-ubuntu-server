import os

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
    assert _jobs(tmp_path, {}, notes=notes) == [] and notes == []


def test_a_crontab_that_cannot_be_read_is_a_note_naming_the_user(tmp_path):
    notes = []
    failures = {"shopper": migrate.CommandFailed("crontab failed: must be privileged to use -u")}
    _jobs(tmp_path, {}, notes=notes, failures=failures)
    assert len(notes) == 1 and "shopper" in notes[0]


def test_a_server_without_the_crontab_command_says_so(tmp_path):
    notes = []
    failures = {name: migrate.CommandFailed("crontab isn't installed", missing=True) for name in ("shopper", "root")}
    assert _jobs(tmp_path, {}, notes=notes, failures=failures) == []
    assert len(notes) == 1 and "shopper" in notes[0]


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
