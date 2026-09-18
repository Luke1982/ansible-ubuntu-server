-- The mail accounts as SOGo reads them (its SQL user source, see templates/sogo.conf.j2).
-- The view runs with the rights of its definer, so the sogo user needs none on mailserver.
-- It reads one table without joins, so SOGo's password changes can go through it.
CREATE OR REPLACE SQL SECURITY DEFINER VIEW sogo_users AS
SELECT email AS c_uid,
       email AS c_name,
       password AS c_password,
       email AS c_cn,
       email AS mail,
       SUBSTRING_INDEX(email, '@', -1) AS c_domain
FROM mailserver.virtual_users;
