<?php
// Autodiscover (the "POX" protocol), which Outlook and phones post to
// /autodiscover/autodiscover.xml. Each says in the request which answer it
// understands: Outlook asks for the IMAP and SMTP servers, a phone asks for
// ActiveSync, which SOGo serves on the webmail site. A schema we don't know
// gets a 404, so the program falls back to asking the user.
declare(strict_types=1);
require __DIR__ . '/mailsettings.php';

const OUTLOOK = 'http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a';
const MOBILE = 'http://schemas.microsoft.com/exchange/autodiscover/mobilesync/responseschema/2006';

$domain = mail_domain();
$request = (string) file_get_contents('php://input');
$tag = fn(string $name): string => preg_match("#<$name>\\s*([^<\\s]+)\\s*</$name>#i", $request, $found)
    ? html_entity_decode($found[1], ENT_XML1 | ENT_QUOTES, 'UTF-8') : '';

$schema = $tag('AcceptableResponseSchema');
$address = $tag('EMailAddress');

if (strcasecmp($schema, MOBILE) === 0) {
    // ActiveSync is served by the webmail site, not by mail.DOMAIN, which has no
    // web site at all: a phone left to guess asks mail.DOMAIN and is answered
    // with a 404. A domain whose webmail isn't set up yet has no ActiveSync
    // either, and "mailctl doctor" says so about the domain itself.
    $url = xml_text("https://webmail.$domain/Microsoft-Server-ActiveSync");
    $user = xml_text($address);
    $mobile = MOBILE;
    header('Content-Type: application/xml; charset=utf-8');
    echo <<<XML
    <?xml version="1.0" encoding="utf-8"?>
    <Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
      <Response xmlns="$mobile">
        <Culture>en:us</Culture>
        <User>
          <DisplayName>$user</DisplayName>
          <EMailAddress>$user</EMailAddress>
        </User>
        <Action>
          <Settings>
            <Server>
              <Type>MobileSync</Type>
              <Url>$url</Url>
              <Name>$url</Name>
            </Server>
          </Settings>
        </Action>
      </Response>
    </Autodiscover>

    XML;
    exit;
}

if ($schema !== '' && strcasecmp($schema, OUTLOOK) !== 0) {
    http_response_code(404);
    exit;
}
$host = xml_text("mail.$domain");
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
