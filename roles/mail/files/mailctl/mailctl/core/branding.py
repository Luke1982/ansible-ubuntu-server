"""Webmail in the colours and the logo of whoever runs the server.

SOGo's pages are built from files its package installs, so a logo put there is gone with the next upgrade. The
webmail sites serve ours in their place instead, by the name SOGo asks for: the logo above the login box, the
small one in the bar at the top, the icon in the browser tab, and a copy of SOGo's stylesheet in which its own
green-teal is replaced by a colour of our own.

The stylesheet is copied and recoloured rather than written by hand: it is 800 KB of compiled rules, and every
surface that carries the colour -- buttons, the bar, what is selected, the badges -- is coloured by one of the
shades of that one palette. Colours with a hue of their own, like the red of a warning or the green of the save
button, are left as they are.
"""

import colorsys
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import MailctlError

# What SOGo asks for, and what ours is called. The stylesheet is made here; the rest are files somebody hands in.
LOGIN_LOGO = "img/sogo-full.svg"
BAR_LOGO = "img/sogo-compact.svg"
ICON = "img/sogo.ico"
THEME = "css/theme-default.css"
NAMES = {LOGIN_LOGO: "login-logo", BAR_LOGO: "bar-logo", ICON: "icon", THEME: "theme.css"}
COLOUR_FILE = "colour"  # the colour the stylesheet was made with, so it can be shown and made again

# SOGo's own palette is a green-teal, never fully saturated. A colour in the stylesheet with that hue belongs to
# it; the reds, greens and cyans of warnings, buttons and badges have a hue of their own and are left alone.
PRIMARY_HUE = (166.0, 190.0)  # degrees
PRIMARY_SATURATION = 0.62  # anything more saturated is another palette
PRIMARY_LIGHTNESS = 0.402  # of SOGo's own primary, rgb(77,128,128): what the colour given here takes the place of
_RGB = re.compile(r"rgb\((\d{1,3}),\s*(\d{1,3}),\s*(\d{1,3})\)")
_RGBA = re.compile(r"rgba\((\d{1,3}),\s*(\d{1,3}),\s*(\d{1,3}),\s*([0-9.]+)\)")
_HEX = re.compile(r"#([0-9a-fA-F]{6})\b")


@dataclass(frozen=True)
class Branding:
    """What the webmail sites serve of their own."""
    colour: str = ""
    files: tuple[str, ...] = ()  # the names SOGo asks for, as in NAMES

    def __bool__(self) -> bool:
        return bool(self.colour or self.files)


def colour_of(text: str) -> tuple[float, float, float]:
    """A colour as hue, saturation and lightness. Raises when it isn't one this understands."""
    found = _HEX.fullmatch(text.strip())
    if not found:
        raise MailctlError(f"{text} isn't a colour.", hint="Write it as #RRGGBB, like the #09526D of a brand.")
    red, green, blue = (int(found.group(1)[at:at + 2], 16) / 255 for at in (0, 2, 4))
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    return hue * 360, saturation, lightness


def recolour(css: str, colour: str) -> str:
    """SOGo's stylesheet with its own palette in this colour.

    The colour given takes the place of SOGo's own primary exactly, and every other shade of the palette moves
    with it, keeping how far it stood from white. A pale background therefore stays a pale background and a dark
    surface stays dark, while the whole palette is the one colour's.
    """
    hue, saturation, lightness = colour_of(colour)
    # How much of the way to white each shade keeps, now that the primary sits where the given colour does.
    towards_white = (1 - lightness) / (1 - PRIMARY_LIGHTNESS)

    def shade(red: int, green: int, blue: int) -> tuple[int, int, int] | None:
        was_hue, was_lightness, was_saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
        if not (PRIMARY_HUE[0] <= was_hue * 360 <= PRIMARY_HUE[1]) or was_saturation > PRIMARY_SATURATION:
            return None
        now_lightness = min(1.0, max(0.0, 1 - (1 - was_lightness) * towards_white))
        now = colorsys.hls_to_rgb(hue / 360, now_lightness, saturation)
        return tuple(round(part * 255) for part in now)

    def as_rgb(found: re.Match) -> str:
        now = shade(*(int(part) for part in found.groups()))
        return f"rgb({now[0]},{now[1]},{now[2]})" if now else found.group()

    def as_rgba(found: re.Match) -> str:
        now = shade(*(int(part) for part in found.groups()[:3]))
        return f"rgba({now[0]},{now[1]},{now[2]},{found.group(4)})" if now else found.group()

    def as_hex(found: re.Match) -> str:
        value = found.group(1)
        now = shade(*(int(value[at:at + 2], 16) for at in (0, 2, 4)))
        return "#%02x%02x%02x" % now if now else found.group()

    return _HEX.sub(as_hex, _RGBA.sub(as_rgba, _RGB.sub(as_rgb, css)))


def current(config: Config) -> Branding:
    """What the sites serve of their own now."""
    directory = config.webmail_branding
    colour = ""
    saved = directory / COLOUR_FILE
    if saved.is_file():
        colour = saved.read_text().strip()
    return Branding(colour, tuple(sorted(asked for asked, name in NAMES.items() if _served(directory, name))))


def overrides(config: Config) -> dict[str, str]:
    """What the sites serve in SOGo's place: the name SOGo asks for, and the file of ours that answers it."""
    directory = config.webmail_branding
    found = {}
    for asked, name in NAMES.items():
        ours = _served(directory, name)
        if ours:
            found[asked] = ours.name
    return found


def apply(config: Config, colour: str = "", logo: Path | None = None, bar_logo: Path | None = None,
          icon: Path | None = None) -> Branding:
    """Puts the files the sites serve in place. What isn't given is left as it is."""
    directory = config.webmail_branding
    directory.mkdir(parents=True, exist_ok=True)
    for given, asked in ((logo, LOGIN_LOGO), (bar_logo, BAR_LOGO), (icon, ICON)):
        if given:
            _put(directory, NAMES[asked], given)
    if colour:
        colour_of(colour)  # before anything is written
        source = config.sogo_resources / THEME
        if not source.is_file():
            raise MailctlError(f"SOGo's own stylesheet isn't at {source}, so it can't be recoloured.",
                               hint="Is SOGo installed? Its files are where sogo_resources in the config says.")
        (directory / NAMES[THEME]).write_text(recolour(source.read_text(errors="replace"), colour))
        (directory / COLOUR_FILE).write_text(colour.strip() + "\n")
    return current(config)


def refresh(config: Config) -> bool:
    """Makes the stylesheet again when SOGo's own is newer than ours, which it is after SOGo is upgraded: the new
    one holds rules the old didn't. Also makes it when a colour was put here by hand, as a playbook run does."""
    colour = current(config).colour
    if not colour:
        return False
    ours = config.webmail_branding / NAMES[THEME]
    theirs = config.sogo_resources / THEME
    if not theirs.is_file():
        return False
    if ours.is_file() and ours.stat().st_mtime >= theirs.stat().st_mtime:
        return False
    apply(config, colour=colour)
    return True


def clear(config: Config) -> bool:
    """Takes ours away, so the sites serve SOGo's own again. Returns whether there was anything."""
    directory = config.webmail_branding
    if not directory.is_dir():
        return False
    found = False
    for name in (*NAMES.values(), COLOUR_FILE):
        for file in directory.glob(f"{name}*" if "." not in name else name):
            file.unlink()
            found = True
    return found


def _served(directory: Path, name: str) -> Path | None:
    """The file of ours that answers for that name, whatever its kind: a logo may be an SVG, a PNG or a JPEG."""
    if "." in name:
        file = directory / name
        return file if file.is_file() else None
    found = sorted(file for file in directory.glob(f"{name}.*") if file.is_file())
    return found[0] if found else None


def _put(directory: Path, name: str, given: Path) -> None:
    if not given.is_file():
        raise MailctlError(f"{given} isn't a file.")
    suffix = given.suffix.lower()
    if suffix not in (".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp"):
        raise MailctlError(f"{given} isn't a picture mailctl knows.",
                           hint="Give an .svg, .png, .jpg, .gif, .webp or .ico file.")
    for old in directory.glob(f"{name}.*"):
        old.unlink()
    shutil.copyfile(given, directory / f"{name}{suffix}")
