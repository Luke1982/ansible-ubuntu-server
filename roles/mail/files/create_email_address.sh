#!/bin/bash

# Setting up the arguments variables, so we can error on empty ones later
EMAIL=
PASSWORD=

# Getting the passed arguments
while getopts "e:p:" option; do
        case $option in
                e) EMAIL=$OPTARG;;
                p) PASSWORD=$OPTARG;;
        esac
done

# Error out when one of the arguments is missing
if [[ -z $EMAIL ]] || [[ -z $PASSWORD ]]; then
        echo "Missing an argument. Pass e for email, p for passowrd"
        exit
fi

# Split the e-mail address on the '@' to get the domain name
DOMAIN_ARRAY=(${EMAIL//@/ })

# Create the domain in the virtual domains
mysql -D mailserver -e "INSERT IGNORE INTO virtual_domains (name) VALUES ('${DOMAIN_ARRAY[1]}')"

# Hash the password
PASSWORD_HASH=$(dovecot pw -s SHA256-CRYPT -p ${PASSWORD})

# Create the user, update password if already exists
mysql -D mailserver -e "INSERT INTO virtual_users (domain_id, email, password) VALUES ((SELECT id FROM virtual_domains WHERE name = '${DOMAIN_ARRAY[1]}'), '${EMAIL}', '${PASSWORD_HASH}') ON DUPLICATE KEY UPDATE password='${PASSWORD_HASH}'"

# Create entry in sender alias table
mysql -D mailserver -e "INSERT INTO virtual_sender_aliases (domain_id, users, alias) VALUES ((SELECT id FROM virtual_domains WHERE name = '${DOMAIN_ARRAY[1]}'), '${EMAIL}', '${EMAIL}')"

# Create directories
sudo -u vmail mkdir -p /var/vmail/${DOMAIN_ARRAY[1]}/${DOMAIN_ARRAY[0]}/Maildir/.Sent
sudo -u vmail mkdir -p /var/vmail/${DOMAIN_ARRAY[1]}/${DOMAIN_ARRAY[0]}/Maildir/.Trash
sudo -u vmail mkdir -p /var/vmail/${DOMAIN_ARRAY[1]}/${DOMAIN_ARRAY[0]}/Maildir/.Junk
cd /var/vmail/${DOMAIN_ARRAY[1]}/${DOMAIN_ARRAY[0]}/Maildir

sudo -u vmail ln -s .Sent .Verzonden\ items
sudo -u vmail ln -s .Sent .Verzonden\ Items
sudo -u vmail ln -s .Sent .Sent\ Messages
sudo -u vmail ln -s .Sent .Sent\ Items

sudo -u vmail ln -s .Trash .Verwijderde\ items
sudo -u vmail ln -s .Trash .Deleted\ Messages

sudo -u vmail ln -s .Junk .Ongewenste\ e-mail
