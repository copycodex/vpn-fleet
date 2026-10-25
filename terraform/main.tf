resource "hcloud_ssh_key" "node" {
  name       = "vpnfleet-${var.node_name}"
  public_key = var.ssh_public_key
}

resource "hcloud_server" "node" {
  name        = var.node_name
  server_type = var.server_type
  image       = var.image
  location    = var.location
  ssh_keys    = [hcloud_ssh_key.node.id]

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  labels = {
    project = "vpn-fleet"
    role    = var.node_role
  }
}
