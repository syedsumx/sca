package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"reflect"
	"regexp"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/boolplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/float64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// The resource types are generated from Cairn's resource registry (zz_generated_kinds.go); this file is the
// one engine that reads and writes them all, the same way cairn bundle and cairn export do
// (src/cairn/iac/engine.py).

// field is one attribute of a resource.
type field struct {
	name      string // Terraform's name
	api       string // Cairn's name for it, when Terraform reserves the name (e.g. connection)
	read      string // its name in Cairn's records, when it differs from the request's
	typ       string // string | bool | int | float | list | json (a JSON document, e.g. jsonencode([...]))
	required  bool
	computed  bool // Cairn decides it when not given
	forceNew  bool
	sensitive bool
	writeOnly bool // sent to Cairn, never stored in Terraform's state (secrets); <name>_version sets it again
	onlySet   bool // a JSON document Cairn always returns: kept in state only when the configuration sets it
	once      bool // only the create call returns it (a key): kept in state as created
	def       any  // a static default; nil: null when not given
	desc      string
}

// key is the field's name in Cairn's API.
func (f field) key() string {
	if f.api != "" {
		return f.api
	}
	return f.name
}

// readKey is the field's name in Cairn's records (dotted: inside a document, e.g. spec.select).
func (f field) readKey() string {
	if f.read != "" {
		return f.read
	}
	return f.key()
}

// dig is a record's field by its dotted name.
func dig(m map[string]any, name string) (any, bool) {
	var cur any = m
	for _, part := range strings.Split(name, ".") {
		mm, ok := cur.(map[string]any)
		if !ok {
			return nil, false
		}
		if cur, ok = mm[part]; !ok {
			return nil, false
		}
	}
	return cur, true
}

// op is one API call: its method, its path template ({id}: Cairn's id, {field}: a field's value) and its body.
type op struct {
	method, path string
	omit, only   []string
	query        []string // fields sent as query parameters
	body         string   // fixed values added to the body, as JSON
	noBody       bool
}

// kind is one resource type and its API.
type kind struct {
	typeName, name, desc string
	read, listKey, idKey string // the list's path (and the key holding it; "." the record itself), Cairn's id field
	key, parents         []string
	lookup               string // the data source's lookup attribute
	mine                 bool
	ownerField           string
	match                string      // only records with these values (JSON) are this kind
	skip                 [][2]string // records whose field starts with this are Cairn's own
	create               *op
	update, del          *op
	fields               []field
}

var placeholder = regexp.MustCompile(`\{([a-z_]+)\}`)

func (k kind) singleton() bool { return k.listKey == "." }

func (k kind) fieldByAPI(name string) *field {
	for i := range k.fields {
		if k.fields[i].key() == name {
			return &k.fields[i]
		}
	}
	return nil
}

func esc(v any) string {
	return strings.ReplaceAll(url.PathEscape(fmt.Sprint(v)), "%2A", "*")
}

func expand(tmpl string, vals map[string]any, id string) (string, error) {
	var err error
	out := placeholder.ReplaceAllStringFunc(tmpl, func(m string) string {
		name := m[1 : len(m)-1]
		if name == "id" {
			if id == "" {
				err = fmt.Errorf("%s: the resource's id is not known yet", tmpl)
			}
			return esc(id)
		}
		v, ok := vals[name]
		if !ok || v == nil || fmt.Sprint(v) == "" {
			err = fmt.Errorf("%s: %s is not set", tmpl, name)
			return ""
		}
		return esc(v)
	})
	return out, err
}

// recordID is Cairn's id of a record.
func (k kind) recordID(m map[string]any) string {
	if k.singleton() {
		return ""
	}
	return fmt.Sprint(m[k.idKey])
}

// tfID is Terraform's id: the parents' values and Cairn's id, joined by /.
func (k kind) tfID(m map[string]any) string {
	parts := []string{}
	for _, p := range k.parents {
		parts = append(parts, fmt.Sprint(m[p]))
	}
	if !k.singleton() {
		parts = append(parts, k.recordID(m))
	}
	if len(parts) == 0 {
		return k.name
	}
	return strings.Join(parts, "/")
}

// splitID is the parents and Cairn's id in a Terraform id.
func (k kind) splitID(id string) (map[string]any, string, error) {
	parents := map[string]any{}
	n := len(k.parents)
	if !k.singleton() {
		n++
	}
	if n == 0 {
		return parents, "", nil
	}
	parts := strings.SplitN(id, "/", n)
	if len(parts) != n {
		want := append(append([]string{}, k.parents...), k.idKey)
		if k.singleton() {
			want = k.parents
		}
		return nil, "", fmt.Errorf("cairn_%s: an id is %s", k.typeName, strings.Join(want, "/"))
	}
	for i, p := range k.parents {
		parents[p] = parts[i]
	}
	if k.singleton() {
		return parents, "", nil
	}
	return parents, parts[n-1], nil
}

// records lists the kind's records under the parents, with the parents' values filled in.
func (k kind) records(ctx context.Context, c *client, parents map[string]any) ([]map[string]any, error) {
	p, err := expand(k.read, parents, "")
	if err != nil {
		return nil, err
	}
	res, err := c.call(ctx, "GET", p, nil)
	if err != nil {
		return nil, err
	}
	var items []any
	switch {
	case k.singleton():
		items = []any{res}
	case k.listKey != "":
		m, _ := res.(map[string]any)
		items, _ = m[k.listKey].([]any)
	default:
		items, _ = res.([]any)
	}
	var want map[string]any
	if k.match != "" {
		_ = json.Unmarshal([]byte(k.match), &want)
	}
	var out []map[string]any
	for _, x := range items {
		m, ok := x.(map[string]any)
		if !ok {
			continue
		}
		keep := true
		for a, b := range want {
			if !reflect.DeepEqual(m[a], b) {
				keep = false
			}
		}
		for _, s := range k.skip {
			if strings.HasPrefix(fmt.Sprint(m[s[0]]), s[1]) {
				keep = false
			}
		}
		if !keep {
			continue
		}
		for _, p := range k.parents {
			m[p] = parents[p]
		}
		out = append(out, m)
	}
	return out, nil
}

// find reads one resource by its parents and id; nil: it is gone.
func (k kind) find(ctx context.Context, c *client, parents map[string]any, id string) (map[string]any, error) {
	rs, err := k.records(ctx, c, parents)
	if err != nil {
		var ae *apiError
		if errors.As(err, &ae) && ae.status == 404 && len(k.parents) > 0 {
			return nil, nil // the parent is gone, and its children with it
		}
		return nil, err
	}
	for _, m := range rs {
		if k.singleton() || k.recordID(m) == id {
			return m, nil
		}
	}
	return nil, nil
}

func (k kind) body(o *op, vals map[string]any) map[string]any {
	if o.noBody {
		return nil
	}
	out := map[string]any{}
	for _, f := range k.fields {
		v, ok := vals[f.key()]
		if !ok || f.once || contains(o.omit, f.key()) || contains(o.query, f.key()) ||
			(len(o.only) > 0 && !contains(o.only, f.key())) {
			continue
		}
		out[f.key()] = v
	}
	if o.body != "" {
		var fixed map[string]any
		_ = json.Unmarshal([]byte(o.body), &fixed)
		for a, b := range fixed {
			out[a] = b
		}
	}
	return out
}

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

func (k kind) send(ctx context.Context, c *client, o *op, vals map[string]any, id string) (any, error) {
	p, err := expand(o.path, vals, id)
	if err != nil {
		return nil, err
	}
	q := url.Values{}
	for _, name := range o.query {
		if v, ok := vals[name]; ok && v != nil {
			q.Set(name, fmt.Sprint(v))
		}
	}
	if len(q) > 0 {
		p += "?" + q.Encode()
	}
	b := k.body(o, vals)
	if b == nil {
		return c.call(ctx, o.method, p, nil)
	}
	return c.call(ctx, o.method, p, b)
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
	idDesc := "Terraform's id: Cairn's " + r.k.idKey
	if len(r.k.parents) > 0 {
		idDesc = "Terraform's id: " + strings.Join(append(append([]string{}, r.k.parents...), r.k.idKey), "/")
	}
	attrs := map[string]schema.Attribute{
		"id": schema.StringAttribute{Computed: true, Description: idDesc + " (import with it).",
			PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()}},
	}
	for _, f := range r.k.fields {
		opt := !f.required
		comp := (f.computed || f.def != nil) && !f.writeOnly
		switch f.typ {
		case "string", "json":
			a := schema.StringAttribute{Required: f.required, Optional: opt, Computed: comp, Description: f.desc,
				Sensitive: f.sensitive, WriteOnly: f.writeOnly}
			if d, ok := f.def.(string); ok {
				a.Default = stringdefault.StaticString(d)
			}
			if f.forceNew && !f.writeOnly {
				a.PlanModifiers = append(a.PlanModifiers, stringplanmodifier.RequiresReplace())
			}
			if f.computed {
				a.PlanModifiers = append(a.PlanModifiers, stringplanmodifier.UseStateForUnknown())
				if f.typ == "json" {
					a.PlanModifiers = append(a.PlanModifiers, subsetOfState{})
				}
			}
			attrs[f.name] = a
		case "bool":
			a := schema.BoolAttribute{Required: f.required, Optional: opt, Computed: comp, Description: f.desc, Sensitive: f.sensitive}
			if d, ok := f.def.(bool); ok {
				a.Default = booldefault.StaticBool(d)
			}
			if f.forceNew {
				a.PlanModifiers = append(a.PlanModifiers, boolplanmodifier.RequiresReplace())
			}
			if f.computed {
				a.PlanModifiers = append(a.PlanModifiers, boolplanmodifier.UseStateForUnknown())
			}
			attrs[f.name] = a
		case "int":
			a := schema.Int64Attribute{Required: f.required, Optional: opt, Computed: comp, Description: f.desc, Sensitive: f.sensitive}
			if d, ok := f.def.(int64); ok {
				a.Default = int64default.StaticInt64(d)
			}
			if f.forceNew {
				a.PlanModifiers = append(a.PlanModifiers, int64planmodifier.RequiresReplace())
			}
			if f.computed {
				a.PlanModifiers = append(a.PlanModifiers, int64planmodifier.UseStateForUnknown())
			}
			attrs[f.name] = a
		case "float":
			a := schema.Float64Attribute{Required: f.required, Optional: opt, Computed: comp, Description: f.desc, Sensitive: f.sensitive}
			if f.forceNew {
				a.PlanModifiers = append(a.PlanModifiers, float64planmodifier.RequiresReplace())
			}
			if f.computed {
				a.PlanModifiers = append(a.PlanModifiers, float64planmodifier.UseStateForUnknown())
			}
			attrs[f.name] = a
		case "list":
			a := schema.ListAttribute{ElementType: types.StringType, Required: f.required, Optional: opt, Computed: comp,
				Description: f.desc, Sensitive: f.sensitive}
			if d, ok := f.def.([]string); ok {
				vals := make([]attr.Value, len(d))
				for i, s := range d {
					vals[i] = types.StringValue(s)
				}
				a.Default = listdefault.StaticValue(types.ListValueMust(types.StringType, vals))
			}
			if f.forceNew {
				a.PlanModifiers = append(a.PlanModifiers, listplanmodifier.RequiresReplace())
			}
			if f.computed {
				a.PlanModifiers = append(a.PlanModifiers, listplanmodifier.UseStateForUnknown())
			}
			attrs[f.name] = a
		}
		if f.writeOnly {
			attrs[f.name+"_version"] = schema.Int64Attribute{Optional: true,
				Description: "Raise it to send " + f.name + " to Cairn again (" + f.name + " is write-only: never in Terraform's state)."}
		}
	}
	desc := r.k.desc
	if r.k.del == nil {
		desc += " Destroying it only removes it from Terraform's state: the setting stays in Cairn."
	}
	resp.Schema = schema.Schema{Description: desc, Attributes: attrs}
}

func (r *kindResource) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	if req.ProviderData != nil {
		r.c = req.ProviderData.(*client)
	}
}

type getter func(context.Context, path.Path, any) diag.Diagnostics

// values is the API request for the planned values (unset values are left out: Cairn's defaults); write-only
// values come from the configuration, the only place Terraform has them.
func (r *kindResource) values(ctx context.Context, get, config getter) (map[string]any, diag.Diagnostics) {
	var diags diag.Diagnostics
	out := map[string]any{}
	for _, f := range r.k.fields {
		p := path.Root(f.name)
		g := get
		if f.writeOnly {
			if config == nil {
				continue
			}
			g = config
		}
		switch f.typ {
		case "string":
			var v types.String
			diags.Append(g(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.key()] = v.ValueString()
			}
		case "bool":
			var v types.Bool
			diags.Append(g(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.key()] = v.ValueBool()
			}
		case "int":
			var v types.Int64
			diags.Append(g(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.key()] = v.ValueInt64()
			}
		case "float":
			var v types.Float64
			diags.Append(g(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.key()] = v.ValueFloat64()
			}
		case "json":
			var v types.String
			diags.Append(g(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				var doc any
				if err := json.Unmarshal([]byte(v.ValueString()), &doc); err != nil {
					diags.AddAttributeError(p, "not JSON", err.Error())
				}
				out[f.key()] = doc
			}
		case "list":
			var v types.List
			diags.Append(g(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				var ss []string
				diags.Append(v.ElementsAs(ctx, &ss, false)...)
				if ss == nil {
					ss = []string{}
				}
				out[f.key()] = ss
			}
		}
	}
	return out, diags
}

type setter func(context.Context, path.Path, any) diag.Diagnostics

// store writes what Cairn returned into the state. “prior“ is the plan (or the state being refreshed):
// a JSON document Cairn holds everything of is kept as written, so Cairn's own additions are not drift.
func (r *kindResource) store(ctx context.Context, obj map[string]any, set setter, prior getter, imported bool) diag.Diagnostics {
	var diags diag.Diagnostics
	diags.Append(set(ctx, path.Root("id"), types.StringValue(r.k.tfID(obj)))...)
	for _, f := range r.k.fields {
		p := path.Root(f.name)
		if f.writeOnly {
			if prior != nil { // a data source has no write-only attributes
				var ver types.Int64
				prior(ctx, path.Root(f.name+"_version"), &ver)
				diags.Append(set(ctx, path.Root(f.name+"_version"), ver)...)
			}
			continue
		}
		v, present := dig(obj, f.readKey())
		if f.once && prior == nil {
			continue // a data source does not show it
		}
		if f.once && !present {
			var was types.String
			prior(ctx, p, &was)
			diags.Append(set(ctx, p, was)...)
			continue
		}
		switch f.typ {
		case "string":
			if s, ok := v.(string); ok && present {
				diags.Append(set(ctx, p, types.StringValue(s))...)
			} else if present && v != nil {
				diags.Append(set(ctx, p, types.StringValue(fmt.Sprint(v)))...)
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
		case "float":
			if n, ok := v.(float64); ok {
				diags.Append(set(ctx, p, types.Float64Value(n))...)
			} else {
				diags.Append(set(ctx, p, types.Float64Null())...)
			}
		case "json":
			if !present || v == nil {
				diags.Append(set(ctx, p, types.StringNull())...)
				continue
			}
			if prior != nil {
				var was types.String
				prior(ctx, p, &was)
				if f.onlySet && (was.IsNull() || was.IsUnknown()) && !imported {
					// not declared: Cairn's own value is not drift (an import takes what is there)
					diags.Append(set(ctx, p, types.StringNull())...)
					continue
				}
				var doc any
				if !was.IsNull() && !was.IsUnknown() && json.Unmarshal([]byte(was.ValueString()), &doc) == nil && subset(doc, v) {
					diags.Append(set(ctx, p, was)...)
					continue
				}
			}
			b, _ := json.Marshal(v)
			diags.Append(set(ctx, p, types.StringValue(string(b)))...)
		case "list":
			if v == nil && f.def == nil && !f.computed {
				diags.Append(set(ctx, p, types.ListNull(types.StringType))...)
				continue
			}
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

// subsetOfState plans the state's JSON when it holds everything the configuration declares: Cairn's own
// additions (defaults, empty lists) are not a change, also after an import, when there is no earlier plan.
type subsetOfState struct{}

func (subsetOfState) Description(context.Context) string {
	return "Cairn's additions to the declared JSON are not a change"
}

func (m subsetOfState) MarkdownDescription(ctx context.Context) string { return m.Description(ctx) }

func (subsetOfState) PlanModifyString(_ context.Context, req planmodifier.StringRequest, resp *planmodifier.StringResponse) {
	if req.StateValue.IsNull() || req.StateValue.IsUnknown() || req.ConfigValue.IsNull() || req.ConfigValue.IsUnknown() {
		return
	}
	var want, have any
	if json.Unmarshal([]byte(req.ConfigValue.ValueString()), &want) != nil ||
		json.Unmarshal([]byte(req.StateValue.ValueString()), &have) != nil {
		return
	}
	if subset(want, have) {
		resp.PlanValue = req.StateValue
	}
}

// subset: does Cairn's “have“ hold everything “want“ declares (ids and defaults Cairn adds aside)?
func subset(want, have any) bool {
	switch w := want.(type) {
	case map[string]any:
		h, ok := have.(map[string]any)
		if !ok {
			return false
		}
		for k, v := range w {
			if !subset(v, h[k]) {
				return false
			}
		}
		return true
	case []any:
		h, ok := have.([]any)
		if !ok || len(h) != len(w) {
			return false
		}
		for i := range w {
			if !subset(w[i], h[i]) {
				return false
			}
		}
		return true
	default:
		return reflect.DeepEqual(want, have)
	}
}

// created finds what a create made: by the id Cairn answered with or the one given, else by its key.
func (r *kindResource) created(ctx context.Context, res any, vals map[string]any) (map[string]any, error) {
	parents := map[string]any{}
	for _, p := range r.k.parents {
		parents[p] = vals[p]
	}
	id := ""
	if v, ok := vals[r.k.idKey]; ok {
		id = fmt.Sprint(v)
	}
	if m, ok := res.(map[string]any); ok && m[r.k.idKey] != nil && fmt.Sprint(m[r.k.idKey]) != "" {
		id = fmt.Sprint(m[r.k.idKey])
	}
	once := func(obj map[string]any) map[string]any {
		if m, ok := res.(map[string]any); ok && obj != nil {
			for _, f := range r.k.fields {
				if v, given := m[f.key()]; f.once && given {
					obj[f.key()] = v // only the create answer has it
				}
			}
		}
		return obj
	}
	if r.k.singleton() || id != "" {
		obj, err := r.k.find(ctx, r.c, parents, id)
		if err != nil || obj != nil {
			return once(obj), err
		}
	}
	rs, err := r.k.records(ctx, r.c, parents)
	if err != nil {
		return nil, err
	}
	for _, m := range rs {
		same := true
		for _, key := range r.k.key {
			f := r.k.fieldByAPI(key)
			got, _ := dig(m, f.readKey())
			if fmt.Sprint(got) != fmt.Sprint(vals[key]) {
				same = false
			}
		}
		if same {
			return once(m), nil
		}
	}
	return nil, errors.New("created, but Cairn does not list it")
}

func (r *kindResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	vals, diags := r.values(ctx, req.Plan.GetAttribute, req.Config.GetAttribute)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	id := ""
	if v, ok := vals[r.k.idKey]; ok {
		id = fmt.Sprint(v)
	}
	res, err := r.k.send(ctx, r.c, r.k.create, vals, id)
	if err == nil {
		var obj map[string]any
		obj, err = r.created(ctx, res, vals)
		if err == nil {
			resp.Diagnostics.Append(r.store(ctx, obj, resp.State.SetAttribute, req.Plan.GetAttribute, false)...)
			return
		}
	}
	resp.Diagnostics.AddError("cairn: create "+r.k.typeName, err.Error())
}

func (r *kindResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var id types.String
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	if resp.Diagnostics.HasError() {
		return
	}
	parents, rid, err := r.k.splitID(id.ValueString())
	if err == nil {
		var obj map[string]any
		obj, err = r.k.find(ctx, r.c, parents, rid)
		if err == nil {
			if obj == nil {
				resp.State.RemoveResource(ctx) // deleted outside Terraform: it will be created again
				return
			}
			resp.Diagnostics.Append(r.store(ctx, obj, resp.State.SetAttribute, req.State.GetAttribute, r.imported(ctx, req.State.GetAttribute))...)
			return
		}
	}
	resp.Diagnostics.AddError("cairn: read "+r.k.typeName, err.Error())
}

func (r *kindResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var id types.String
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	vals, diags := r.values(ctx, req.Plan.GetAttribute, req.Config.GetAttribute)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	if r.k.update == nil {
		resp.Diagnostics.AddError("cairn: update "+r.k.typeName, "created once: a change replaces it")
		return
	}
	parents, rid, err := r.k.splitID(id.ValueString())
	if err == nil {
		for p, v := range parents {
			vals[p] = v
		}
		if _, err = r.k.send(ctx, r.c, r.k.update, vals, rid); err == nil {
			var obj map[string]any
			if obj, err = r.k.find(ctx, r.c, parents, rid); err == nil {
				if obj == nil {
					err = errors.New("changed, but Cairn no longer lists it")
				} else {
					resp.Diagnostics.Append(r.store(ctx, obj, resp.State.SetAttribute, req.Plan.GetAttribute, false)...)
					return
				}
			}
		}
	}
	resp.Diagnostics.AddError("cairn: update "+r.k.typeName, err.Error())
}

func (r *kindResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	if r.k.del == nil {
		return // a setting that always exists: destroying it forgets it
	}
	var id types.String
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	vals, diags := r.values(ctx, req.State.GetAttribute, nil)
	resp.Diagnostics.Append(diags...)
	if resp.Diagnostics.HasError() {
		return
	}
	parents, rid, err := r.k.splitID(id.ValueString())
	if err == nil {
		for p, v := range parents {
			vals[p] = v
		}
		_, err = r.k.send(ctx, r.c, r.k.del, vals, rid)
	}
	var ae *apiError
	if err != nil && !(errors.As(err, &ae) && ae.status == 404) {
		resp.Diagnostics.AddError("cairn: delete "+r.k.typeName, err.Error())
	}
}

// imported: the state holds only an id (just imported), so what Cairn holds is taken as declared. A required
// attribute is never null in a state Terraform wrote.
func (r *kindResource) imported(ctx context.Context, state getter) bool {
	for _, f := range r.k.fields {
		if !f.required || f.writeOnly {
			continue
		}
		var v attr.Value
		switch f.typ {
		case "string", "json":
			var x types.String
			state(ctx, path.Root(f.name), &x)
			v = x
		case "bool":
			var x types.Bool
			state(ctx, path.Root(f.name), &x)
			v = x
		case "int":
			var x types.Int64
			state(ctx, path.Root(f.name), &x)
			v = x
		case "float":
			var x types.Float64
			state(ctx, path.Root(f.name), &x)
			v = x
		case "list":
			var x types.List
			state(ctx, path.Root(f.name), &x)
			v = x
		}
		return v == nil || v.IsNull()
	}
	return false
}

func (r *kindResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	if _, _, err := r.k.splitID(req.ID); err != nil {
		resp.Diagnostics.AddError("cairn: import "+r.k.typeName, err.Error())
		return
	}
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
