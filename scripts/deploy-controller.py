#!/usr/bin/env python3
"""Actions entrypoints for the public OS base and private OVH staging deployment."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import guaranine as cli
from terraform_tool import ensure_terraform


def publish_base(directory):
    source = cli.build_base(directory)
    target = f'ghcr.io/{os.environ["GITHUB_REPOSITORY"].lower()}/kairos:{os.environ["GITHUB_SHA"]}'
    cli.private_command(['sudo', '--', 'docker', 'tag', source, target], directory)
    with cli.progress('Publishing the public OS base to GHCR…'):
        cli.private_command(['sudo', '--', 'docker', 'push', target], directory, timeout=1800)
    references = json.loads(cli.private_command(
        ['sudo', '--', 'docker', 'image', 'inspect', '--format', '{{json .RepoDigests}}', target], directory))
    repository = target.rsplit(':', 1)[0]
    reference = next(ref for ref in references if ref.startswith(repository + '@sha256:'))
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write('image=' + reference + '\n')
    print('Published OS base: ' + reference)


def check_plan(plan, had_controller):
    """CI may create a controller once; disruption and deletion require local review."""
    for resource in plan.get('resource_changes', []):
        if resource.get('mode') != 'managed':
            continue
        actions = resource['change']['actions']
        if any(action not in ('no-op', 'read', 'create', 'update') for action in actions):
            raise ValueError('Refusing destructive plan for ' + resource['address'] + '; review it locally')
        if (resource['address'] == 'openstack_compute_instance_v2.controller'
                and had_controller and actions != ['no-op']):
            raise ValueError('Refusing a change to the existing controller; review it locally')


def stage_controller(directory):
    if os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise ValueError('Deploy only from main')
    expected = os.environ['DEPLOY_COMMIT']
    if os.environ['GITHUB_SHA'] != expected:
        raise ValueError('main moved after dispatch; review the new commit before retrying')
    binary = ensure_terraform((cli.ROOT / '.terraform-version').read_text().strip(), directory, os.environ)
    os.environ['TF_DATA_DIR'] = str(directory / 'terraform-cache')
    os.environ['TF_VAR_state_bucket'] = os.environ['OVH_STATE_BUCKET']
    os.environ['TF_VAR_storage_region'] = os.environ['OVH_STORAGE_REGION']
    os.environ['TF_VAR_region'] = os.environ['OS_REGION_NAME']
    os.environ['AWS_ENDPOINT_URL_S3'] = f'https://s3.{os.environ["OVH_STORAGE_REGION"]}.io.cloud.ovh.net'
    command = [str(binary), '-chdir=' + str(cli.ROOT / 'infra/ovh')]
    def terraform(*arguments):
        return cli.private_command([*command, *arguments], directory, timeout=1800)
    terraform('init', '-input=false', '-lockfile=readonly',
              '-backend-config=bucket=' + os.environ['OVH_STATE_BUCKET'],
              '-backend-config=region=' + os.environ['OVH_STORAGE_REGION'])
    state = json.loads(terraform('state', 'pull'))
    resources = {resource['type'] + '.' + resource['name']: resource for resource in state.get('resources', [])}
    had_controller = 'openstack_compute_instance_v2.controller' in resources
    image = directory / 'controller/image/guaranine-kairos.qcow2'
    saved_image = resources.get('openstack_images_image_v2.controller')
    if saved_image:
        if not had_controller:
            raise ValueError('Seed image exists without a controller; inspect the partial deployment locally '
                             'before retrying. Its embedded enrollment key may have expired.')
        attributes = saved_image['instances'][0]['attributes']
        image.parent.mkdir(mode=0o700, parents=True)
        print('Reusing the existing private seed image; installed OS upgrades use Kairos.', flush=True)
        cli.private_command(['openstack', 'image', 'save', '--file', str(image), attributes['id']], directory, timeout=1800)
        image.chmod(0o600)
        if cli.digest(image) != attributes['properties']['guaranine_sha256']:
            raise ValueError('Downloaded image does not match its recorded SHA-256')
    else:
        if had_controller:
            raise ValueError('Controller exists without its managed image; inspect state before proceeding')
        expires = datetime.fromisoformat(os.environ['ENROLLMENT_EXPIRES'].replace('Z', '+00:00'))
        if not expires.tzinfo or expires <= datetime.now(timezone.utc):
            raise ValueError('Tailscale enrollment expired; dispatch a fresh build')
        config = cli.machine_config(os.environ['TAILSCALE_AUTH_KEY'], os.environ['SSH_PUBLIC_KEY'], role='controlplane')
        cli.build_image(config, 'qcow2', image.parent, os.environ['KAIROS_IMAGE'])
        if expires <= datetime.now(timezone.utc):
            raise ValueError('Tailscale enrollment expired during the build; no VM was created')
    os.environ['TF_VAR_image_path'] = str(image)
    os.environ['TF_VAR_image_sha256'] = cli.digest(image)
    plan = directory / 'ovh.tfplan'
    with cli.progress('Planning OVH infrastructure…'):
        terraform('plan', '-input=false', '-lock-timeout=60s', '-out=' + str(plan))
    check_plan(json.loads(terraform('show', '-json', str(plan))), had_controller)
    with cli.progress('Applying the non-destructive OVH plan…'):
        terraform('apply', '-input=false', '-lock-timeout=60s', str(plan))
    terraform('plan', '-input=false', '-lock-timeout=60s', '-detailed-exitcode')
    handoff = json.loads(terraform('output', '-json', 'handoff'))
    print(json.dumps(handoff, indent=2))
    print('OVH infrastructure is provisioned. Verify its console identity and Tailscale access. K3s remains stopped.')


if __name__ == '__main__':
    os.umask(0o077)
    try:
        directory = cli.output_directory(Path(os.environ['RUNNER_TEMP']) / ('guaranine-' + sys.argv[1]))
        {'publish': publish_base, 'stage': stage_controller}[sys.argv[1]](directory)
    except Exception as error:
        # Upstream exception bodies may contain credentials. Private logs stay on
        # this ephemeral runner and are never uploaded as artifacts.
        if isinstance(error, (cli.PrivateCommandError, ValueError)):
            print(str(error), file=sys.stderr)
        else:
            print(type(error).__name__ + ': inspect the private deployment step; diagnostics were not published', file=sys.stderr)
        sys.exit(1)
