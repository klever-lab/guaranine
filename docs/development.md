# Development and validation

## Repository checks

Use Python 3.12+ and run the same checks as CI from the repository root:

```bash
env -u NO_COLOR uv run --no-project --with-requirements requirements-dev.txt bash scripts/validate.sh
```

[scripts/validate.sh](../scripts/validate.sh) runs these checks:

| Check | Source and scope |
| --- | --- |
| Markdown | Recursive PyMarkdown scan using [.pymarkdown.json](../.pymarkdown.json), respecting gitignore. |
| Unit tests | [scripts/tests/](../scripts/tests/) covers private-file handling, role credentials, Wi-Fi capture, SSH trust, disk confirmations, USB flashing and Terraform tool selection. |
| Kairos validation | [validate-kairos.py](../scripts/validate-kairos.py) checks public and generated native profiles with the checksum-pinned upstream agent, JSON policy syntax, workflows with actionlint, shell with ShellCheck and Python compilation. |
| Terraform validation | [validate-terraform.py](../scripts/validate-terraform.py) checks formatting, initialization, provider schemas, mock tests, lifecycle plans, retained-disk deletion refusal and an explicit local state backend. |

The Terraform runner copies configuration into temporary storage, removes ambient Azure/Terraform inputs and uses mock providers. It verifies actual plan actions for unchanged deployments and VM replacement, and requires the specific `prevent_destroy` failure for data-disk replacement. No Azure provisioning is required, though tool/provider downloads need internet access.

## Firewall packet checks

Install `iproute2`, `nftables`, `iputils-ping` and the ordinary Linux namespace tools. The harness needs network namespaces and root or user-namespace support:

```bash
uv run --no-project --with-requirements requirements.txt python scripts/test-firewall.py
```

[test-firewall.py](../scripts/test-firewall.py) loads the actual native nftables rules into disposable namespaces. It checks host/pod LAN isolation over IPv4/IPv6, attached public subnets, denied physical ingress, permitted internet/Tailscale paths, DNS identity restrictions, subnet changes and blocked unconfigured egress. It first proves fixture connectivity so a broken fixture cannot silently count as isolation. CI runs it with sudo.

## Disposable image checks

With privileged Docker access, `sudo`, `qemu-system-x86_64` and OVMF installed, run:

```bash
uv run --no-project --with-requirements requirements.txt python scripts/test-private-images.py
```

The [image harness](../scripts/test-private-images.py) builds ISO/VHD fixtures using disposable credentials and restricted QEMU networking. Test-only access-point tools are mounted into guests, outside the production image and installed filesystem. Image qualification coverage, the recorded result and remaining physical/live-network checks belong in [Laptop hardware](hardware.md#qualification-and-limits).

## CI

[Repository validation](../.github/workflows/validate.yml) runs on pushes and pull requests, including documentation changes. It runs repository checks and firewall packet checks.

[Private image builds](../.github/workflows/private-images.yml) runs for changes to the image/configuration, Kubernetes manifests, CLI, image harness, launcher, runtime requirements or its own workflow; it also supports manual dispatch. A docs-only change does not trigger image builds. The workflow does not publish images or guest state.

## Dependency pins

[kairos/versions.json](../kairos/versions.json) owns Kairos, AuroraBoot, K3s and Tailscale versions/checksums. [kairos/Dockerfile](../kairos/Dockerfile) pins the base, firmware images and network-tool packages, including the Broadcom firmware extraction input. Keep those facts in their source files rather than copying version tables into prose.

[.terraform-version](../.terraform-version) and [terraform-checksums.json](../scripts/terraform-checksums.json) must agree. [terraform_tool.py](../scripts/terraform_tool.py) uses an exact installed CLI or downloads a checksum-verified temporary copy. Each root owns its AzureRM requirement and provider lock file. [requirements.txt](../requirements.txt) and [requirements-dev.txt](../requirements-dev.txt) own runtime and validation dependencies.

Use the [documentation index](README.md#check-for-drift) to refresh docs from the current checkout and distinguish implemented checks from successful runs.
