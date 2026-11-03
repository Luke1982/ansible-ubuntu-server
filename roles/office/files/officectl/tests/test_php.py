"""The PHP settings of one Nextcloud: a pool of its own, which nothing else on the server reads."""

from officectl.core import php


def test_the_pool_is_named_after_the_nextcloud(config):
    assert php.processor("ncacme") == "ncacme-php"
    assert php.socket("ncacme") == "uds://tmp/lshttpd/ncacme-php.sock"


def test_php_reads_the_modules_of_this_php_and_then_these_settings(config):
    where = php.scan_dir(config, "ncacme")
    assert where.endswith("/8.5/mods-available:" + str(config.php_settings_dir("ncacme")))


def test_the_settings_give_nextcloud_what_it_asks_for(config):
    text = php.settings(config, "ncacme", "acme.office.example.nl")
    assert "memory_limit = 512M" in text
    assert "upload_max_filesize = 16G" in text and "post_max_size = 16G" in text
    assert "max_execution_time = 3600" in text
    # Nextcloud reads the comments of its own code, and it caches on the command line as well as in a page.
    assert "opcache.save_comments = 1" in text
    assert "apc.enable_cli = 1" in text


def test_the_settings_point_at_its_own_temporary_directory(config):
    text = php.settings(config, "ncacme", "acme.office.example.nl")
    assert f"upload_tmp_dir = {config.tmp_of('ncacme')}" in text
    assert f"sys_temp_dir = {config.tmp_of('ncacme')}" in text


def test_the_settings_say_which_nextcloud_they_are_for(config):
    assert "acme.office.example.nl" in php.settings(config, "ncacme", "acme.office.example.nl")


def test_writing_them_twice_changes_nothing_the_second_time(config):
    assert php.write_settings(config, "ncacme", "acme.office.example.nl")
    assert not php.write_settings(config, "ncacme", "acme.office.example.nl")
    assert php.settings_path(config, "ncacme").is_file()


def test_removing_them_takes_the_directory_too(config):
    php.write_settings(config, "ncacme", "acme.office.example.nl")
    assert php.remove_settings(config, "ncacme")
    assert not php.settings_path(config, "ncacme").exists()
    assert not config.php_settings_dir("ncacme").exists()


def test_removing_settings_that_are_not_there_is_no_error(config):
    assert not php.remove_settings(config, "ncacme")


def test_the_php_nextcloud_needs_is_looked_for_where_it_is_installed(config, ols_root):
    assert php.installed(config)
    (ols_root / "lsphp85" / "bin" / "lsphp").unlink()
    assert not php.installed(config)
