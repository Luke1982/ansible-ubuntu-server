<?php
// Outlook's autodiscover (the "POX" protocol): the IMAP and SMTP servers of the
// domain, and the address from the request as the login. Outlook posts to
// /autodiscover/autodiscover.xml. Requests for other schemas, like phones asking
// for ActiveSync, get a 404, so they fall back to asking the user.
declare(strict_types=1);
require __DIR__ . '/mailsettings.php';

const OUTLOOK = 'http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a';

$domain = mail_domain();
$request = (string) file_get_contents('php://input');
$tag = fn(string $name): string => preg_match("#<$name>\\s*([^<\\s]+)\\s*</$name>#i", $request, $found)
    ? html_entity_decode($found[1], ENT_XML1 | ENT_QUOTES, 'UTF-8') : '';

$schema = $tag('AcceptableResponseSchema');
if ($schema !== '' && strcasecmp($schema, OUTLOOK) !== 0) {
    http_response_code(404);
    exit;
}
$host = xml_text("mail.$domain");
$address = $tag('EMailAddress');
$login = $address === '' ? '' : '<LoginName>' . xml_text($address) . '</LoginName>';
$outlook = OUTLOOK;

header('Content-Type: application/xml; charset=utf-8');
echo <<<XML
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
  <Response xmlns="$outlook">
    <Account>
      <AccountType>email</AccountType>
      <Action>settings</Action>
      <Protocol>
        <Type>IMAP</Type>
        <Server>$host</Server>
        <Port>993</Port>
        <DomainRequired>off</DomainRequired>
        $login
        <SPA>off</SPA>
        <SSL>on</SSL>
        <AuthRequired>on</AuthRequired>
      </Protocol>
      <Protocol>
        <Type>SMTP</Type>
        <Server>$host</Server>
        <Port>465</Port>
        <DomainRequired>off</DomainRequired>
        $login
        <SPA>off</SPA>
        <SSL>on</SSL>
        <AuthRequired>on</AuthRequired>
        <UsePOPAuth>on</UsePOPAuth>
        <SMTPLast>off</SMTPLast>
      </Protocol>
    </Account>
  </Response>
</Autodiscover>

XML;
