#!/bin/bash
# Installs the latest phpMyAdmin in a directory of its own, once, and takes along
# the copy that earlier versions of this script put in OpenLiteSpeed's Example
# site, where every name that site answers to could reach it. It's unpacked in a
# temporary directory and moved into place whole, so a run that stops halfway
# leaves nothing behind for the next one to trip over.
#
# Says "Changed:" when it did something, so Ansible can see it.
set -euo pipefail

target=${1:?Usage: install-phpmyadmin.sh DIRECTORY}
example=/usr/local/lsws/Example/html/phpmyadmin

# Left in the Example site by earlier versions of this script that stopped halfway
rm -rf /usr/local/lsws/Example/html/phpMyAdmin-*-all-languages \
       /usr/local/lsws/Example/html/phpMyAdmin-latest-all-languages.zip

if [ -f "$target/index.php" ]; then
    if [ -e "$example" ]; then
        rm -rf "$example"
        echo "Changed: took phpMyAdmin out of OpenLiteSpeed's Example site; $target serves it now."
    else
        echo "No change: phpMyAdmin is already installed in $target."
    fi
else
    mkdir -p "$(dirname "$target")"
    if [ -d "$example" ]; then
        # The copy in the Example site moves to its own place, with its settings.
        mv "$example" "$target"
        echo "Changed: moved phpMyAdmin from OpenLiteSpeed's Example site to $target."
    else
        work=$(mktemp -d)
        trap 'rm -rf "$work"' EXIT
        wget -q -O "$work/phpmyadmin.zip" https://www.phpmyadmin.net/downloads/phpMyAdmin-latest-all-languages.zip
        unzip -q "$work/phpmyadmin.zip" -d "$work"
        unpacked=$(echo "$work"/phpMyAdmin-*-all-languages)
        mv "$unpacked" "$target"
        echo "Changed: installed $(basename "$unpacked") in $target."
    fi
fi

# Cookie login needs a passphrase of its own, 32 characters long. Without one
# phpMyAdmin makes up a new one whenever it restarts, which logs everybody out,
# and says so on every page. Earlier versions of this script left the sample
# settings in place, which have none: those are replaced, and kept next to it.
if ! grep -qs "blowfish_secret'\] = '[^']" "$target/config.inc.php"; then
    if [ -f "$target/config.inc.php" ]; then
        mv "$target/config.inc.php" "$target/config.inc.php.previous"
    fi
    (
        umask 027
        cat > "$target/config.inc.php" <<CONFIG
<?php
// Written when phpMyAdmin was installed. Changes made here are kept.
\$cfg['blowfish_secret'] = '$(head -c 24 /dev/urandom | base64)';
\$i = 0;

\$i++;
\$cfg['Servers'][\$i]['auth_type'] = 'cookie';
\$cfg['Servers'][\$i]['host'] = 'localhost';
\$cfg['Servers'][\$i]['compress'] = false;
\$cfg['Servers'][\$i]['AllowNoPassword'] = false;

\$cfg['TempDir'] = '$target/tmp';
CONFIG
    )
    echo "Changed: gave phpMyAdmin its own settings in $target/config.inc.php."
fi
