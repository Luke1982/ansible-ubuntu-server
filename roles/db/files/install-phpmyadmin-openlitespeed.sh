#!/bin/bash

if [ -d /usr/local/lsws/Example/html/phpmyadmin ]; then
    echo "PHPMyAdmin is already installed"
    exit 0
fi

cd /usr/local/lsws/Example/html
wget -q https://www.phpmyadmin.net/downloads/phpMyAdmin-latest-all-languages.zip
unzip phpMyAdmin-latest-all-languages.zip
rm phpMyAdmin-latest-all-languages.zip
mv phpMyAdmin-*-all-languages phpmyadmin
mv phpmyadmin/config.sample.inc.php phpmyadmin/config.inc.php