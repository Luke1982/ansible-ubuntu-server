#!/bin/sh

IPT="/sbin/iptables"

# Your DNS servers you use: cat /etc/resolv.conf
DNS_SERVER="127.0.0.53"

# FLush all iptables rules
iptables --flush

# Setup the policies
iptables --policy INPUT DROP
iptables --policy OUTPUT ACCEPT

# Custom block rules for known attackers
$IPT -A INPUT -s 58.218.198.154 -j DROP
$IPT -A INPUT -s 54.171.83.126 -j DROP

## This should be one of the first rules.
## so dns lookups are already allowed for your other rules
for ip in $DNS_SERVER
do
	echo "Allowing DNS lookups (tcp, udp port 53) to server '$ip'"
	$IPT -A OUTPUT -p udp -d $ip --dport 53 -m state --state NEW,ESTABLISHED -j ACCEPT
	$IPT -A INPUT  -p udp -s $ip --sport 53 -m state --state ESTABLISHED     -j ACCEPT
	$IPT -A OUTPUT -p tcp -d $ip --dport 53 -m state --state NEW,ESTABLISHED -j ACCEPT
	$IPT -A INPUT  -p tcp -s $ip --sport 53 -m state --state ESTABLISHED     -j ACCEPT
done

# Enable loopback
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT

## Enable ports on input
# Enable established connections to return data
$IPT -A INPUT -m state --state ESTABLISHED -j ACCEPT
# Web server
iptables -A INPUT -p tcp --dport 80 -s 0/0 -m state --state NEW,ESTABLISHED -j ACCEPT
iptables -A INPUT -p tcp --dport 443 -s 0/0 -m state --state NEW,ESTABLISHED -j ACCEPT
# Mail server
iptables -A INPUT -p tcp --dport 143 -s 0/0 -m state --state NEW,ESTABLISHED -j ACCEPT
iptables -A INPUT -p tcp --dport 25 -s 0/0 -m state --state NEW,ESTABLISHED -j ACCEPT
iptables -A INPUT -p tcp --dport 587 -s 0/0 -m state --state NEW,ESTABLISHED -j ACCEPT
iptables -A INPUT -p tcp --dport 465 -s 0/0 -m state --state NEW,ESTABLISHED -j ACCEPT
iptables -A INPUT -p tcp --dport 993 -s 0/0 -m state --state NEW,ESTABLISHED -j ACCEPT
#Open DKIM
iptables -A INPUT -p tcp --dport 12301 -s 0/0 -m state --state NEW,ESTABLISHED -j ACCEPT
# SSH
iptables -A INPUT -p tcp --dport 22 -s 0/0 -j ACCEPT
# FTP
iptables -A INPUT -p tcp -m tcp --dport 21 -m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT
iptables -A INPUT -p tcp -m tcp --dport 20 -m conntrack --ctstate NEW,ESTABLISHED -j ACCEPT
# Following rules opens everything above port 5000 for FTP traffic after connection is established
iptables -A INPUT -p tcp -m tcp --sport 5000: --dport 5000: -m conntrack --ctstate ESTABLISHED -j ACCEPT
