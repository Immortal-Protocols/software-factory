CREATE TABLE `gateway_routes` (
	`id` text PRIMARY KEY NOT NULL,
	`upstream` text NOT NULL,
	`token_hashes` text NOT NULL,
	`updated_at` text NOT NULL
);
