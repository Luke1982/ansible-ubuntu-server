<?php
// A configuration profile (.mobileconfig) for Apple Mail on iPhone, iPad and
// Mac, at https://autoconfig.DOMAIN/apple: Apple uses neither autoconfig nor
// autodiscover for IMAP accounts. Without an address it shows a form asking
// for one; with ?email=ADDRESS it hands out the profile for that account:
// IMAP and SMTP at mail.DOMAIN over SSL, the address as user name. The
// password isn't in it: the device asks for it while installing the profile.
// Only addresses of the domain itself are accepted; whether the account
// exists isn't checked, so the page tells nobody which addresses exist.
declare(strict_types=1);
require __DIR__ . '/mailsettings.php';

$domain = mail_domain();
$email = strtolower(trim((string) ($_GET['email'] ?? '')));
$valid = $email !== ''
    && filter_var($email, FILTER_VALIDATE_EMAIL) !== false
    && str_ends_with($email, '@' . $domain);

if (!$valid) {
    form($domain, $email);
    exit;
}

$host = "mail.$domain";
// The same address always gets the same identifiers, so installing the
// profile again replaces the account instead of adding a second one.
$id = fn(string $kind): string => strtoupper(vsprintf(
    '%s%s-%s-%s-%s-%s%s%s',
    str_split(md5("$kind:$email"), 4)
));
$reverse = implode('.', array_reverse(explode('.', $domain)));
$x = fn(string $text): string => xml_text($text);

header('Content-Type: application/x-apple-aspen-config');
header('Content-Disposition: attachment; filename="' . $domain . '-mail.mobileconfig"');
header('Cache-Control: no-store');
echo <<<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>PayloadContent</key>
  <array>
    <dict>
      <key>PayloadType</key><string>com.apple.mail.managed</string>
      <key>PayloadVersion</key><integer>1</integer>
      <key>PayloadIdentifier</key><string>{$x("$reverse.mail.account." . $id('account'))}</string>
      <key>PayloadUUID</key><string>{$id('account')}</string>
      <key>PayloadDisplayName</key><string>{$x($email)}</string>
      <key>EmailAccountDescription</key><string>{$x($email)}</string>
      <key>EmailAccountType</key><string>EmailTypeIMAP</string>
      <key>EmailAddress</key><string>{$x($email)}</string>
      <key>IncomingMailServerHostName</key><string>{$x($host)}</string>
      <key>IncomingMailServerPortNumber</key><integer>993</integer>
      <key>IncomingMailServerUseSSL</key><true/>
      <key>IncomingMailServerAuthentication</key><string>EmailAuthPassword</string>
      <key>IncomingMailServerUsername</key><string>{$x($email)}</string>
      <key>OutgoingMailServerHostName</key><string>{$x($host)}</string>
      <key>OutgoingMailServerPortNumber</key><integer>465</integer>
      <key>OutgoingMailServerUseSSL</key><true/>
      <key>OutgoingMailServerAuthentication</key><string>EmailAuthPassword</string>
      <key>OutgoingMailServerUsername</key><string>{$x($email)}</string>
      <key>OutgoingPasswordSameAsIncomingPassword</key><true/>
    </dict>
  </array>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadVersion</key><integer>1</integer>
  <key>PayloadIdentifier</key><string>{$x("$reverse.mail." . $id('profile'))}</string>
  <key>PayloadUUID</key><string>{$id('profile')}</string>
  <key>PayloadDisplayName</key><string>{$x("E-mail $email")}</string>
  <key>PayloadDescription</key><string>{$x("Zet het e-mailaccount $email op dit apparaat. / Sets up the mail account $email on this device.")}</string>
  <key>PayloadOrganization</key><string>{$x($domain)}</string>
  <key>PayloadRemovalDisallowed</key><false/>
</dict>
</plist>

XML;

function form(string $domain, string $email): void
{
    $d = htmlspecialchars($domain, ENT_QUOTES | ENT_HTML5, 'UTF-8');
    $e = htmlspecialchars($email, ENT_QUOTES | ENT_HTML5, 'UTF-8');
    $error = $email === '' ? '' : "<p class=\"error\">Vul een adres in dat eindigt op @$d. / Enter an address ending in @$d.</p>";
    header('Content-Type: text/html; charset=utf-8');
    header('Cache-Control: no-store');
    echo <<<HTML
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E-mail instellen op iPhone, iPad of Mac – $d</title>
<style>
  body { font: 17px/1.5 -apple-system, system-ui, sans-serif; max-width: 34em; margin: 2em auto; padding: 0 1em; color: #1d1d1f; }
  h1 { font-size: 1.4em; }
  input[type=email] { font: inherit; width: 100%; box-sizing: border-box; padding: .6em; border: 1px solid #aaa; border-radius: 8px; }
  button { font: inherit; margin-top: .8em; padding: .6em 1.2em; border: 0; border-radius: 8px; background: #0071e3; color: #fff; }
  .error { color: #c00; }
  ol { padding-left: 1.2em; }
  .en { color: #555; font-size: .9em; margin-top: 2em; }
</style>
</head>
<body>
<h1>E-mail van $d instellen op iPhone, iPad of Mac</h1>
$error
<form method="get">
  <label for="email">Uw e-mailadres</label>
  <input type="email" id="email" name="email" value="$e" placeholder="naam@$d" required autocomplete="email" autocapitalize="none">
  <button type="submit">Download profiel</button>
</form>
<ol>
  <li>Open deze pagina in Safari en download het profiel.</li>
  <li>iPhone/iPad: Instellingen → Profiel gedownload → Installeer. Mac: Systeeminstellingen → Privacy en beveiliging → Profielen.</li>
  <li>Vul uw wachtwoord in als daarom gevraagd wordt.</li>
</ol>
<p class="en">English: enter your address and download the profile in Safari. Then install it in Settings (iPhone/iPad) or System Settings → Privacy &amp; Security → Profiles (Mac), and enter your password when asked.</p>
</body>
</html>

HTML;
}
