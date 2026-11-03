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
provider "hcloud" {}
