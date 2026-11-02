// The Cairn Terraform (and OpenTofu) provider: pipes, streams, derived tables and table maintenance
// policies, managed through Cairn's API as the token's user.
package main

import (
	"context"
	"flag"
	"log"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"

	"github.com/syedsumx/sca/cairn/terraform-provider-cairn/internal/provider"
)

var version = "dev"

func main() {
	var debug bool
	flag.BoolVar(&debug, "debug", false, "run with support for debuggers")
	flag.Parse()
	err := providerserver.Serve(context.Background(), provider.New(version), providerserver.ServeOpts{
		Address: "registry.terraform.io/cairn/cairn",
		Debug:   debug,
	})
	if err != nil {
		log.Fatal(err)
	}
}
