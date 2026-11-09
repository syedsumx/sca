package provider

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// kindData reads one existing resource of a kind by its name (or its lookup attribute, and its parents), for
// configurations that refer to what is managed elsewhere: by hand, by a bundle, or by another workspace.
type kindData struct {
	k kind
	c *client
}

var _ datasource.DataSourceWithConfigure = (*kindData)(nil)

// lookupField is the lookup attribute's field ("": a setting, read by its parents alone).
func (d *kindData) lookupField() *field {
	if d.k.lookup == "" {
		return nil
	}
	return d.k.fieldByAPI(d.k.lookup)
}

func (d *kindData) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_" + d.k.typeName
}

func (d *kindData) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	attrs := map[string]schema.Attribute{"id": schema.StringAttribute{Computed: true}}
	look := d.lookupField()
	by := []string{}
	for _, f := range d.k.fields {
		if f.writeOnly {
			continue
		}
		in := (look != nil && f.name == look.name) || contains(d.k.parents, f.key())
		if in {
			by = append(by, f.name)
		}
		switch f.typ {
		case "string", "json":
			attrs[f.name] = schema.StringAttribute{Required: in, Computed: !in, Description: f.desc, Sensitive: f.sensitive}
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
	desc := d.k.desc
	if len(by) > 0 {
		desc += fmt.Sprintf(" Read by its %v.", by)
	}
	resp.Schema = schema.Schema{Description: desc, Attributes: attrs}
}

func (d *kindData) Configure(_ context.Context, req datasource.ConfigureRequest, _ *datasource.ConfigureResponse) {
	if req.ProviderData != nil {
		d.c = req.ProviderData.(*client)
	}
}

func (d *kindData) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	parents := map[string]any{}
	for _, p := range d.k.parents {
		f := d.k.fieldByAPI(p)
		var v types.String
		resp.Diagnostics.Append(req.Config.GetAttribute(ctx, path.Root(f.name), &v)...)
		parents[p] = v.ValueString()
	}
	look := d.lookupField()
	var want types.String
	if look != nil {
		resp.Diagnostics.Append(req.Config.GetAttribute(ctx, path.Root(look.name), &want)...)
	}
	if resp.Diagnostics.HasError() {
		return
	}
	rs, err := d.k.records(ctx, d.c, parents)
	if err != nil {
		resp.Diagnostics.AddError("cairn: read "+d.k.typeName, err.Error())
		return
	}
	var found []map[string]any
	for _, m := range rs {
		if look == nil || fmt.Sprint(m[look.readKey()]) == want.ValueString() {
			found = append(found, m)
		}
	}
	if len(found) > 1 && d.k.mine {
		// Saved queries and dashboards others share are listed too: the caller's own one wins.
		me, err := d.c.call(ctx, "GET", "/me", nil)
		if err == nil {
			urn := fmt.Sprint(me.(map[string]any)["urn"])
			var mine []map[string]any
			for _, m := range found {
				if fmt.Sprint(m[d.k.ownerField]) == urn {
					mine = append(mine, m)
				}
			}
			found = mine
		}
	}
	name := d.k.typeName
	switch len(found) {
	case 0:
		resp.Diagnostics.AddError("cairn: no "+name+" "+want.ValueString(), "nothing of that "+d.k.lookup+" that you may see")
		return
	case 1:
	default:
		resp.Diagnostics.AddError("cairn: several "+name+"s named "+want.ValueString(), "rename one, or use its id")
		return
	}
	r := &kindResource{k: d.k}
	resp.Diagnostics.Append(r.store(ctx, found[0], resp.State.SetAttribute, nil)...)
}
