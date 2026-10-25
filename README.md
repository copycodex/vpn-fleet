# vpn-fleet 2.0

An Ansible toolkit for running a small VPN fleet. A node serves VLESS with Reality through Xray and Hysteria2 through sing-box. Linux clients use sing-box in TUN mode; a Windows bundle and client export tools are also included.

Version 1.0 was the earlier Xray-only implementation, without sing-box or Hysteria2, and remains available at the `v1.0` tag.

## Repository map

| Path | Purpose |
|---|---|
| `roles/`, `playbooks/` | Provision, harden, and deploy nodes |
| `wizard/` | Guided setup and lifecycle commands |
| `inventories/` | Templates and examples for your inventory |
| `client/` | Linux and Windows client tooling |
| `tests/` | Synthetic tests and configuration render checks |
| `terraform/` | Optional Hetzner provisioning |

## Start your own fleet

Use a Linux control host and a VPS running Ubuntu. Install the tools listed in `requirements-ci.txt` and the collections in `requirements.yml`. The guided route starts with:

```bash
wizard/deps.sh
source ~/.venv/vpn-fleet/bin/activate
wizard/keys.sh
wizard/private_tree.py
wizard/wizard.py
```

Create your own `private/` tree from the templates. It holds your inventory, host keys, public administrator keys, SOPS configuration, and encrypted secrets. No operational `private/` tree is included here. Keep the age identity and SSH private key outside the repository.

For individual commands, the usual sequence is `make new-server`, `make first-contact NODE=<node>`, `make bootstrap NODE=<node> FROM=<user>@<port>`, `make site NODE=<node>`, and `make export NODE=<node>`. See the [English guide](GUIDE.md) or [Russian guide](GUIDE_RU.md) for the sequence and prerequisites. The wizard walks through the same steps and checks local readiness.

`make test` runs local tests and configuration checks. Deployment and client switching change live network state; perform those steps from a session where you can recover access.

## License

[MIT](LICENSE).
