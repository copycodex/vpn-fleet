# Guide: a VPN fleet from scratch

The path from an empty machine to a working node, step by step. README is the
reference: it explains what lives where and why. This is the order of actions
and the expected result of each one.

The guide follows one example throughout: a node bought from a VPS provider and
handed over with a password for the first login. Substitute your own node name
wherever `<node>` appears.

## The wizard, or by hand

Every step below can be done by the wizard, and every one can be done by hand —
these are not two different orders, they are the same one. The wizard hides
nothing: it types the same files and calls the same commands, while the guide
explains what exactly it does and why.

One entry point for everything:

```bash
wizard/wizard.py
```

It looks at what is already done, names the stage of every server and offers the
next step — but never runs it without your "yes". It keeps no memory of its own:
the stage is derived from the files and the door state right now, so a run
interrupted anywhere continues from where it stopped.

The screen is built from three parts:

```
KEYS:
  установка  my-fleet
  age        ~/.config/sops/age/my-fleet.txt
  ssh        ~/.ssh/my-fleet_ed25519

MENU:
  1) Create server
  2) Servers
  3) Quit

YOUR CHOSE [1]:
```

`KEYS` is always printed: it is the first thing to learn when sitting down at a
machine you have forgotten. The `WARNINGS` block appears only when something is
missing, and every line in it carries the command that fixes it — when all is
well there is no block at all. Answers are numbers everywhere: `Back` and `Quit`
are list items like any other, and every menu has a step back.

`Create server` leads to the hoster first — the route and the door both depend
on it, so it is asked before anything is created. `Servers` lists the servers
with their stages; pick one and you see its next step and can run it from there.

The individual steps, if you go one at a time:

| Step | By command | By hand — section |
|---|---|---|
| 0. Dependencies | `wizard/deps.sh` | 0.1–0.2 |
| 1. Installation keys | `wizard/keys.sh` | 0.3–0.4 |
| 2. The private tree | `wizard/private_tree.py` | 0.5 |
| 3–9. Hoster and server | `make new-server` | 0.5, "What comes next" |
| 10. The run | `make first-contact`, `make bootstrap` | "What comes next" |

## What you need

| What | Why |
|---|---|
| A Linux control host | every command runs from here; nobody logs into the node by hand |
| A VPS with Ubuntu 22.04/24.04/26.04 | the node itself, created in the provider's panel |
| Its first-login credentials | user, port and password — whatever the provider sent |
| The provider's console | the spare door if the network goes away |

Terraform is not needed for most providers. It exists in this repository but
targets Hetzner, and there it is a route of its own — see "What comes next".

## Step 0. The control host from scratch

Starting state: a clean machine. No `sops`, no `age`, no fleet keys.

**0.1.** Clone the repository, work from its root.

**0.2.** Install the tooling: apt packages (git, python3, venv, ssh, age) →
`sops` from the pinned release binary → Python dependencies from
`requirements-ci.txt` into a venv → the ansible collections. Check: three
commands print their versions.

**0.3.** Generate the age key at `~/.config/sops/age/<installation>.txt`,
chmod 600, take the public half `age1…`. Back it up off the machine.

**0.4.** Generate the admin SSH key `~/.ssh/<installation>_ed25519`, without a
passphrase.

**0.5.** Create the private tree in `private/`:

- `.sops.yaml` — your own age recipient in both rules;
- `ssh_keys.yml` — your public SSH key;
- `known_hosts` — empty; the first contact puts the host key there;
- `first-entry.sops.yml` — an empty map, `first_entry: {}`;
- `nodes/<node>.yml` and `secrets.sops.yml` — these appear together with the
  first server (`make new-server`).

**0.6.** `scripts/check-private-inputs.sh <node>` — red on `known_hosts` alone,
which is normal until the first contact.

Result: the tooling is installed, both keys exist, the private tree is filled in
and the node is ready for `make first-contact`.

Points 0.1–0.2 are done by `wizard/deps.sh`, 0.3–0.4 by `wizard/keys.sh`, and
0.5 by `wizard/private_tree.py`. What follows explains each piece in detail —
both so you can do it by hand and so you know what the command is doing.

### 0.1. The repository

```bash
git clone <repository url> vpn-fleet
cd vpn-fleet
```

Every command below runs from the repository root, and that is not a matter of
taste: `ansible.cfg` declares the inventory by the relative path
`private/inventories`, `known_hosts` lives there too, and the `Makefile` builds
every other path from the root. From another directory the commands simply will
not find the inventory or the keys.

In a clone of the private repository, `private/` already contains tracked
installation files, including the SOPS configuration, encrypted maps, inventory,
and known hosts. Inspect them before step 0.5; use `wizard/private_tree.py` or
the samples in `inventories/` only for missing inputs. The public export does
not contain this tree. Private keys stay outside Git.

### 0.2. Tooling

By command: `wizard/deps.sh` — it prints a present/missing table, asks once and
installs only what is missing; `wizard/deps.sh --check` installs nothing at all.
The same thing by hand follows.

System packages (Debian/Ubuntu):

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip openssh-client age
```

Nothing here is spare: `openssh-client` gives `ssh`, `ssh-keygen` and
`ssh-keyscan` — the three commands with which the first contact accepts the host
key and opens the door; `age` gives `age-keygen` for the encryption key;
`python3-venv` is what lets you install ansible without breaking the system
Python.

`sops` is what encrypts and edits the secret files: it keeps values as
ciphertext inside the YAML, so the file stays readable and diffable in git.
Distributions usually ship an old one or none at all, so install the release
binary. The fleet's files are written by 3.9.x:

```bash
curl -fsSL -o /tmp/sops \
  https://github.com/getsops/sops/releases/download/v3.9.1/sops-v3.9.1.linux.amd64
sudo install -m 0755 /tmp/sops /usr/local/bin/sops
sops --version
```

The Python dependencies are pinned by `requirements-ci.txt`, and each has its
job: `ansible-core` is the engine behind every step after the first contact;
`pexpect` drives the PTY password dialogue in step 1; `PyYAML` reads the
first-entry map; `yamllint` and `ansible-lint` are the local gates, the same
ones CI runs. Install them into a venv: recent Ubuntu refuses installs into the
system Python.

```bash
python3 -m venv ~/.venv/vpn-fleet
~/.venv/vpn-fleet/bin/pip install -r requirements-ci.txt
source ~/.venv/vpn-fleet/bin/activate
ansible-galaxy collection install -r requirements.yml
```

`source` enables the venv in the current terminal session — a new session needs
it again. `ansible-galaxy` fetches the collections from `requirements.yml`:
`ansible.posix` and `community.general` give the roles their modules, while
`community.sops` teaches ansible to decrypt secrets while reading the inventory,
with no manual `sops -d`.

Checking this step — three commands must run without errors:

```bash
sops --version
ansible --version | head -1
python3 -c 'import pexpect, yaml; print("deps ok")'
```

### 0.3. The age identity — the root of all encryption

By command: `wizard/keys.sh` — it shows the keys you already have, offers to
take an existing one or create a new one, and asks for the installation name.
The same thing by hand follows.

An installation has exactly two keys, and they are named after it — in the
examples below, `vpn-fleet-01`. The installation name has nothing to do with a
server name: there can be any number of servers, they are named in 0.5 and live
in the inventory, while the age key encrypts the whole private tree and is
therefore single.

```bash
mkdir -p ~/.config/sops/age
age-keygen -o ~/.config/sops/age/vpn-fleet-01.txt
chmod 600 ~/.config/sops/age/vpn-fleet-01.txt
age-keygen -y ~/.config/sops/age/vpn-fleet-01.txt    # the public half, age1…
```

`age-keygen -o` writes the private half into a file, `age-keygen -y` prints the
public one from it — the `age1…` string the fleet's files are encrypted to. The
private half is what decrypts them, and it has no other copy.

The name is hard-wired nowhere. The `Makefile` finds the key itself: if
`~/.config/sops/age` holds exactly one `*.txt`, that one is taken and no
variable is needed. Once a second key appears, name the one you want:
`make <target> FLEET=vpn-fleet-01`. A completely different path is given as
`make <target> SOPS_AGE_KEY_FILE=<path>`.

The private half is never committed and never printed on screen. Back it up off
the machine: losing this key means losing every secret in the fleet. The public
half (`age1…`) is the encryption recipient and goes into `.sops.yaml`.

### 0.4. The admin SSH key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/vpn-fleet-01_ed25519 -C "vpn-fleet-01"
```

The name matches the age key: two keys per installation, not per server — this
one key opens every node. It works with both halves: the first contact puts the
public one (`.pub`) into the node's `authorized_keys`, and the private one is
what it, and every later Ansible run, connects with. That is why the path to the
private half is declared in the node file (`ansible_ssh_private_key_file`) while
the public one also goes into the admin list in 0.5.

The simplest choice is a key without a passphrase: the first contact runs
non-interactively and there is nobody to ask. If you need a passphrase, keep the
key in `ssh-agent` with its `.pub` next to the private file.

### 0.5. The private tree

By command: `wizard/private_tree.py` — it creates what is missing and fills in
what is empty, and never touches what is someone else's: a placeholder recipient
it offers to replace, a foreign recipient it leaves alone entirely. The same
thing by hand follows.

Four files plus the inventory directory. `private/` as a whole is outside the
public part of the repository, so a fresh clone does not have it — it appears
here.

```text
private/
├── .sops.yaml                                  encryption rules
└── inventories/
    ├── first-entry.sops.yml                    the first-entry map
    ├── nodes/<node>.yml                        the node's coordinates
    └── group_vars/all/
        ├── ssh_keys.yml                        admins' public keys
        └── secrets.sops.yml                    the fleet's secrets
```

`private/inventories/known_hosts` needs no filling in: the first contact puts
the host key there as `<host> ssh-ed25519 <key> vpn-fleet:<node>:door` (or
`[<host>]:<port>` for a custom port). Bootstrap records the proven steady port
with `vpn-fleet:<node>:steady`. For older unlabelled rows, use the wizard's
“Fill in missing data” action; it shows an assignment plan and asks before
writing. A shared address needs a human decision.

**Encryption rules — `private/.sops.yaml`.** Put your public age half from 0.3
into both rules:

```yaml
creation_rules:
  - path_regex: inventories/group_vars/all/secrets\.sops\.yml$
    age: age1…your public recipient…

  - path_regex: inventories/first-entry\.sops\.yml$
    age: age1…your public recipient…
    encrypted_regex: '^(password)$'
```

There are two rules because the files are encrypted differently: the secrets
whole, and the first-entry map only in its `password` field, so that `user`,
`port` and `method` can be read without the decryption key.

The public `.sops.yaml` in the repository root stays a sample with a
placeholder: a real recipient must never appear in the public part.

**The node inventory.** This is where the server name appears — the same name it
will carry everywhere afterwards: in the inventory, in the secret prefixes, in
the maps and in every command. Pick a telling one, for example `vpn-example-01`.
There can be no skeleton for a specific node — only you know its name and
address:

```bash
make new-server
```

The command asks for the name, the address and the door, then writes
`private/inventories/nodes/<node>.yml` with the path to the installation key. It
touches nothing that already exists: an existing server it merely fills in.

By hand it is the same — a copy of `inventories/nodes/example.yml.template` with
`ansible_host`, `node_name` and `ansible_ssh_private_key_file` adjusted. The
first login is not in this file and must not be: it lives in one map for the
whole fleet.

**Admin keys — `ssh_keys.yml`.** The list in the sample is empty. Write your
installation key into it:

```yaml
ssh_authorized_keys_devops:
  - name: control-host
    pubkey: "ssh-ed25519 AAAA… vpn-fleet-01"
```

The list must contain the installation key. Bootstrap installs exactly this list
for the `devops` user and then disables root and password login: an empty or
foreign list leaves the node without a single door.

**Secrets — `secrets.sops.yml`.** This file is created by `make new-server`
together with the first server: `sops` refuses to encrypt a document with no
data, so it cannot exist empty. The wizard creates the X25519 Reality pair and
short ID, including when all three fields are missing from an existing server.
It refuses a partial trio to avoid rotating an existing identity. For manual
creation, use `inventories/group_vars/all/secrets.sops.yml.example` with the
`node_example_` prefix replaced by your node's name and supply a valid trio:

```yaml
node_<node>_reality_private_key: "<Xray private key>"
node_<node>_reality_public_key: "<matching Xray public key>"
node_<node>_reality_short_id: "<16 lowercase hex digits>"
node_<node>_clients: []
node_<node>_console_password: ""
```

A filled-in file is encrypted once, and from then on it is edited only through
`make secrets-edit`:

```bash
python3 scripts/secrets_lock.py private/inventories/group_vars/all/secrets.sops.yml -- \
  sops --config private/.sops.yaml -e -i \
  private/inventories/group_vars/all/secrets.sops.yml
```

The line `node_<node>_console_password: ""` has to exist beforehand: the first
contact replaces it, it does not create it. An empty value is left unencrypted
by sops, which is how an unfilled field stays visible. Client UUIDs are added
when clients are created; the wizard supplies Reality identity before deploy.

**The first-entry map — `first-entry.sops.yml`.** One record per node; `user`,
`port` and `method` are whatever the provider sent:

```yaml
first_entry:
    <node>:
        user: ubuntu
        port: 22
        method: password
        password: ""
```

`method: key` is for nodes the provider hands over with a key already installed;
such a record has no `password` field at all. A filled-in map is encrypted with
the same command:

```bash
python3 scripts/secrets_lock.py private/inventories/first-entry.sops.yml -- \
  sops --config private/.sops.yaml -e -i private/inventories/first-entry.sops.yml
```

The password goes in last, straight into the ciphertext:

```bash
make secrets-edit FILE=private/inventories/first-entry.sops.yml
```

The editor shows the file decrypted — the only moment the password is on screen.
Once saved it lands in the file as ciphertext, while `user`, `port` and the
comments stay in the clear.

### 0.6. Checking this step

```bash
scripts/check-private-inputs.sh <node>
```

This is a gate, not a report: until the private inputs are filled in, no network
step begins. It checks that the files are in place, that `.sops.yaml` carries a
real recipient rather than a placeholder, that the admin key list is not empty,
that the secrets hold variables with your node's prefix, and that the node's
door is defined — either a record in the first-entry map or a readable
installation key.

The script names the field and the file, never the value. At this point it is
still red on one line: `known_hosts` is empty, because the node's host key is
accepted by the first contact. Everything else must be filled in;
`make doctor NODE=<node>` goes fully green after step 1.

## What comes next

A server is created by one command — it asks for everything else itself:

```bash
make new-server
```

These are steps 3–9: the hoster and its route, the name and address, the
first-entry door, the port the node moves to after bootstrap, the transports and
the clients. At the end the wizard shows the plan and waits for confirmation: no
file changes before your "yes".

**The hoster picks the route, and there are two routes.** With OVH and HostHatch
you create the node by hand in the provider's panel and the door arrives by
email. With Hetzner the node does not exist at all until terraform creates it:
terraform also puts our public key inside, so no password exists, the door is
always `key`, and the address appears only in terraform's output. The profile
catalogue is `inventories/hosters.yml`; a new hoster is added with
`wizard/hoster.py --add`.

Then, in order:

| Step | Command | What it does |
|---|---|---|
| 1. First contact | `make first-contact NODE=<node>` | accepts the host key by TOFU, changes the issued password, installs the fleet key, proves the login with it and clears the record from the map |
| 2. Bootstrap | `make bootstrap NODE=<node> FROM=<user>@<port>` | preflight, then `devops`, the SSH move to the chosen port, root and password login disabled |
| 3. The node | `make site NODE=<node>` | hardening, a firewall built from the declared transports, Xray and sing-box |
| 4. Clients | `make add-client NAME=<client>`, `make export` | UUID into the ciphertext, config, link and QR |

Bootstrap's `FROM` is the door the first contact left behind: its record in the
map is deleted on success, so the user and the port are named by the call. The
preflight checks four things before any network action: the first contact is
done, our key is in the admin list, the host key for the new port is known or
will be recorded, and that port is not already taken on the node itself.

Steps 1–4 are written up in the following sections of this guide.
