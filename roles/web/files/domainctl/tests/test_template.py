import pytest

from domainctl.core import template
from serverctl.errors import CtlError

WITH_MARKERS = """  rewrite  {
    enable                1
# BEGIN https-redirect
RewriteCond %{HTTPS} off
RewriteRule .* https://%{HTTP_HOST}%{REQUEST_URI} [L,R=301]
# END https-redirect
  }
"""


def test_the_redirect_starts_out_on():
    assert template.redirect_is_on(WITH_MARKERS)


def test_switching_it_off_comments_only_the_rules():
    off = template.with_redirect(WITH_MARKERS, False)
    assert "#RewriteCond %{HTTPS} off" in off
    assert "#RewriteRule .* https://" in off
    assert "    enable                1" in off  # outside the markers, untouched
    assert not template.redirect_is_on(off)


def test_switching_it_on_again_gives_back_the_same_file():
    off = template.with_redirect(WITH_MARKERS, False)
    assert template.with_redirect(off, True) == WITH_MARKERS


def test_switching_on_a_file_that_is_already_on_changes_nothing():
    assert template.with_redirect(WITH_MARKERS, True) == WITH_MARKERS


def test_switching_off_twice_does_not_comment_twice():
    once = template.with_redirect(WITH_MARKERS, False)
    assert template.with_redirect(once, False) == once


@pytest.mark.parametrize("text", [
    "no markers at all\n",
    "# BEGIN https-redirect\nrule\n",  # no end
    "# END https-redirect\n# BEGIN https-redirect\n",  # the wrong way round
    "# BEGIN https-redirect\n# END https-redirect\n# BEGIN https-redirect\n# END https-redirect\n",
])
def test_a_template_without_one_clear_pair_of_markers_is_refused(text):
    with pytest.raises(CtlError, match="markers|BEGIN"):
        template.with_redirect(text, False)


def test_a_template_without_markers_counts_as_having_the_redirect_on():
    """So a hand-written template is never mistaken for one left switched off."""
    assert template.redirect_is_on("no markers at all\n")


def test_set_redirect_writes_the_file_and_reports_the_change(config):
    assert template.set_redirect(config, False) is True
    assert not template.redirect_is_on(template.read(config))
    assert template.set_redirect(config, False) is False  # already off
    assert template.set_redirect(config, True) is True


def test_a_missing_template_says_to_run_the_playbook(config):
    config.template_path().unlink()
    with pytest.raises(CtlError, match="There is no OpenLiteSpeed template") as failure:
        template.read(config)
    assert "Ansible playbook" in failure.value.hint
