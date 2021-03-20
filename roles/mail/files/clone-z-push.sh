#!/bin/bash

DOMAIN=$1

## Avoid error when trying to clone into an already existing directory
if [ ! -d "$DOMAIN" ] ; then
	git clone --single-branch --branch=master https://github.com/Z-Hub/Z-Push ${DOMAIN}
fi