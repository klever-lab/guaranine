# Pod compromise and privilege escalation

This audit began on 2026-11-17. Its question is whether code execution in one
container can acquire authority over a host or other workloads. Losing that
application's own data is outside this audit's main concern.

The review uses repository definitions and pinned upstream source. It does not
attempt exploitation or use production workloads as test fixtures. A change in
Git does not protect an installed cluster until it has been applied.

## Findings

| Component | Finding at the start | Repository resolution |
| --- | --- | --- |
| qBittorrent VPN | A long-running root VPN sidecar has network-administration capabilities. Admission cannot contain code execution inside it. | Replaced by a short-lived initializer that exits before the application starts. Native incoming peers and seeding are preserved; a temporary setup capability remains. The [application guide](https://github.com/klever-lab/self-hosted/blob/main/docs/seeding.md#vpn-routing-and-pod-security) owns that exception. |
| OpenEBS helpers | Upstream requests fully privileged helpers with writable access to the storage parent directory. | Native admission removes privilege and confines each helper to its own claim directory. [Storage confinement](storage.md#permissions-and-network-access) owns the specification. |
| OpenEBS controller | Its API permissions can otherwise request arbitrary host-mounted Pods or PVs. | Independent helper and PV admission blocks those requests; unused PV-delete permission is removed. |
| Kairos node labelers | Broad host mounts, incomplete hardening and authority to change any Node object. | Generated readers are hardened, mounts narrowed, and real Pod-bound credentials limited to their own node's Kairos metadata. [Reader permissions](operations.md#operator-permissions-and-traffic) owns the details. |
| Kairos Operator | Deliberate host-root maintenance plus unnecessary cluster-wide workload writes. | Workload writes are scoped to its namespace and unused metrics roles removed. Host administration remains an explicit trust boundary. |

## Administrator trust boundary

The [Kairos maintenance guide](operations.md#operator-permissions-and-traffic)
owns the operator's remaining host authority. Software authorized to administer
all hosts cannot also be treated as an untrusted application confined to one pod.
An operator compromise therefore remains a cluster-administrator incident.

Ordinary application accounts must not receive those permissions. Scoped gateway
identity access and read-only monitoring permissions are not equivalent to
permission to create privileged workloads. Their owning guides are
[networking and security](security.md) and [monitoring](monitoring.md#permissions-and-traffic).

Containers also share the host kernel. Removing privileges reduces the attack
surface; protection against every kernel or runtime vulnerability would require
an additional isolation boundary. This work does not claim that guarantee.

## Verification and rollout

Local validation passed: 122 Guaranine unit tests, the full repository checks,
71 application tests including real namespace/WireGuard packet fixtures, and
ShellCheck. A disposable upstream Kubernetes API server verified actual admission
mutations, rejected filesystem/command overrides, PV restrictions, operator RBAC,
real Pod-bound token scope and denial when the mutation binding is missing.
The application's rendered admission templates and privilege-override rejections
also passed a disposable API server.

The packet fixture preserved incoming TCP and UDP source addresses, tunneled DNS
and WebUI access. It checked initialization retries, endpoint outage/recovery and
denied bypass traffic. It used generated keys and private network namespaces;
no production traffic or credentials entered these fixtures.

Activation requires the [platform guard installation](../SETUP.md#platform-admission-guards),
the component updates described in their owning guides, and the application's
ordinary deployment. Existing application data, bound volumes and enrolled
gateway identities must be preserved. Offline readers acquire runtime hardening
when their Pods can be recreated; API restrictions apply to subsequent requests.
Production exploitation, breakout attempts and live security probes are excluded.
