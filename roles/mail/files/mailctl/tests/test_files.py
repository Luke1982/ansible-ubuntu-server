from mailctl.core import files


def test_replace_writes_a_new_file_and_says_so(tmp_path):
    path = tmp_path / "conf" / "table"

    assert files.replace(path, "a\n")
    assert path.read_text() == "a\n"
    assert path.stat().st_mode & 0o777 == 0o644


def test_replace_leaves_a_file_with_the_same_content_alone(tmp_path):
    path = tmp_path / "table"
    files.replace(path, "a\n")
    written = path.stat().st_mtime_ns

    assert not files.replace(path, "a\n")
    assert path.stat().st_mtime_ns == written


def test_replace_leaves_no_temporary_files_behind(tmp_path):
    files.replace(tmp_path / "table", "a\n")
    files.replace(tmp_path / "table", "b\n")

    assert [path.name for path in tmp_path.iterdir()] == ["table"]


def test_read_of_a_missing_file_is_none(tmp_path):
    assert files.read(tmp_path / "missing") is None


def test_restore_puts_back_what_read_returned(tmp_path):
    existing, missing = tmp_path / "existing", tmp_path / "missing"
    existing.write_text("old\n")
    before = files.read(existing), files.read(missing)
    files.replace(existing, "new\n")
    files.replace(missing, "new\n")

    files.restore(existing, before[0])
    files.restore(missing, before[1])

    assert existing.read_text() == "old\n"
    assert not missing.exists()


def test_remove_deletes_a_file_and_says_whether_there_was_one(tmp_path):
    path = tmp_path / "site.conf"
    path.write_text("site\n")

    assert files.remove(path)
    assert not path.exists()
    assert not files.remove(path)


def test_replace_puts_a_file_in_place_of_a_link_and_leaves_its_target_alone(tmp_path):
    target = tmp_path / "target"
    target.write_text("keep\n")
    link = tmp_path / "table"
    link.symlink_to(target)

    files.replace(link, "new\n")

    assert not link.is_symlink() and link.read_text() == "new\n"
    assert target.read_text() == "keep\n"
