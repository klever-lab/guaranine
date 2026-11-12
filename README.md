# guaranine

One cloud K3s server and laptop workers, all running the same immutable Kairos/Hadron image. Private inter-host traffic uses Tailscale, including across separate sites and relayed connections.

Use upstream components and ordinary Kubernetes manifests. Keep the repository small: one CLI, one image recipe and two Terraform roots. No custom management API or enrollment service.

## Setup

1. Follow [SETUP.md](SETUP.md) to initialize private operator files and prepare the cloud controller.
2. Provide the laptop models so their built-in Wi-Fi driver/firmware coverage can be checked.
3. Run `./guaranine iso` on the operator computer while connected to the desired Wi-Fi. It captures that network, builds private media and offers USB flashing.
4. Boot a laptop, then run `./guaranine worker` to verify its identity, choose its name and explicitly confirm its installation disk.

The ISO contains network access but no cluster join token or automatic disk installation. Installed workers receive their configuration only after confirmation. Operational commands, security limits and recovery are in [SETUP.md](SETUP.md).

One server is an availability tradeoff. Disconnected workers can continue running; stateful failover requires fencing, spare capacity and suitable storage. Local disks do not follow workloads.

## Development

Use Python 3.12+ and run the same checks as CI:

```bash
env -u NO_COLOR uv run --no-project --with-requirements requirements-dev.txt bash scripts/validate.sh
```

The private-image workflow builds and boots disposable fixtures without publishing images or using real credentials. Laptop hardware and live WAN operation require separate verification.

Todo issues in the existing [Guaranine Linear project](https://linear.app/kian-finishing-zombie-granola/project/guaranine-5e147e0efbea) use the shared Clusterfork Symphony controller.

## Original design question

```text
okay be honest, should i go with a purpose built controller or k3s?
my needs:
- many machines, only connected over tailscale
- automatic management of services and machines
- good support for visibility/monitering
- easily expose services online via domains, subdomains, etc.
- secret management for being able to use APIs, etc.
- docker, lxc, qemu, etc. all sorts of support
and potentially more i havent thought of yet


paste this exact message in the readme and the succint short answer beneath it.

give me the proper version here
```

Use Kairos/Hadron with K3s. Kairos manages the OS; Kubernetes manages applications. Connect machines through Tailscale and reuse upstream components.
