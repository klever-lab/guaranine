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
| Unit tests | [scripts/tests/](../scripts/tests/) covers private-file handling, role credentials, Wi-Fi capture, SSH trust, disk confirmations, USB flashing/readback, Pi recovery-image checks, rollout targeting/failures, deployment refusal and Terraform tool selection. |
| OS validation | [validate-kairos.py](../scripts/validate-kairos.py) checks public and generated native Kairos profiles with the checksum-pinned upstream agent, JSON policy syntax, kiosk scripts, workflows with actionlint, shell with ShellCheck and Python compilation. |
| Terraform validation | [validate-terraform.py](../scripts/validate-terraform.py) checks formatting, initialization, provider schemas, mock tests, lifecycle plans and VM replacement refusal. OVH's remote backend is excluded from offline checks. |

The Terraform runner copies configuration into temporary storage, removes ambient cloud/Terraform inputs and uses mock providers. It verifies unchanged plans, seed-image replacement without VM replacement, and the specific `prevent_destroy` failure for OVH VM replacement. OVH checks also cover private image hashing, the fixed d2-2 flavor and attachment to OVH's default security group. The packet harness checks actual ingress restrictions. No cloud provisioning is required, though tool/provider downloads need internet access.

## Firewall packet checks

Install `iproute2`, `nftables`, `iputils-ping` and the ordinary Linux namespace tools. The harness needs network namespaces and root or user-namespace support:

```bash
uv run --no-project --with-requirements requirements.txt python scripts/test-firewall.py
```

[test-firewall.py](../scripts/test-firewall.py) loads the actual Kairos and standalone kiosk nftables rules into disposable namespaces. It checks host/pod LAN isolation over IPv4/IPv6, attached public subnets, denied physical ingress, permitted internet/Tailscale paths, DNS identity restrictions, subnet changes and blocked unconfigured egress. Kiosk checks also exercise the real NetworkManager dispatcher, a late IPv6 prefix without a dispatcher event, and marked IPv4/IPv6 Tailscale transport. It proves fixture connectivity so a broken fixture cannot silently count as isolation. CI runs it with sudo.

## Disposable image checks

With privileged Docker access, `sudo`, `qemu-system-x86_64`, `qemu-img` and OVMF installed, run:

```bash
uv run --no-project --with-requirements requirements.txt python scripts/test-private-images.py
```

The [image harness](../scripts/test-private-images.py) builds ISO/QCOW2 fixtures using disposable credentials and restricted QEMU networking. Test-only access-point tools are mounted into guests, outside the production image and installed filesystem. Installed worker fixtures also run the production configuration activation script in a child PID namespace: they first set logind to suspend, then require the rollout to restore ignore and leave an unrelated boot-stage tripwire untouched. The controller boots with one CPU and 2,000 MB RAM, checks native persistence and verifies that no K3s tokens or activation marker exist and K3s stays stopped. It reports immucore mount failures to its serial log; an unexpected reboot still fails the check. This fixture does not measure a running K3s server's capacity. Image qualification coverage, the recorded result and remaining physical/live-network checks belong in [Laptop hardware](hardware.md#qualification-and-limits).

The default runs all eight guests. Use `--suite live`, `--suite controller`, `--suite worker-bios` or `--suite worker-uefi` to run only the three live boots, controller activation guard, BIOS install/reboot pair or UEFI install/reboot pair. Every suite builds its own disposable base/tools/media. The live suite keeps its three-way SSH identity comparison in one process; all suites retain guest disk hashing. Logs include base, tools, media and guest durations, plus each guest's KVM/TCG and CPU selection. Legacy CPU coverage always uses TCG.

## CI

[Stage OVH controller](../.github/workflows/deploy.yml) runs only by manual dispatch from reviewed `main`. It has separate public-base and private-deployment jobs, serializes deployments and never uploads private artifacts. [deploy-controller.py](../scripts/deploy-controller.py) checks saved plan actions before applying, refuses any deletion or change to an existing controller, and requires a final unchanged plan. Unit tests exercise refusal before apply; live setup verifies remote state privacy, encryption, versioning and locking. [SETUP.md](../SETUP.md#controller-deployment) owns environment and backend configuration.

[Repository validation](../.github/workflows/validate.yml) runs on pushes and pull requests, including documentation changes. It runs repository checks and firewall packet checks.

[Private image builds](../.github/workflows/private-images.yml) runs for changes to the image/configuration, Kubernetes manifests, CLI, deployment helper, image harness, launcher, runtime requirements or image/deployment workflows; it also supports manual dispatch. One `ubuntu-24.04` matrix runs the four image suites independently, with fail-fast disabled so every suite reports its result. The job grants its disposable runner access to `/dev/kvm` when that device exists, allowing the harness to use hardware acceleration. Runners without KVM retain software emulation; the legacy CPU guest always uses TCG. A docs-only change does not trigger image builds. The workflow does not cache or publish private configuration, media or guest state.

Measure feedback time from the workflow's `created_at` through the latest job's `completed_at`, including queue time; do not sum parallel job durations. The successful [baseline run](https://github.com/klever-lab/guaranine/actions/runs/43333532181) took 33m32s including its 4s queue (33m28s job runtime). The [combined implementation run](https://github.com/klever-lab/guaranine/actions/runs/43349091458) passed all four suites and eight guests in 12m42s including queue time: 20m50s faster, a 62.1% reduction. Job runtimes were 12m29s live, 4m52s controller, 9m26s BIOS worker and 10m05s UEFI worker. All guests used TCG; the legacy guest retained `Opteron_G1`. This measurement includes the concurrent image simplifications and does not isolate the matrix's contribution. No artifacts were published.

The [boot-selection hardening run](https://github.com/klever-lab/guaranine/actions/runs/43359077952) passed the same four suites and eight guests in 12m43s, including its 20s queue: one second longer than the previous run. Stronger firmware and installation-success checks added no jobs or guest boots. Job runtimes were 12m23s live, 5m34s controller, 9m46s BIOS worker and 8m23s UEFI worker.

The [flash-logging run](https://github.com/klever-lab/guaranine/actions/runs/43520383289) took 11m25s including its 3s queue, with all eight guests using TCG. The slowest suite, UEFI worker, spent 601s of its 660s harness runtime in guest installation and boot. Base build, test tools and ISO creation took 59s. This is the baseline before granting runner KVM access.

With KVM access enabled and [controller startup ordering](architecture.md#images-and-node-roles) corrected, the [accelerated run](https://github.com/klever-lab/guaranine/actions/runs/43530386247) passed all four suites and eight guests on its first attempt in 7m38s including its 3s queue: 3m47s less elapsed time, a 33.1% reduction. Job runtimes were 7m35s live, 4m32s controller, 3m30s BIOS worker and 3m41s UEFI worker. Seven guests used KVM; the legacy CPU guest retained TCG and `Opteron_G1`. Earlier accelerated trials exposed the controller's first-boot label race and are not successful benchmark runs. This compares single runs on hosted runners, whose performance varies; coverage is unchanged. Repository validation passed separately in 49s.

## Dependency pins

[kairos/versions.json](../kairos/versions.json) owns Kairos, AuroraBoot and K3s versions/checksums. [kairos/Dockerfile](../kairos/Dockerfile) pins the base, upstream Tailscale layer, firmware images and network-tool packages, including the Broadcom firmware extraction input. Keep those facts in their source files rather than copying version tables into prose.

The Alpine build stage uses [Docker's official image on ECR Public](https://gallery.ecr.aws/docker/library/alpine), pinned by content digest, to avoid Docker Hub's unauthenticated pull limit on shared CI runners. The disposable Wi-Fi tools reuse the cached production `wifi` build stage and run it with `--pull=never`, avoiding another registry pull after the OS build.

[.terraform-version](../.terraform-version) and [terraform-checksums.json](../scripts/terraform-checksums.json) must agree. [terraform_tool.py](../scripts/terraform_tool.py) uses an exact installed CLI or downloads a checksum-verified temporary copy. Each root owns its provider requirement and lock file. [requirements.txt](../requirements.txt) and [requirements-dev.txt](../requirements-dev.txt) own runtime and validation dependencies.

Use the [documentation index](README.md#check-for-drift) to refresh docs from the current checkout and distinguish implemented checks from successful runs.
