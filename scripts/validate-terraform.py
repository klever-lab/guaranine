#!/usr/bin/env python3
"""Exercise the real Terraform CLI and provider schema, without Azure access."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import yaml

from terraform_tool import ensure_terraform


REPOSITORY = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def terraform(root, env, *arguments, check=True):
    result = subprocess.run(
        [env["GUARANINE_TERRAFORM_BINARY"], f"-chdir={root}", *arguments],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
        check=False,
    )
    if check and result.returncode:
        raise SystemExit(result.stdout)
    return result


def copy_configuration(source, destination):
    """Exclude operator tfvars, backend overrides, state, plans and init caches."""
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if ".terraform" in relative.parts or not path.is_file():
            continue
        if path.name == "backend_override.tf":
            continue
        if not (
            path.name.endswith((".tf", ".tftest.hcl", ".tfmock.hcl", ".pub"))
            or path.name in (".terraform.lock.hcl", "cloud-init.yaml", "local-backend.tf.example")
        ):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def test_events(root, env, *arguments, check=True):
    result = terraform(root, env, "test", "-json", *arguments, check=check)
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result, events


def check_lifecycle_plans(events, name):
    plans = {
        event["@testrun"]: event["test_plan"]
        for event in events
        if event["type"] == "test_plan"
    }
    unchanged = plans.get("unchanged_plan", {})
    changes = unchanged.get("resource_changes", [])
    require(changes, "Missing unchanged-deployment plan evidence")
    require(
        all(change["change"]["actions"] == ["no-op"] for change in changes)
        and all(change["actions"] == ["no-op"] for change in unchanged["output_changes"].values()),
        f"An unchanged {name} deployment must produce no resource or output changes",
    )
    if name == "state":
        print("  unchanged state infrastructure: no changes", flush=True)
        return
    replacement = {
        change["address"]: change["change"]["actions"]
        for change in plans.get("replace_vm_keep_data", {}).get("resource_changes", [])
    }
    require(
        replacement.get("azurerm_linux_virtual_machine.coordinator") == ["delete", "create"]
        and replacement.get("azurerm_managed_disk.coordinator_data") == ["no-op"],
        "A real VM replacement plan must preserve the existing data disk",
    )
    print("  unchanged deployment: no changes; VM replacement: data disk retained", flush=True)


def check_disk_guard(root, env):
    result, events = test_events(root, env, "-test-directory=guard-tests", check=False)
    errors = [
        event["diagnostic"]
        for event in events
        if event["type"] == "diagnostic" and event["diagnostic"]["severity"] == "error"
    ]
    completed = {
        event["test_run"]["run"]: event["test_run"].get("status")
        for event in events
        if event["type"] == "test_run" and event["test_run"]["progress"] == "complete"
    }
    require(
        result.returncode == 1
        and completed.get("seed_mock_state") == "pass"
        and completed.get("refuse_disk_replacement") == "error"
        and len(errors) == 1
        and errors[0]["summary"] == "Instance cannot be destroyed"
        and "azurerm_managed_disk.coordinator_data has lifecycle.prevent_destroy" in errors[0]["detail"],
        "Persistent disk deletion guard did not produce exactly the expected refusal:\n" + result.stdout,
    )
    print("  persistent disk replacement: refused by Terraform prevent_destroy", flush=True)


def check_cloud_init():
    configuration = yaml.safe_load(
        (REPOSITORY / "infra/azure/coordinator/cloud-init.yaml").read_text()
    )
    require(
        configuration["ssh_pwauth"] is False and configuration["disable_root"] is True,
        "cloud-init must disable password and root SSH access",
    )
    ssh = next(
        entry for entry in configuration["write_files"]
        if entry["path"] == "/etc/ssh/sshd_config.d/00-guaranine.conf"
    )
    require(
        {"PasswordAuthentication no", "KbdInteractiveAuthentication no", "PermitRootLogin no"}
        <= set(ssh["content"].splitlines()),
        "The OS bootstrap must enforce key-only non-root SSH",
    )
    print("cloud-init: YAML and SSH baseline valid", flush=True)


def main():
    expected_version = (REPOSITORY / ".terraform-version").read_text().strip()
    # Tests always use mocks; also remove ambient Azure credentials, TF inputs,
    # CLI flags, logs and backend/workspace configuration from the test process.
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("ARM_", "AZURE_", "TF_"))
    }
    env.update(TF_IN_AUTOMATION="1", CHECKPOINT_DISABLE="1")
    check_cloud_init()
    with tempfile.TemporaryDirectory(prefix="guaranine-terraform-") as directory:
        scratch = Path(directory)
        env["GUARANINE_TERRAFORM_BINARY"] = str(ensure_terraform(expected_version, scratch, env))
        actual_version = json.loads(terraform(REPOSITORY, env, "version", "-json").stdout)["terraform_version"]
        require(actual_version == expected_version, f"Terraform {expected_version} required; found {actual_version}")
        (scratch / "plugins").mkdir()
        (scratch / "terraform.rc").write_text("disable_checkpoint = true\n")
        env.update(
            TF_PLUGIN_CACHE_DIR=str(scratch / "plugins"),
            TF_CLI_CONFIG_FILE=str(scratch / "terraform.rc"),
        )
        for name in ("state", "coordinator"):
            print(f"Terraform {name}: format, init, validate and mock tests", flush=True)
            root = scratch / name
            copy_configuration(REPOSITORY / "infra/azure" / name, root)
            env["TF_DATA_DIR"] = str(scratch / f"{name}-data")
            terraform(root, env, "fmt", "-check", "-recursive", "-no-color")
            terraform(root, env, "init", "-backend=false", "-input=false", "-lockfile=readonly", "-no-color")
            terraform(root, env, "validate", "-no-color")
            _, events = test_events(root, env, "-verbose")
            for event in events:
                if event["type"] == "test_summary":
                    require(event["test_summary"]["status"] == "pass", "Terraform tests did not pass")
                    print("  " + event["@message"], flush=True)
            check_lifecycle_plans(events, name)
            if name == "coordinator":
                check_disk_guard(root, env)
            else:
                # Verify that the documented first-run override selects a local
                # backend, with all state/cache paths outside the source tree.
                shutil.copyfile(root / "local-backend.tf.example", root / "backend_override.tf")
                terraform(
                    root, env, "init", "-input=false", "-lockfile=readonly", "-no-color",
                    f"-backend-config=path={scratch / 'bootstrap.tfstate'}",
                )
                metadata = json.loads((Path(env["TF_DATA_DIR"]) / "terraform.tfstate").read_text())
                require(metadata["backend"]["type"] == "local", "Bootstrap override must select local state")
                print("  first-run local backend override: valid", flush=True)


if __name__ == "__main__":
    main()
