package provider

import (
	"context"
	"errors"
	"fmt"
	"net/url"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// field is one attribute of a resource, as Cairn's API names it.
type field struct {
	name     string
	typ      string // string | bool | int | list
	required bool
	computed bool // Cairn decides it when not given (e.g. a debezium stream's mode)
	forceNew bool
	def      any // a static default; nil: null when not given
	desc     string
}

// kind is one resource type and its API.
type kind struct {
	typeName, path, desc string
	byScope              bool // maintenance policies: known by their scope, created and changed with PUT
	fields               []field
}

var kinds = []kind{
	{typeName: "pipe", path: "/v1/pipes", desc: "Files landing in a Cairn Store prefix, loaded into a lakehouse table once each.", fields: []field{
		{name: "name", typ: "string", required: true},
		{name: "bucket", typ: "string", required: true},
		{name: "prefix", typ: "string", def: ""},
		{name: "format", typ: "string", required: true, desc: "csv, json (lines) or parquet"},
		{name: "target", typ: "string", required: true, desc: "namespace.table"},
		{name: "create_table", typ: "bool", def: false},
		{name: "enabled", typ: "bool", def: true},
	}},
	{typeName: "stream", path: "/v1/streams", desc: "A Kafka topic (Debezium change events included) written into a lakehouse table, exactly once.", fields: []field{
		{name: "name", typ: "string", required: true},
		{name: "topic", typ: "string", required: true},
		{name: "format", typ: "string", def: "json", desc: "json or debezium"},
		{name: "mode", typ: "string", computed: true, desc: "append or upsert (debezium: always upsert)"},
		{name: "key", typ: "list", def: []string{}},
		{name: "target", typ: "string", required: true},
		{name: "create_table", typ: "bool", def: false},
		{name: "evolve", typ: "bool", def: false},
		{name: "start", typ: "string", def: "earliest"},
		{name: "source", typ: "string", desc: "the dataset URN the topic carries (lineage)"},
		{name: "batch_rows", typ: "int", def: int64(5000)},
		{name: "batch_seconds", typ: "int", def: int64(10)},
		{name: "enabled", typ: "bool", def: true},
	}},
	{typeName: "derived_table", path: "/v1/derived", desc: "A SELECT kept as a lakehouse table, refreshed on Trino as the token's user.", fields: []field{
		{name: "name", typ: "string", required: true},
		{name: "sql", typ: "string", required: true, desc: "one SELECT, without a trailing semicolon"},
		{name: "target", typ: "string", required: true, desc: "schema.table"},
		{name: "mode", typ: "string", def: "full", desc: "full, append or merge"},
		{name: "watermark", typ: "string"},
		{name: "key", typ: "list", def: []string{}},
		{name: "every_minutes", typ: "int"},
		{name: "on_change", typ: "bool", def: false},
		{name: "enabled", typ: "bool", def: true},
	}},
	{typeName: "maintenance_policy", path: "/maintenance/policies", byScope: true, desc: "Table maintenance (compaction, snapshot expiry, orphan cleanup) for *, a schema or schema.table.", fields: []field{
		{name: "scope", typ: "string", required: true, forceNew: true},
		{name: "optimize", typ: "bool", def: true},
		{name: "optimize_threshold", typ: "string", def: "128MB"},
		{name: "expire_days", typ: "int"},
		{name: "orphan_days", typ: "int"},
		{name: "every_hours", typ: "int", def: int64(24)},
		{name: "enabled", typ: "bool", def: true},
	}},
}

type kindResource struct {
	k kind
	c *client
}

var (
	_ resource.Resource                = (*kindResource)(nil)
	_ resource.ResourceWithConfigure   = (*kindResource)(nil)
	_ resource.ResourceWithImportState = (*kindResource)(nil)
)

func (r *kindResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_" + r.k.typeName
}

func (r *kindResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	attrs := map[string]schema.Attribute{
		"id": schema.StringAttribute{Computed: true, Description: "Cairn's id (for maintenance policies, the scope).",
			PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
	}
	for _, f := range r.k.fields {
		opt := !f.required
		comp := f.computed || f.def != nil
		switch f.typ {
		case "string":
			a := schema.StringAttribute{Required: f.required, Optional: opt, Computed: comp, Description: f.desc}
			if d, ok := f.def.(string); ok {
				a.Default = stringdefault.StaticString(d)
			}
			if f.forceNew {
				a.PlanModifiers = append(a.PlanModifiers, stringplanmodifier.RequiresReplace())
			}
			if f.computed {
				a.PlanModifiers = append(a.PlanModifiers, stringplanmodifier.UseStateForUnknown())
			}
			attrs[f.name] = a
		case "bool":
			a := schema.BoolAttribute{Required: f.required, Optional: opt, Computed: comp, Description: f.desc}
			if d, ok := f.def.(bool); ok {
				a.Default = booldefault.StaticBool(d)
			}
			attrs[f.name] = a
		case "int":
			a := schema.Int64Attribute{Required: f.required, Optional: opt, Computed: comp, Description: f.desc}
			if d, ok := f.def.(int64); ok {
				a.Default = int64default.StaticInt64(d)
			}
			attrs[f.name] = a
		case "list":
			a := schema.ListAttribute{ElementType: types.StringType, Required: f.required, Optional: opt, Computed: comp, Description: f.desc}
			if d, ok := f.def.([]string); ok {
				vals := make([]attr.Value, len(d))
				for i, s := range d {
					vals[i] = types.StringValue(s)
				}
				a.Default = listdefault.StaticValue(types.ListValueMust(types.StringType, vals))
			}
			attrs[f.name] = a
		}
	}
	resp.Schema = schema.Schema{Description: r.k.desc, Attributes: attrs}
}

func (r *kindResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData != nil {
		r.c = req.ProviderData.(*client)
	}
}

type getter func(context.Context, path.Path, any) diag.Diagnostics

// body is the API request for the planned values (unset values are left out: Cairn's defaults).
func (r *kindResource) body(ctx context.Context, get getter) (map[string]any, diag.Diagnostics) {
	var diags diag.Diagnostics
	out := map[string]any{}
	for _, f := range r.k.fields {
		p := path.Root(f.name)
		switch f.typ {
		case "string":
			var v types.String
			diags.Append(get(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.name] = v.ValueString()
			}
		case "bool":
			var v types.Bool
			diags.Append(get(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.name] = v.ValueBool()
			}
		case "int":
			var v types.Int64
			diags.Append(get(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.name] = v.ValueInt64()
			}
		case "list":
			var v types.List
			diags.Append(get(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				var ss []string
				diags.Append(v.ElementsAs(ctx, &ss, false)...)
				out[f.name] = ss
			}
		}
	}
	return out, diags
}

type setter func(context.Context, path.Path, any) diag.Diagnostics

// store writes what Cairn returned into the state.
func (r *kindResource) store(ctx context.Context, obj map[string]any, set setter) diag.Diagnostics {
	var diags diag.Diagnostics
	id := fmt.Sprint(obj["id"])
	if r.k.byScope {
		id = fmt.Sprint(obj["scope"])
	}
	diags.Append(set(ctx, path.Root("id"), types.StringValue(id))...)
	for _, f := range r.k.fields {
		v, present := obj[f.name]
		p := path.Root(f.name)
		switch f.typ {
		case "string":
			if s, ok := v.(string); ok && present {
				diags.Append(set(ctx, p, types.StringValue(s))...)
			} else {
				diags.Append(set(ctx, p, types.StringNull())...)
			}
		case "bool":
			b, _ := v.(bool)
			diags.Append(set(ctx, p, types.BoolValue(b))...)
		case "int":
			if n, ok := v.(float64); ok {
				diags.Append(set(ctx, p, types.Int64Value(int64(n)))...)
			} else {
				diags.Append(set(ctx, p, types.Int64Null())...)
			}
		case "list":
			vals := []attr.Value{}
			if xs, ok := v.([]any); ok {
				for _, x := range xs {
					vals = append(vals, types.StringValue(fmt.Sprint(x)))
				}
			}
			diags.Append(set(ctx, p, types.ListValueMust(types.StringType, vals))...)
		}
	}
	return diags
}

// find reads one resource by id (Cairn lists them); nil: it is gone.
func (r *kindResource) find(ctx context.Context, id string) (map[string]any, error) {
	res, err := r.c.call(ctx, "GET", r.k.path, nil)
	if err != nil {
		return nil, err
	}
	key := "id"
	if r.k.byScope {
		key = "scope"
	}
	for _, x := range res.([]any) {
		if m, ok := x.(map[string]any); ok && fmt.Sprint(m[key]) == id {
			return m, nil
		}
	}
	return nil, nil
}

func (r *kindResource) write(ctx context.Context, id string, body map[string]any) (map[string]any, error) {
	var res any
	var err error
	switch {
	case r.k.byScope:
		scope := fmt.Sprint(body["scope"])
		delete(body, "scope")
		res, err = r.c.call(ctx, "PUT", r.k.path+"/"+url.PathEscape(scope), body)
	case id == "":
		res, err = r.c.call(ctx, "POST", r.k.path, body)
	default:
		res, err = r.c.call(ctx, "PUT", r.k.path+"/"+url.PathEscape(id), body)
	}
	if err != nil {
		return nil, err
	}
	m, _ := res.(map[string]any)
	if m == nil {
		return nil, errors.New("cairn returned no resource")
	}
	if !r.k.byScope {
		// Read back what Cairn keeps (create answers vary in shape between kinds).
		return r.find(ctx, fmt.Sprint(m["id"]))
	}
	return m, nil
}

func (r *kindResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	body, diags := r.body(ctx, req.Plan.GetAttribute)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	obj, err := r.write(ctx, "", body)
	if err != nil {
		resp.Diagnostics.AddError("cairn: create "+r.k.typeName, err.Error())
		return
	}
	resp.Diagnostics.Append(r.store(ctx, obj, resp.State.SetAttribute)...)
}

func (r *kindResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var id types.String
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	if resp.Diagnostics.HasError() {
		return
	}
	obj, err := r.find(ctx, id.ValueString())
	if err != nil {
		resp.Diagnostics.AddError("cairn: read "+r.k.typeName, err.Error())
		return
	}
	if obj == nil {
		resp.State.RemoveResource(ctx) // deleted outside Terraform: it will be created again
		return
	}
	resp.Diagnostics.Append(r.store(ctx, obj, resp.State.SetAttribute)...)
}

func (r *kindResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var id types.String
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	body, diags := r.body(ctx, req.Plan.GetAttribute)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	obj, err := r.write(ctx, id.ValueString(), body)
	if err != nil {
		resp.Diagnostics.AddError("cairn: update "+r.k.typeName, err.Error())
		return
	}
	resp.Diagnostics.Append(r.store(ctx, obj, resp.State.SetAttribute)...)
}

func (r *kindResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var id types.String
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	if resp.Diagnostics.HasError() {
		return
	}
	_, err := r.c.call(ctx, "DELETE", r.k.path+"/"+url.PathEscape(id.ValueString()), nil)
	var ae *apiError
	if err != nil && !(errors.As(err, &ae) && ae.status == 404) {
		resp.Diagnostics.AddError("cairn: delete "+r.k.typeName, err.Error())
	}
}

func (r *kindResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
