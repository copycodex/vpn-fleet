terraform {
  required_version = "~> 1.15.0"
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "= 1.64.0"
    }
  }
}

# Токен читается из env HCLOUD_TOKEN — НЕ кладём в файлы.
provider "hcloud" {}
