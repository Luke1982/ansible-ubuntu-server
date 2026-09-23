"""Code shared by this repository's server tools (mailctl, domainctl).

The modules here know nothing about mail or web hosting: they edit OpenLiteSpeed's configuration, look up DNS,
talk to TransIP's API, run certbot and openssl, and print to the terminal. Anything specific to one tool lives in
that tool's own package.

The source of truth is shared/serverctl in the Ansible repository. Each role's Ansible tasks copy this directory
next to the tool it installs, so a server carries one copy per tool and no tool depends on another being installed.
"""
