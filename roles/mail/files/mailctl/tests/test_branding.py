import colorsys
from dataclasses import replace
from pathlib import Path

import pytest

from mailctl.core import branding
from mailctl.core.errors import MailctlError

BLUE = "#09526D"
# SOGo's own: the teal it colours everything with, one of its pale shades, and two colours of another hue.
SOGO_CSS = """
.md-primary{color:rgb(77,128,128)}
.md-primary.md-hue-1{background-color:rgb(178,214,211)}
.md-warn{color:#dd2c00}
.md-save{background-color:rgb(86,176,76)}
.md-ink{background-color:rgba(77,128,128,0.87)}
.md-cyan{color:rgb(0,176,192)}
"""


@pytest.fixture
def config(config, tmp_path):
    """A server whose SOGo files are in the test's own directory."""
    resources = tmp_path / "sogo-resources"
    (resources / "css").mkdir(parents=True)
    (resources / "css" / "theme-default.css").write_text(SOGO_CSS)
    return replace(config, sogo_resources=resources, webmail_branding=tmp_path / "branding")


def picture(tmp_path, name="logo.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n and then some")
    return path


def hue_of(text: str) -> float:
    red, green, blue = (int(text[at:at + 2], 16) / 255 for at in (1, 3, 5))
    return colorsys.rgb_to_hls(red, green, blue)[0] * 360


def test_the_colour_of_a_brand_is_read_as_hue_saturation_and_lightness():
    hue, saturation, lightness = branding.colour_of(BLUE)

    assert 195 < hue < 200  # a blue
    assert saturation > 0.8 and lightness < 0.3  # deep and dark, as it is


def test_something_that_isnt_a_colour_says_so():
    with pytest.raises(MailctlError, match="isn't a colour"):
        branding.colour_of("blue")


def test_recolouring_puts_the_brand_hue_on_sogos_own_palette():
    css = branding.recolour(SOGO_CSS, BLUE)

    assert "rgb(77,128,128)" not in css
    assert "rgb(178,214,211)" not in css  # its pale shade too
    assert "rgba(" in css and "0.87)" in css  # what was see-through stays see-through


def test_recolouring_leaves_the_colours_that_say_something_alone():
    """The red of a warning says what it is; only SOGo's two palettes are ours."""
    css = branding.recolour(SOGO_CSS, BLUE)

    assert "#dd2c00" in css
    assert "rgb(0,176,192)" in css  # a cyan of its own: near the primary's hue, but far more saturated


def test_the_panel_the_login_box_sits_in_is_recoloured_too():
    """SOGo's second palette is a green, and it is the first thing anybody sees."""
    css = branding.recolour(SOGO_CSS, BLUE)

    assert "rgb(86,176,76)" not in css
    assert ".md-save{background-color:rgb(9,82,109)}" in css


def test_a_second_colour_can_be_given_for_that_panel():
    css = branding.recolour(SOGO_CSS, BLUE, accent="#E46623")

    assert ".md-primary{color:rgb(9,82,109)}" in css
    assert ".md-save{background-color:rgb(228,102,35)}" in css


def test_every_shade_keeps_the_lightness_it_had():
    """So what was a pale background stays pale, and what was a dark bar stays dark."""
    css = branding.recolour(SOGO_CSS, BLUE)
    dark = css.split(".md-primary{color:")[1].split("}")[0]
    pale = css.split(".md-hue-1{background-color:")[1].split("}")[0]

    values = [tuple(int(part) for part in one.removeprefix("rgb(").removesuffix(")").split(",")) for one in
              (dark, pale)]
    assert sum(values[0]) < sum(values[1])  # the pale shade is still the lighter one
    assert 190 < colorsys.rgb_to_hls(*(part / 255 for part in values[0]))[0] * 360 < 205


def test_applying_a_colour_writes_the_stylesheet_the_sites_serve(config):
    now = branding.apply(config, colour=BLUE)

    written = (config.webmail_branding / "theme.css").read_text()
    assert "rgb(77,128,128)" not in written
    assert now.colour == BLUE
    assert branding.current(config).colour == BLUE


def test_applying_a_logo_keeps_the_kind_of_picture_it_is(config, tmp_path):
    branding.apply(config, logo=picture(tmp_path), bar_logo=picture(tmp_path, "white.svg"))

    assert (config.webmail_branding / "login-logo.png").is_file()
    assert (config.webmail_branding / "bar-logo.svg").is_file()
    assert branding.overrides(config) == {branding.LOGIN_LOGO: "login-logo.png", branding.BAR_LOGO: "bar-logo.svg"}


def test_a_new_logo_takes_the_place_of_the_one_before_it(config, tmp_path):
    branding.apply(config, logo=picture(tmp_path, "first.png"))
    branding.apply(config, logo=picture(tmp_path, "second.svg"))

    assert not (config.webmail_branding / "login-logo.png").exists()
    assert branding.overrides(config) == {branding.LOGIN_LOGO: "login-logo.svg"}


def test_a_file_that_isnt_a_picture_is_refused(config, tmp_path):
    text = tmp_path / "notes.txt"
    text.write_text("hello")

    with pytest.raises(MailctlError, match="isn't a picture"):
        branding.apply(config, logo=text)


def test_a_colour_without_sogos_own_stylesheet_says_where_it_looked(config):
    (config.sogo_resources / "css" / "theme-default.css").unlink()

    with pytest.raises(MailctlError, match="isn't at"):
        branding.apply(config, colour=BLUE)


def test_clearing_puts_sogos_own_back(config, tmp_path):
    branding.apply(config, colour=BLUE, logo=picture(tmp_path))

    assert branding.clear(config) is True
    assert branding.current(config) == branding.Branding()
    assert branding.overrides(config) == {}
    assert branding.clear(config) is False


def test_a_server_that_was_never_branded_serves_sogos_own(config):
    assert not branding.current(config)
    assert branding.overrides(config) == {}


def test_the_stylesheet_is_made_again_when_sogos_own_is_newer(config):
    import os
    import time

    branding.apply(config, colour=BLUE)
    ours = config.webmail_branding / "theme.css"
    ours.write_text("stale")
    os.utime(ours, (time.time() - 60, time.time() - 60))

    assert branding.refresh(config) is True
    assert "stale" not in ours.read_text()
    assert branding.refresh(config) is False  # nothing to do the second time


def test_nothing_is_made_again_on_a_server_without_a_colour(config):
    assert branding.refresh(config) is False


def test_the_colour_given_takes_the_place_of_sogos_own_exactly():
    """So webmail is in the brand's colour, not in something near it."""
    css = branding.recolour(".md-primary{color:rgb(77,128,128)}", BLUE)

    assert css == ".md-primary{color:rgb(9,82,109)}"


def test_a_shade_that_stands_out_never_turns_black():
    """SOGo's brightest accent is what a button to write a message is made of; with a dark brand colour the sum
    would be black, and a black button on a dark panel is a button nobody sees."""
    css = branding.recolour(".md-fab{background-color:rgb(0,200,83)}", "#09526D")

    values = [int(part) for part in css.split("rgb(")[1].split(")")[0].split(",")]
    assert sum(values) > 90


SOGO_JS = ('r.definePalette("sogo-blue",{50:"f0faf9",400:"b2d6d3",900:"4d8080",A700:"00b0c0",'
           'contrastDefaultColor:"light",contrastDarkColors:["50","100"]});'
           'r.definePalette("sogo-green",{500:"56b04c",A400:"00e676"});'
           'r.theme("default").primaryPalette("sogo-blue",{default:"900"})')


def test_webmail_builds_its_own_colours_from_its_javascript():
    """The stylesheet is the login page's; webmail itself builds its colours from two palettes written out in its
    script, so the same colours go there or only the front door is ours."""
    js = branding.recolour_script(SOGO_JS, BLUE, accent="#0693E3")

    assert '900:"09526d"' in js  # the shade the theme is built around, exactly the colour given
    assert '500:"0693e3"' in js
    assert '4d8080' not in js and '56b04c' not in js


def test_recolouring_a_script_leaves_everything_else_as_it_was():
    js = branding.recolour_script(SOGO_JS, BLUE)

    assert 'contrastDefaultColor:"light"' in js
    assert 'contrastDarkColors:["50","100"]' in js
    assert 'r.theme("default").primaryPalette("sogo-blue",{default:"900"})' in js


def test_a_script_without_the_palettes_is_left_alone():
    assert branding.recolour_script("nothing to see", BLUE) == "nothing to see"


def test_applying_a_colour_writes_the_script_webmail_builds_from(config):
    (config.sogo_resources / "js").mkdir(parents=True, exist_ok=True)
    (config.sogo_resources / "js" / "Common.js").write_text(SOGO_JS)

    branding.apply(config, colour=BLUE)

    assert '900:"09526d"' in (config.webmail_branding / "common.js").read_text()
    assert branding.overrides(config)[branding.SCRIPT] == "common.js"
