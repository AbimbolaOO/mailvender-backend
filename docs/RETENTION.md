# Data retention

Enforced daily by the worker's `retention` job (`LifecycleService.enforce_retention`);
periods are configurable (`RETENTION_*` variables, defaults below).

| Data | Kept | Then |
| --- | --- | --- |
| Message HTML / text bodies | 30 days | Bodies erased (`content_purged_at`); "view in browser" links stop working |
| Messages and recipient metadata (address, status, timestamps) | 90 days | Deleted with their delivery attempts |
| Delivery events (bounces, complaints) | 180 days | Deleted (health rates use the last 7 days) |
| Suppressions | While the account exists, and after deletion | Kept: an unsubscribed or complaining address must never be mailed again. Only manual suppressions can be removed by users. |
| Audit events | Indefinitely | Immutable (database trigger) |
| Sessions | Until expiry (14 days) or logout | Deleted 30 days after expiry/revocation |
| Email-verification / reset tokens | 48 h / 60 min | Deleted 7 days after expiry |
| Invitations | 7 days | Kept for audit |
| Outbox events | Until processed | Deleted 7 days later; system-email bodies (which hold link tokens) are erased as soon as they're sent |
| Rate-limit counters | Their window | Deleted after 2 days |
| Account exports | 24 hours | File deleted, record marked expired |
| DKIM keys | While active | Retired keys deleted 30 days after rotation |
| Abandoned uploads | 1 hour after the upload URL expired | Deleted |
| Assets | Until deleted by the user | Public object deleted immediately |
| Deleted accounts | 30 days | Workspace data (pages, components, brand, datasets, versions) and postal address deleted; suppressions and audit kept |
| Backups | 30 days (database PITR, S3 noncurrent versions) | Expire |

## Account deletion

`POST /v1/account/deletion` (owners, confirming the account name) immediately:
revokes the account's API keys and its members' sessions, disables its domains
(freeing the names) and senders, fails pending sends and cancels their outbox
events, removes memberships, and queues deletion of every asset. It's recorded
in the audit log. The remaining data follows the table above.

## Export

`POST /v1/account/exports` (owners) builds a ZIP of JSON – workspace records,
members, domains, senders, asset metadata, message metadata and recipient
statuses (no bodies), and suppressions – downloadable by owners for 24 hours.
Requests, completions and downloads are audited.
