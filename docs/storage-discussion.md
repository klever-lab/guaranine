# Storage portability discussion

Discussed on 2026-11-17. All implementation is deferred.

The goal was to reduce how many workloads depend on a particular worker, recover from loss of a worker or disk, and keep the cloud K3s controller small. Cross-worker storage would need to tolerate intermittent Wi-Fi and Tailscale links. [Current storage and recovery](storage.md#capacity-outages-and-backups) · [Controller availability](architecture.md#workloads-and-availability)

## Worker placement at the time of the audit

Six continuously running pods depended on local storage.

| Worker | Pods | Count |
| --- | --- | --- |
| `demure` | Argument Arena, Calibre-Web, Grafana, qBittorrent, download file gateway | 5 |
| `muller` | SABnzbd | 1 |

Each scheduled book importer also ran on its downloader's worker when active. The earlier target of three pinned pods assumed SABnzbd could use temporary storage and both SQLite apps could move automatically. qBittorrent's bulk downloads and the gateway that serves them were expected to stay on the local disk. Calibre-Web would still use a local library with an off-site backup. [Downloader storage and imports](https://github.com/klever-lab/self-hosted/blob/main/docs/media-imports.md)

## Configuration and SQLite

Small static settings fit in ConfigMaps. Credentials such as Usenet logins, private feed URLs and VPN keys belong in Secrets. qBittorrent and SABnzbd generate writable state that can include credentials, so protect live config files and their backups. Keep runtime state and database files on app storage to limit what the controller stores.

Argument Arena used about 4 MiB including its SQLite write-ahead log. Grafana used about 6 MiB. The simplest recovery option discussed was to keep SQLite, take consistent snapshots using SQLite's backup API, and copy each snapshot to another worker over SSH through Tailscale. The last successful snapshot is the recovery point. Starting on another worker requires a manual restore and fencing the old writer if it is unreachable. Grafana's backup needs its secret key as well as its database. Argument Arena's existing backup and restore commands require the bot to be stopped. [SQLite backup API](https://sqlite.org/backup.html) · [Grafana storage](monitoring.md#storage-and-reapplication) · [Argument Arena operations](https://github.com/KianBahasadri/argument-arena/blob/main/docs/operations.md)

### Alternatives considered

- **Shared PostgreSQL.** OVH listed a `d2-2` at CA$9.14/month for compute, plus public IPv4 if needed. On a single self-managed VM, one outage would affect both apps. OVH Web Cloud Databases listed at CA$10.99/month with daily backups and unverified automatic failover. Public Cloud PostgreSQL listed at CA$87.09/month for a single node and CA$221.77/month for two nodes with automatic failover. These were Canadian catalog prices before tax on 2026-11-17. [OVH Public Cloud pricing](https://www.ovhcloud.com/en-ca/public-cloud/prices/#postgresql) · [Web Cloud Databases](https://www.ovhcloud.com/en-ca/web-cloud/databases/) · [PostgreSQL failover](https://docs.ovhcloud.com/en/guides/public-cloud/databases/postgresql-concept-high-availability)
- **Litestream over SFTP.** Continuous asynchronous copies reduce the likely recovery gap when the link is healthy. This still needs a receiver, replication configuration and a restore on the replacement worker. That was more setup than desired for these databases. [Litestream SFTP](https://litestream.io/guides/sftp/) · [Replication lag](https://litestream.io/tips/#data-loss-window)
- **Replicated Kubernetes volumes.** Piraeus with DRBD would let a replacement pod attach an up-to-date copy on a surviving worker with storage quorum. It needs dependable peers, a compatible kernel module, and testing for both worker loss and a Tailscale partition. The published Hadron layer checked was built for `7.2.6-hadron` while the workers ran `7.3.0-hadron`. OpenEBS Mayastor is a separate engine from the installed Hostpath provisioner and reserves at least 2 GiB of huge pages per storage worker. Both require worker preparation, and rescheduling depends on the K3s controller. [Piraeus replicated volumes](https://piraeus.io/docs/stable/tutorial/replicated-volumes/) · [Hadron layers](https://github.com/kairos-io/hadron-layers) · [OpenEBS prerequisites](https://openebs.io/docs/main/quickstart-guide/prerequisites)

## Calibre library and downloader handoff

OVH Object Storage was selected for books. The Calibre library measured about 1.8 GiB. One copy at the listed Beauharnois Standard 1-AZ rate would be about CA$0.02/month for storage. A recoverable snapshot would keep the books, covers and SQLite catalog together. The proposal needs a dedicated private books bucket and scoped app credentials. [OVH prices](https://www.ovhcloud.com/en-ca/public-cloud/prices/) · [Current Calibre backup](https://github.com/klever-lab/self-hosted/blob/main/docs/operations.md#backups)

The existing SABnzbd and qBittorrent jobs import books directly into Calibre-Web. The qBittorrent `books` category was empty at the audit. Manual uploads and the on-demand Gutenberg importer are also available. [Current book import paths](https://github.com/klever-lab/self-hosted/blob/main/docs/media-imports.md)

The proposed handoff was to use OVH as the common inbox for automated downloads.

1. Both downloaders would use the same uploader for completed books. Files would be keyed by SHA-256 and uploaded with a conditional create, so a byte-identical file occupies one object even if two jobs upload it concurrently. The hash identifies exact file content. Different editions or formats can have different hashes. Torrent originals would stay on disk for seeding. [OVH conditional writes](https://docs.ovhcloud.com/en/guides/storage-and-backup/object-storage/s3-conditional-writes)
2. For SABnzbd, the uploader would verify every book file from a successful release in OVH, record a durable receipt keyed by its altHUB release ID, and then remove that exact cart entry. The cart poller would check the receipt and SABnzbd's queue and history before submitting a download. Failed transfers would leave the cart entry available for retry. A receipt would let the poller retry a failed cart removal without downloading the release again. Unknown release IDs would need manual review. [Current altHUB tooling](https://github.com/klever-lab/self-hosted/blob/main/docs/usenet.md#operator-cli)
3. Calibre-Web would check the inbox every two minutes, verify file hashes, and import new books through its authenticated upload endpoint. The existing import records would supply the initial deduplication history. The inbox would replace the two direct import jobs at cutover. Separate full-library snapshots would capture Calibre's catalog and local edits for recovery onto another worker.
4. SABnzbd could switch to `emptyDir` volumes after testing that completed releases reach Calibre and unfinished cart releases can be picked up after a pod restart. Its uploader would share the pod's temporary download directory. A ConfigMap and Secrets would seed its configuration at startup. The cutover would keep the existing claims available for rollback until recovery was verified.
