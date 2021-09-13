# Ansible server setup

## Steps to setup
- Create a VM, local or remote
- Install ubuntu
- Create a (ansible) user with a password and home directory
- Copy your public key to the authorized_keys file in the `.ssh` directory of that user's home directory (create that if it doesn't exist) (or use ssh-copy-id)

## To deploy

- Create a file called `inventory.yml` in the `private` directory. You can use `inventory-example.yml` in the root directory as a starting point.
- Add the DNS servers for your hosts, or host groups als a var with the name `dns_servers` followed by one or more space-separated DNS records.

### To configure webservers

- Create a file called `linuxusers.yml` in `roles/web/vars/private/` and populate that like documented in the README file in that directory.
- Do the same for `apachevhosts.yml` in the `roles/web/vars/private/` folder.

### To configure DB servers
No special configuration needed. Remember that MariaDB will allow `sudo mysql` as a user in the 'sudoers' group to allow you to do administrative stuff like create users and databases.

### To configure mailservers
- Create a file called `mailvars.yml` in `roles/mail/vars/private` according to the example you can find in the README in that directory.

### To start the deployment

Remember the username you created on the remote machine, and the password of that user. Now execute `ansible-playbook -u USER -K site.yml`. 
You'll be asked for a SUDO password, that should be the password of the user on the remote machine. After that you may be asked for your own SSH key password, if you've set that.

### How to create password hashes

You never store the actual password in a variable file. In stead you store the hash as saved in `/etc/shadow`. To make these passwords run `mkpasswd --method=sha-512`

## TODO

### Web
- Setup Zabbix
### Mail
- Setup Z-push (90% done)
  - src/config.php SYNC_MAX_ITEMS to 100
  - src/config.php BACKEND_PROVIDER BackendCombined
  - src/backends/combined: backends
  - src/backends/imap/config.php IMAP_FOLDER_SPAM = JUNK
  - src/backends/caldav/config.php CALDAV_SERVER (nextcloud URL)
  - src/backends/caldav/config.php CALDAV_PATH `/remote.php/dav/calendars/%u/'`
  - src/backends/caldav/config.php CALDAV_PERSONAL `Persoonlijk`?
  - src/backends/caldav/config.php CALDAV_SUPPORTS_SYNC `true`
  - src/backends/carddav/config.php CARDDAV_SERVER (nextcloud URL)
  - src/backends/carddav/config.php CARDDAV_PATH `/remote.php/dav/addressbooks/users/%u/contacts/`
  - src/backends/carddav/config.php CARDDAV_DEFAULT_PATH `/remote.php/dav/addressbooks/users/%u/contacts/`
  - src/backends/carddav/config.php CARDDAV_CONTACTS_FOLDER_NAME `%u Contactpersonen`
  - src/backends/carddav/config.php CARDDAV_SUPPORTS_SYNC `true`
  - https://forum.kopano.io/topic/1634/centos7-no-matter-what-i-get-access-denied-please-send-authorisation-information/5?_=1631478031707
- Setup webmail subdomains	
