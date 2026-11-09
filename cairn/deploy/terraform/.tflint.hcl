# tflint for Cairn's Terraform modules and examples (CI: job terraform-kind). The terraform ruleset is built in.
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
