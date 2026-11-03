package provider

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// kindData reads one existing resource of a kind by its name (maintenance policies: their scope), for
// configurations that refer to what is managed elsewhere: by hand, by a bundle, or by another workspace.
type kindData struct {
	k kind
	c *client
}

var _ datasource.DataSourceWithConfigure = (*kindData)(nil)

func (d *kindData) lookup() string {
	if d.k.byScope {
		return "scope"
	}
	return "name"
}

func (d *kindData) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_" + d.k.typeName
}

func (d *kindData) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	attrs := map[string]schema.Attribute{"id": schema.StringAttribute{Computed: true}}
	for _, f := range d.k.fields {
		lookup := f.name == d.lookup()
		switch f.typ {
		case "string", "json":
			attrs[f.name] = schema.StringAttribute{Required: lookup, Computed: !lookup, Description: f.desc}
		case "bool":
			attrs[f.name] = schema.BoolAttribute{Computed: true, Description: f.desc}
		case "int":
			attrs[f.name] = schema.Int64Attribute{Computed: true, Description: f.desc}
		case "float":
			attrs[f.name] = schema.Float64Attribute{Computed: true, Description: f.desc}
		case "list":
			attrs[f.name] = schema.ListAttribute{ElementType: types.StringType, Computed: true, Description: f.desc}
		}
	}
	resp.Schema = schema.Schema{Description: d.k.desc + " Read by its " + d.lookup() + ".", Attributes: attrs}
}

func (d *kindData) Configure(_ context.Context, req datasource.ConfigureRequest, _ *datasource.ConfigureResponse) {
	if req.ProviderData != nil {
		d.c = req.ProviderData.(*client)
	}
}

func (d *kindData) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var want types.String
	resp.Diagnostics.Append(req.Config.GetAttribute(ctx, path.Root(d.lookup()), &want)...)
	if resp.Diagnostics.HasError() {
		return
	}
	res, err := d.c.call(ctx, "GET", d.k.path, nil)
	if err != nil {
		resp.Diagnostics.AddError("cairn: read "+d.k.typeName, err.Error())
		return
	}
	var found []map[string]any
	for _, x := range res.([]any) {
		if m, ok := x.(map[string]any); ok && fmt.Sprint(m[d.lookup()]) == want.ValueString() {
			found = append(found, m)
		}
	}
	if len(found) > 1 {
		// Saved queries and dashboards others share are listed too: the caller's own one wins.
		me, err := d.c.call(ctx, "GET", "/me", nil)
		if err == nil {
			urn := fmt.Sprint(me.(map[string]any)["urn"])
			var mine []map[string]any
			for _, m := range found {
				if fmt.Sprint(m["owner"]) == urn {
					mine = append(mine, m)
				}
			}
			found = mine
		}
	}
	switch len(found) {
	case 0:
		resp.Diagnostics.AddError("cairn: no "+d.k.typeName+" "+want.ValueString(), "nothing of that "+d.lookup()+" that you may see")
		return
	case 1:
	default:
		resp.Diagnostics.AddError("cairn: several "+d.k.typeName+"s named "+want.ValueString(), "rename one, or use its id")
		return
	}
	r := &kindResource{k: d.k}
	resp.Diagnostics.Append(r.store(ctx, found[0], resp.State.SetAttribute, nil)...)
}
