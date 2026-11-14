terraform {
  required_version = ">= 1.6"
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.52"
    }
  }
}

# HCLOUD_TOKEN: a read-write API token of the Hetzner Cloud project (Cloud Console -> Security -> API tokens).
# The provider needs no settings of its own here, so this folder is also a module (deploy/terraform/modules/hetzner).
