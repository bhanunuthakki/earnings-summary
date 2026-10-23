# Personal-use information boundaries

The application trusts the owner's devices and private network. It does not require a separate application login. Keep the dashboard behind private Tailscale Serve, keep backends on loopback, and limit network access to trusted devices. Remote desktop, device accounts, and provider accounts are separate access boundaries.

## Public source

Git ignore rules prevent ordinary inclusion; they do not control access to files. Keep credentials, account records, private reports, and operational receipts out of public Git history. The pre-push gate checks every outgoing commit, including private material removed before the final commit. Its scanner is a guard against known patterns, not proof that all possible personal information is absent. Review the exact staged paths and content before publication.

## Backups and AI providers

The weekly workspace backup includes private notes, research files, repository history, and consistent database snapshots. That shared archive is compressed, not encrypted by the application. The owner accepts Google Drive as a trusted private-data boundary. Separate encrypted database backups do not encrypt the full workspace archive. Do not change exclusions or delete old copies without preserving recovery coverage.

Hosted AI features receive the portfolio and research context required for their purposes. This can include holdings, weights, account/tax context, and owner notes. Provider accounts and retention settings are therefore part of the privacy boundary. Log redaction does not remove this intentional research context from model requests.

## Reports

Interactive standalone reports are private application artifacts. They include stored comments and a capability used to connect to the local application. Do not publish those files.

To create a separate offline reading copy:

```sh
python execution/sqlite_bootstrap.py execution/export_shareable_report.py private-report.html reading-copy.html
```

The destination must be new. Export removes hidden comment payloads, application capabilities and connections. It retains report reading/navigation and public citation links. The visible analysis can still contain sensitive notes or financial information: review it before sharing. Existing private reports retain their interactions.

## Device protection

Malicious software running with the owner's permissions can read private files. Installed service code and its interpreter must not be writable by restricted sandbox accounts. Protect disks with encryption and keep recovery keys outside the affected device. Preparing a recovery key does not mean encryption is enabled. Never put a recovery key in Git, logs, or chat.
