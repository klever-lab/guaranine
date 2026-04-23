`named after the failed homelab with my boy froggy.`

Guaranine is actually a cloud platform. It is not the homelab itself. its simple and written in python. It's my AWS at home. It provides typical cloud features like virtual machines, containers, storage, and serverless functions. It also does configuration management for the hardware its running on.

config management:
- apply nixos config (https://github.com/klever-lab/nixos)
- maintain tailscale vpn connection

cloud features:
- Spin up server, with options for hostname
	- distributes workloads based on resources requirements
- Serverless Functions

## how orchestrate
Each machine will establish a connection to tailscale network. A python server will be listening on port 48584. This python server can recieve and execute commands. The commands will be related to features as mentioned above.

## how provision server
Upon being instructed to provision a server, a machine will create an LXC instance with the specified resources. It will be added to the tailscale network and the hostname saved. If this server is ever destroyed it will also be removed the the tailscale network.
