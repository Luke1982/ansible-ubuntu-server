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



Remember the username you created on the remote machine, and the password of that user. Now execute `ansible-playbook -u USER -K site.yml`. 
You'll be asked for a SUDO password, that should be the password of the user on the remote machine. After that you may be asked for your own SSH key password, if you've set that.

### How to create password hashes

You never store the actual password in a variable file. In stead you store the hash as saved in `/etc/shadow`. To make these passwords run `mkpasswd --method=sha-512`

## TODO

### Web
- Setup virtual hosts
- Setup Zabbix
### Mail
- Setup Z-push
