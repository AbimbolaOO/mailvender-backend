# Data retention

Enforced daily by the worker's `retention` job (`LifecycleService.enforce_retention`);
periods are configurable (`RETENTION_*` variables, defaults below).

| Data                                                          | Kept                                            | Then                                                                                                                       |
| ------------------------------------------------------------- | ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Message HTML / text bodies                                    | 30 days                                         | Bodies erased (`content_purged_at`); "view in browser" links stop working                                                  |
| Messages and recipient metadata (address, status, timestamps) | 90 days                                         | Deleted with their delivery attempts                                                                                       |
| Delivery events (bounces, complaints)                         | 180 days                                        | Deleted (health rates use the last 7 days)                                                                                 |
| Suppressions                                                  | While the account exists, and after deletion    | Kept: an unsubscribed or complaining address must never be mailed again. Only manual suppressions can be removed by users. |
| Audit events                                                  | Indefinitely                                    | Immutable (database trigger)                                                                                               |
| Sessions                                                      | Until expiry (14 days) or logout                | Deleted 30 days after expiry/revocation                                                                                    |
| Email-verification / reset tokens                             | 48 h / 60 min                                   | Deleted 7 days after expiry                                                                                                |
| Invitations                                                   | 7 days                                          | Kept for audit                                                                                                             |
| Outbox events                                                 | Until processed                                 | Deleted 7 days later; system-email bodies (which hold link tokens) are erased as soon as they're sent                      |
| Rate-limit counters                                           | Their window                                    | Deleted after 2 days                                                                                                       |
| Account exports                                               | 24 hours                                        | File deleted, record marked expired                                                                                        |
| DKIM keys                                                     | While active                                    | Retired keys deleted 30 days after rotation                                                                                |
| Abandoned uploads                                             | 1 hour after the upload URL expired             | Deleted                                                                                                                    |
| Assets                                                        | Until deleted by the user, or the account is purged | Public object deleted immediately                                                                                      |
| Deleted accounts                                              | 30 days (restorable by an owner until then)     | Images, workspace data (pages, components, brand, datasets, versions), memberships and postal address deleted; suppressions and audit kept |
| Emails written in the admin (Sent) and their files | 180 days (`RETENTION_INBOUND_MAIL_DAYS`) | Deleted with their files; uploads never sent are deleted after 1 day |
| Deleted logins (users)                                        | Erased at once                                  | Email replaced with `deleted-<id>@deleted.invalid`, password replaced, sessions/tokens deleted; the row stays so audit history makes sense |
| Backups                                                       | 30 days (database PITR, S3 noncurrent versions) | Expire                                                                                                                     |

## Account (workspace) deletion and restore

`POST /v1/account/deletion` (owners, confirming the account name) immediately:
revokes the account's API keys and its members' sessions, disables its domains
(freeing the names) and senders, and fails pending sends and cancels their
outbox events. Owners get an email with the date it will be erased. It's
recorded in the audit log.

For the next `RETENTION_DELETED_ACCOUNT_DAYS` (30) nothing else is removed:
memberships, images and workspace data stay, hidden. An owner sees it in
`deleted_accounts` of `GET /v1/auth/me` (Settings → You, or the "no workspace"
screen) and can bring it back with `POST /v1/accounts/{id}/restore`. Everyone
regains access; domains come back as `pending` (check DNS to verify them again)
unless someone else registered the same domain meanwhile; senders come back as
`pending`; revoked API keys stay revoked.

After that the retention job erases images, workspace data, memberships and the
postal address. Suppressions and audit events stay.

## Login (person) deletion

`POST /v1/auth/me/deletion` (the signed-in person, with their password):

- Refused (409 `owns_shared_accounts`, listing them) while they own an account
  other people are in: they make someone else the owner, or delete it, first.
- Accounts they alone are in are deleted as above, but nobody can restore them.
- They leave every other account; their sessions, email tokens and pending
  invitations to their address are removed.
- Their email address and password are replaced, so the address can sign up
  again. The row stays (audit events refer to it) with `deleted_at` set; admins
  see the person as "deleted".
- A goodbye email goes to the old address. Recorded as `user.deleted`.

## Export

`POST /v1/account/exports` (owners) builds a ZIP – workspace records (with each
dataset's content), members, domains, senders, assets with the image files
(`images/`), message metadata and recipient statuses (no bodies), and
suppressions – in a temporary file, streamed to private storage. Owners
download it for 24 hours through `POST /v1/account/exports/{id}/link`, a
5-minute link straight to storage (large exports never pass through the API).
Requests, completions and downloads are audited.

`POST /v1/auth/me/export` (the signed-in person) returns their own data at once:
profile, the accounts they're in, invitations to their address, active sessions
and their activity (audit events they caused). Audited as `user.exported`.
