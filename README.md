# guaranine

One cloud K3s server and laptop workers, all running the same immutable Kairos/Hadron image. Private inter-host traffic uses Tailscale, including across separate sites and relayed connections.

Start with [SETUP.md](SETUP.md) for deployment. The [documentation index](docs/README.md) covers architecture, laptop hardware, security, operations and [development checks](docs/development.md).

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
