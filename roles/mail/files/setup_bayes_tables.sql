
CREATE TABLE IF NOT EXISTS `bayes_expire` (
  id int(11) NOT NULL default '0',
  runtime int(11) NOT NULL default '0',
  KEY bayes_expire_idx1 (id)
);

CREATE TABLE IF NOT EXISTS `bayes_global_vars` (
  variable varchar(30) NOT NULL default '',
  value varchar(200) NOT NULL default '',
  PRIMARY KEY  (variable)
);

DELETE FROM `bayes_global_vars` WHERE `bayes_global_vars`.`variable` = 'VERSION';
INSERT INTO bayes_global_vars VALUES ('VERSION','3');

CREATE TABLE IF NOT EXISTS `bayes_seen` (
  id int(11) NOT NULL default '0',
  msgid varchar(200) binary NOT NULL default '',
  flag char(1) NOT NULL default '',
  PRIMARY KEY  (id,msgid)
);

CREATE TABLE IF NOT EXISTS `bayes_token` (
  id int(11) NOT NULL default '0',
  token char(5) NOT NULL default '',
  spam_count int(11) NOT NULL default '0',
  ham_count int(11) NOT NULL default '0',
  atime int(11) NOT NULL default '0',
  PRIMARY KEY  (id, token),
  INDEX bayes_token_idx1 (id, atime)
);

CREATE TABLE IF NOT EXISTS `bayes_vars` (
  id int(11) NOT NULL AUTO_INCREMENT,
  username varchar(200) NOT NULL default '',
  spam_count int(11) NOT NULL default '0',
  ham_count int(11) NOT NULL default '0',
  token_count int(11) NOT NULL default '0',
  last_expire int(11) NOT NULL default '0',
  last_atime_delta int(11) NOT NULL default '0',
  last_expire_reduce int(11) NOT NULL default '0',
  oldest_token_age int(11) NOT NULL default '2147483647',
  newest_token_age int(11) NOT NULL default '0',
  PRIMARY KEY (id),
  UNIQUE bayes_vars_idx1 (username)
);
