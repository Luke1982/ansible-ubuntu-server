#!/bin/bash

DOMAIN=$1
RED='\033[0;31m'
FILE="/etc/opendkim/TrustedHosts"
NOTYETPRESENT=`grep -F "$DOMAIN" $FILE`

# Add domain to the trustedhosts file
if [[ $NOTYETPRESENT == "" ]]; then
	sudo su <<EOF
	echo "*.${DOMAIN}" >> $FILE
EOF
fi

FILE="/etc/opendkim/KeyTable"
NOTYETPRESENT=`grep -F "$DOMAIN" $FILE`

# Add domain to the keytable file
if [[ $NOTYETPRESENT == "" ]]; then
	sudo su <<EOF
	echo "mail._domainkey.${DOMAIN} ${DOMAIN}:mail:/etc/opendkim/keys/${DOMAIN}/mail.private" >> $FILE
EOF
fi

FILE="/etc/opendkim/SigningTable"
NOTYETPRESENT=`grep -F "$DOMAIN" $FILE`

# Add domain to the signingtable
if [[ $NOTYETPRESENT == "" ]]; then
	sudo su <<EOF
	echo "*@${DOMAIN} mail._domainkey.${DOMAIN}" >> $FILE
EOF
fi

# Make the keys directory, set permissions and generate keys
if [[ $NOTYETPRESENT == "" ]]; then
	sudo -u root bash <<EOF
	mkdir /etc/opendkim/keys/${DOMAIN}
	cd /etc/opendkim/keys/${DOMAIN}
	opendkim-genkey -s mail -d ${DOMAIN}
	chown opendkim:opendkim mail.private
	cat mail.txt
EOF
fi

echo -e "${RED}Let op:${NC} opendkim moet nog wel opnieuw gestart worden en uiteraard moet bovenstaande"
echo "sleutel nog aan de DNS toegevoegd worden"
