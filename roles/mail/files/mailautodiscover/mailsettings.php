<?php
// Shared by autoconfig.php and autodiscover.php, which OpenLiteSpeed serves at
// autoconfig.DOMAIN and autodiscover.DOMAIN (mailctl autodiscover publish).
declare(strict_types=1);

// The mail domain, from the name the request came in on. Mail programs connect
// to mail.DOMAIN for IMAP and for sending.
function mail_domain(): string
{
    $host = strtolower(explode(':', $_SERVER['HTTP_HOST'] ?? '')[0]);
    $domain = preg_replace('/^(autoconfig|autodiscover)\./', '', $host);
    if (!preg_match('/^(?:[a-z0-9-]+\.)+[a-z0-9-]*[a-z][a-z0-9-]*$/', $domain)) {
        http_response_code(404);
        exit;
    }
    return $domain;
}

function xml_text(string $text): string
{
    return htmlspecialchars($text, ENT_XML1 | ENT_QUOTES, 'UTF-8');
}
