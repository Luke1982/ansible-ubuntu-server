#!/bin/bash
# Installs the latest phpMyAdmin in OpenLiteSpeed's Example site, once. It's
# unpacked in a temporary directory and moved into place whole, so a run that
# stops halfway leaves nothing behind for the next one to trip over.
set -euo pipefail

site=/usr/local/lsws/Example/html
target=$site/phpmyadmin

if [ -d "$target" ]; then
    echo "PHPMyAdmin is already installed"
    exit 0
fi

# Left in the site by earlier versions of this script that stopped halfway
rm -rf "$site"/phpMyAdmin-*-all-languages "$site"/phpMyAdmin-latest-all-languages.zip

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
wget -q -O "$work/phpmyadmin.zip" https://www.phpmyadmin.net/downloads/phpMyAdmin-latest-all-languages.zip
unzip -q "$work/phpmyadmin.zip" -d "$work"
unpacked=$(echo "$work"/phpMyAdmin-*-all-languages)
mv "$unpacked/config.sample.inc.php" "$unpacked/config.inc.php"
mv "$unpacked" "$target"
echo "Installed $(basename "$unpacked") in $target"
