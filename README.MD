# Ansible server setup

## Steps to setup
- Create a VM, local or remote
- Install ubuntu
- Create a user with a password and home directory
- Copy your public key to the authorized_keys file in the `.ssh` directory of that user's home directory (create that if it doesn't exist)
- Add your IP or resolvable hostname to the hosts of each group you want in `inventory.yml`

## To deploy

First edit the inventory file. Mainly: add the DNS servers for your hosts, or host groups als a var with the name `dns_servers` followed by one or more space-separated DNS records

Remember the username you created on the remote machine, and the password of that user. Now execute `ansible-playbook -u USER -K site.yml`. 
You'll be asked for a SUDO password, that should be the password of the user on the remote machine. After that you may be asked for your own SSH key password, if you've set that.

## TODO

### Web
- Setup IDS
- Setup virtual hosts
- Setup FTP