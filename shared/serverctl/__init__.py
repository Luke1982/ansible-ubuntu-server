"""Code shared by this repository's server tools (mailctl, domainctl, officectl).

The modules here know nothing about mail, web hosting or office documents: they edit OpenLiteSpeed's
configuration, look up and publish DNS records, talk to TransIP's API, run certbot and openssl, make Linux
users, databases and access control lists, and print to the terminal. Anything specific to one tool lives in
that tool's own package.

The source of truth is shared/serverctl in the Ansible repository. Each role's Ansible tasks copy this directory
next to the tool it installs, so a server carries one copy per tool and no tool depends on another being installed.
"""
