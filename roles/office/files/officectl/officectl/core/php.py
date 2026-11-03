"""The PHP one Nextcloud runs on: a pool of its own, and the settings that go with it.

Nextcloud needs more of PHP than a web site does -- more memory, no time limit worth speaking of, and a big
opcache -- and it needs a newer PHP than the sites share. So it gets a pool of its own: an external application
inside its own virtual host, which no other site can reach, and a directory of .ini files only that pool reads.

The pool runs as OpenLiteSpeed's own user, like the web sites' PHP does, so Nextcloud's own files are that
user's where it writes and root's where it only reads. occ and the background jobs run on the same PHP with the
same settings, which is what makes them see the same cache as the pages do.
"""

import os
import signal
from pathlib import Path

from serverctl import files

from ..config import Config

HEADER = "; Written by officectl. Changes are overwritten.\n"
SETTINGS_FILE = "60-nextcloud.ini"


def processor(user: str) -> str:
    """The name of the external application in the virtual host."""
    return f"{user}-php"


def socket(user: str) -> str:
    """Where OpenLiteSpeed talks to it. A socket, not a port: nothing outside this server reaches it."""
    return f"uds://tmp/lshttpd/{user}-php.sock"


def scan_dir(config: Config, user: str) -> str:
    """The directories that PHP reads its .ini files from: the modules of this PHP, then these settings."""
    return f"{config.php_ini_dir()}/mods-available:{config.php_settings_dir(user)}"


def settings_path(config: Config, user: str):
    return config.php_settings_dir(user) / SETTINGS_FILE


def settings(config: Config, user: str, host: str) -> str:
    """The .ini file for one Nextcloud's PHP."""
    tmp = config.tmp_of(user)
    return f"""{HEADER}; The PHP of the Nextcloud at {host}: a pool of its own ({processor(user)} in its virtual
; host), so nothing of this reaches the other sites on this server.
memory_limit = {config.php_memory_mb}M
; A browser that uploads a file in one piece is held to this; Nextcloud's own uploads are cut into chunks and
; are not.
upload_max_filesize = {config.php_upload_gb}G
post_max_size = {config.php_upload_gb}G
; occ and cron.php do work that takes minutes: an import, an upgrade, the first scan of a big folder.
max_input_time = {config.php_time_limit}
max_execution_time = {config.php_time_limit}
; Nextcloud writes downloads out as it reads them, so they are not held in memory first.
output_buffering = 0
upload_tmp_dir = {tmp}
sys_temp_dir = {tmp}
; The compiled PHP of Nextcloud and its apps. save_comments stays on: Nextcloud reads the comments of its own
; code to find out what its classes do.
opcache.enable = 1
opcache.enable_cli = 1
opcache.save_comments = 1
opcache.memory_consumption = {config.php_opcache_mb}
opcache.interned_strings_buffer = 64
opcache.max_accelerated_files = {config.php_opcache_files}
opcache.revalidate_freq = 60
; Nextcloud's own cache (memcache.local). The command line gets it too, or occ and cron.php would each warm a
; cache of their own and leave the pages' cache as it was.
apc.shm_size = {config.php_apcu_mb}M
apc.enable_cli = 1
"""


def write_settings(config: Config, user: str, host: str) -> bool:
    """Writes the .ini file. Returns whether it changed."""
    config.php_settings_dir(user).mkdir(parents=True, exist_ok=True)
    return files.replace(settings_path(config, user), settings(config, user, host))


def remove_settings(config: Config, user: str) -> bool:
    """Takes the .ini file and its directory away. Returns whether there was one."""
    removed = files.remove(settings_path(config, user))
    directory = config.php_settings_dir(user)
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
    return removed


def installed(config: Config) -> bool:
    """Whether the PHP Nextcloud needs is on this server. The office role installs it."""
    return config.lsphp().is_file() and config.php().is_file()


def stop_pools(config: Config) -> int:
    """Stops the PHP processes of the Nextclouds, so the next request starts them with the settings as they are
    now. Returns how many were stopped.

    OpenLiteSpeed's graceful restart leaves the PHP processes that are already running in place, and those keep
    the settings they started with. Only this PHP's processes are stopped: the web sites run on another one and
    are left alone. They are found by the program they run, because their own name says no more than "lsphp".
    A page being built at that moment is cut off.
    """
    stopped = 0
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            program = os.readlink(process / "exe")
        except OSError:
            continue  # a process that ended while this ran, or one with no program of its own
        if program == str(config.lsphp()):
            try:
                os.kill(int(process.name), signal.SIGTERM)
                stopped += 1
            except OSError:
                pass  # it ended by itself in the meantime
    return stopped
