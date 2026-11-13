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

The default runs all eight guests. Use `--suite live`, `--suite controller`, `--suite worker-bios` or `--suite worker-uefi` to run only the three live boots, controller data guard, BIOS install/reboot pair or UEFI install/reboot pair. Every suite builds its own disposable base/tools/media. The live suite keeps its three-way SSH identity comparison in one process; all suites retain guest disk hashing. Logs include base, tools, media and guest durations, plus each guest's KVM/TCG and CPU selection. Legacy CPU coverage always uses TCG.

## CI

[Repository validation](../.github/workflows/validate.yml) runs on pushes and pull requests, including documentation changes. It runs repository checks and firewall packet checks.

[Private image builds](../.github/workflows/private-images.yml) runs for changes to the image/configuration, Kubernetes manifests, CLI, image harness, launcher, runtime requirements or its own workflow; it also supports manual dispatch. One `ubuntu-24.04` matrix runs the four image suites independently, with fail-fast disabled so every suite reports its result. A docs-only change does not trigger image builds. The workflow does not cache or publish private configuration, media or guest state.

Measure feedback time from the workflow's `created_at` through the latest job's `completed_at`, including queue time; do not sum parallel job durations. The successful [baseline run](https://github.com/klever-lab/guaranine/actions/runs/43333532181) took 33m32s including its 4s queue (33m28s job runtime). The [combined implementation run](https://github.com/klever-lab/guaranine/actions/runs/43349091458) passed all four suites and eight guests in 12m42s including queue time: 20m50s faster, a 62.1% reduction. Job runtimes were 12m29s live, 4m52s controller, 9m26s BIOS worker and 10m05s UEFI worker. All guests used TCG; the legacy guest retained `Opteron_G1`. This measurement includes the concurrent image simplifications and does not isolate the matrix's contribution. No artifacts were published.

## Dependency pins

[kairos/versions.json](../kairos/versions.json) owns Kairos, AuroraBoot and K3s versions/checksums. [kairos/Dockerfile](../kairos/Dockerfile) pins the base, upstream Tailscale layer, firmware images and network-tool packages, including the Broadcom firmware extraction input. Keep those facts in their source files rather than copying version tables into prose.

[.terraform-version](../.terraform-version) and [terraform-checksums.json](../scripts/terraform-checksums.json) must agree. [terraform_tool.py](../scripts/terraform_tool.py) uses an exact installed CLI or downloads a checksum-verified temporary copy. Each root owns its AzureRM requirement and provider lock file. [requirements.txt](../requirements.txt) and [requirements-dev.txt](../requirements-dev.txt) own runtime and validation dependencies.

Use the [documentation index](README.md#check-for-drift) to refresh docs from the current checkout and distinguish implemented checks from successful runs.
