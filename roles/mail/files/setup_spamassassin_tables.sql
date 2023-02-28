CREATE TABLE  IF NOT EXISTS  `userpref` (
  `username` varchar(100) NOT NULL,
  `preference` varchar(100) NOT NULL,
  `value` varchar(100) NOT NULL,
  `prefid` int(11) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;