<?php
// Thunderbird's autoconfig (https://wiki.mozilla.org/Thunderbird:Autoconfiguration):
// the IMAP and SMTP servers of the domain. Thunderbird fills in %EMAILADDRESS%.
declare(strict_types=1);
require __DIR__ . '/mailsettings.php';

$domain = xml_text(mail_domain());
$host = "mail.$domain";

header('Content-Type: application/xml; charset=utf-8');
echo <<<XML
<?xml version="1.0" encoding="UTF-8"?>
<clientConfig version="1.1">
  <emailProvider id="$domain">
    <domain>$domain</domain>
    <displayName>$domain</displayName>
    <displayShortName>$domain</displayShortName>
    <incomingServer type="imap">
      <hostname>$host</hostname>
      <port>993</port>
      <socketType>SSL</socketType>
      <authentication>password-cleartext</authentication>
      <username>%EMAILADDRESS%</username>
    </incomingServer>
    <incomingServer type="imap">
      <hostname>$host</hostname>
      <port>143</port>
      <socketType>STARTTLS</socketType>
      <authentication>password-cleartext</authentication>
      <username>%EMAILADDRESS%</username>
    </incomingServer>
    <outgoingServer type="smtp">
      <hostname>$host</hostname>
      <port>465</port>
      <socketType>SSL</socketType>
      <authentication>password-cleartext</authentication>
      <username>%EMAILADDRESS%</username>
    </outgoingServer>
    <outgoingServer type="smtp">
      <hostname>$host</hostname>
      <port>587</port>
      <socketType>STARTTLS</socketType>
      <authentication>password-cleartext</authentication>
      <username>%EMAILADDRESS%</username>
    </outgoingServer>
  </emailProvider>
</clientConfig>

XML;
