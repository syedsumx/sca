package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"reflect"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/listplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// field is one attribute of a resource, as Cairn's API names it.
type field struct {
	name     string
	typ      string // string | bool | int | float | list | json (a JSON document, e.g. jsonencode([...]))
	required bool
	computed bool // Cairn decides it when not given (e.g. a debezium stream's mode)
	forceNew bool
	def      any // a static default; nil: null when not given
	desc     string
	api      string // Cairn's name for it, when Terraform reserves the name (e.g. connection)
	onlySet  bool   // a JSON document Cairn always returns: kept in state only when the configuration sets it
}

// key is the field's name in Cairn's API.
func (f field) key() string {
	if f.api != "" {
		return f.api
	}
	return f.name
}

// kind is one resource type and its API.
type kind struct {
	typeName, path, desc string
	byScope              bool   // maintenance policies: known by their scope, created and changed with PUT
	putKey               string // created and changed with PUT path/<this field's value> (policies: their id)
	idKey                string // the field of Cairn's records that is the Terraform id (default "id")
	item                 string // the item's path, "{id}" replaced (default path/{id})
	listKey              string // the list answer wraps the items under this key
	lookup               string // the data source's lookup attribute (default name)
	fields               []field
}

func (k kind) idField() string {
	switch {
	case k.idKey != "":
		return k.idKey
	case k.byScope:
		return "scope"
	}
	return "id"
}

func (k kind) itemPath(id string) string {
	if k.item != "" {
		return strings.ReplaceAll(k.item, "{id}", url.PathEscape(id))
	}
	return k.path + "/" + url.PathEscape(id)
}

// items is a list answer as records.
func (k kind) items(res any) []map[string]any {
	if k.listKey != "" {
		m, _ := res.(map[string]any)
		res = m[k.listKey]
	}
	var out []map[string]any
	xs, _ := res.([]any)
	for _, x := range xs {
		if m, ok := x.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

var kinds = []kind{
	{typeName: "policy", path: "/governance/policies", putKey: "id", listKey: "policies", lookup: "policy_id",
		desc: "One of Cairn's own access policies (source cairn), set and deleted on its own: the rest of the policy bundle stays as it is. Who may use a warehouse is such a policy (USE_WAREHOUSE on urn:li:warehouse:<name>).", fields: []field{
			{name: "policy_id", api: "id", typ: "string", required: true, forceNew: true, desc: "the policy's id (letters, digits, _ . : -)"},
			{name: "description", typ: "string", def: ""},
			{name: "effect", typ: "string", required: true, desc: "allow, deny or obligation"},
			{name: "priority", typ: "int", def: int64(0), desc: "higher is decided first"},
			{name: "actions", typ: "list", required: true, desc: "privileges, e.g. VIEW_DATASET_PROFILE, USE_WAREHOUSE, or *"},
			{name: "target", typ: "json", computed: true, desc: `jsonencode({resource_types = ["dataset"], resource_urns = [...]})`},
			{name: "condition", typ: "json", desc: `jsonencode({attr = "subject.groups", op = "contains", value = "analysts"})`},
			{name: "obligations", typ: "json", computed: true, desc: `jsonencode([{type = "mask_columns", ...}])`},
			{name: "enabled", typ: "bool", def: true},
		}},
	{typeName: "connection", path: "/sources", idKey: "urn", item: "/sources/{id}/detail",
		desc: "An ingestion source: a connector, its form values (secret fields name a Cairn secret, never its value), where it runs and when.", fields: []field{
			{name: "name", typ: "string", required: true},
			{name: "connector", typ: "string", required: true, forceNew: true, desc: "a gallery connector id (postgres, snowflake, ...), or custom"},
			{name: "values", typ: "json", required: true, desc: `jsonencode({host_port = "db:5432", database = "finance", username = "reader", password = "FINANCE_PG_PASSWORD"})`},
			{name: "advanced", typ: "json", computed: true, desc: "connector options beyond the form, as JSON"},
			{name: "recipe", typ: "json", onlySet: true, desc: "custom connector only: the whole recipe, as JSON"},
			{name: "schedule", typ: "json", desc: `jsonencode({interval = "0 2 * * *", timezone = "UTC"})`},
			{name: "executor", typ: "string", def: "default", desc: "default, or an agent pool"},
			{name: "owners", typ: "list", computed: true, desc: "user URNs; the token's user when not given"},
		}},
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
		{name: "encoding", typ: "string", def: "json", desc: "json, or avro (Confluent wire format, schemas from the Schema Registry)"},
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
	{typeName: "vector_index", path: "/v1/vectors", desc: "Vector search over a lakehouse table's text, kept in step with the table and searched under Cairn's policies.", fields: []field{
		{name: "name", typ: "string", required: true, forceNew: true},
		{name: "source", typ: "string", required: true, forceNew: true, desc: "namespace.table"},
		{name: "key", typ: "string", required: true, desc: "the source's key column"},
		{name: "text", typ: "string", required: true, desc: "the source's text column"},
		{name: "chunk_chars", typ: "int", def: int64(1500)},
		{name: "every_minutes", typ: "int"},
		{name: "enabled", typ: "bool", def: true},
	}},
	// The token user's own: saved queries, dashboards, alerts and SQL workflows.
	{typeName: "saved_query", path: "/sql/saved", desc: "A saved SQL query (read-only), with its chart; the token user's own.", fields: []field{
		{name: "name", typ: "string", required: true},
		{name: "sql", typ: "string", required: true},
		{name: "connection_name", api: "connection", typ: "string", required: true, desc: "the SQL connection it runs on (connection is a word Terraform keeps for itself)"},
		{name: "description", typ: "string", def: ""},
		{name: "shared", typ: "bool", def: false},
		{name: "chart", typ: "json", computed: true, desc: `jsonencode({type = "bar", x = "region", y = ["total"]})`},
	}},
	{typeName: "dashboard", path: "/sql/dashboards", desc: "Saved queries shown together, each run as its viewer.", fields: []field{
		{name: "name", typ: "string", required: true},
		{name: "description", typ: "string", def: ""},
		{name: "shared", typ: "bool", def: false},
		{name: "tiles", typ: "json", required: true, desc: `jsonencode([{query = cairn_saved_query.x.id, title = "...", width = 1 or 2}, {element = {...}, connection = "lakehouse"}, {metric = {metrics = [cairn_metric.revenue.name], dimensions = ["region"]}}])`},
		{name: "controls", typ: "json", onlySet: true, desc: `jsonencode([{id = "region", column = "region", kind = "values", source = {connection = "lakehouse", table = "sales.orders"}}])`},
	}},
	{typeName: "workbook", path: "/sql/workbooks", desc: "A spreadsheet workbook: elements compiled to SQL and run as the person looking.", fields: []field{
		{name: "name", typ: "string", required: true},
		{name: "connection_name", api: "connection", typ: "string", required: true, desc: "the SQL connection its elements run on"},
		{name: "spec", typ: "json", required: true, desc: `jsonencode({elements = [{source = {table = "sales.orders"}, group_by = ["region"], aggregates = [{name = "n", formula = "Count()"}]}]})`},
		{name: "description", typ: "string", def: ""},
		{name: "shared", typ: "bool", def: false},
	}},
	{typeName: "metric", path: "/sql/metrics", idKey: "name", desc: "A business measure defined once (a workbook aggregate over one table), the same number in every query, workbook and dashboard; run as the person asking.", fields: []field{
		{name: "name", typ: "string", required: true, forceNew: true, desc: "lowercase letters, digits and _: how queries and tiles refer to it"},
		{name: "label", typ: "string", required: true},
		{name: "connection_name", api: "connection", typ: "string", required: true},
		{name: "table", typ: "string", required: true, desc: "schema.table"},
		{name: "measure", typ: "string", required: true, desc: "a workbook aggregate, e.g. Sum([amount])"},
		{name: "description", typ: "string", def: ""},
		{name: "filters", typ: "json", onlySet: true, desc: `jsonencode([{column = "status", op = "=", value = "paid"}])`},
		{name: "time_column", typ: "string"},
		{name: "dimensions", typ: "list", def: []string{}},
	}},
	{typeName: "input_table", path: "/sql/input-tables", desc: "A lakehouse table people type into, written on Trino as the person writing. Created once: any change replaces it in Terraform's plan, and destroying it unregisters it (the table and its rows stay).", fields: []field{
		{name: "table", api: "table", typ: "string", required: true, forceNew: true, desc: "schema.table, lowercase"},
		{name: "connection_name", api: "connection", typ: "string", required: true, forceNew: true, desc: "a Trino connection that runs as the user"},
		{name: "columns", typ: "json", required: true, forceNew: true, desc: `jsonencode([{name = "region", type = "text"}, {name = "target", type = "number"}])`},
		{name: "description", typ: "string", def: "", forceNew: true},
	}},
	{typeName: "sql_alert", path: "/sql/alerts", desc: "A saved query checked on a schedule as its owner; its owner and recipients are told when the condition starts and stops holding.", fields: []field{
		{name: "name", typ: "string", required: true},
		{name: "query", typ: "string", required: true, desc: "the saved query's id"},
		{name: "measure", typ: "string", def: "rows", desc: "rows, or a column of the first row"},
		{name: "op", typ: "string", required: true, desc: "> >= < <= == !="},
		{name: "value", typ: "float", required: true},
		{name: "every_minutes", typ: "int", def: int64(60)},
		{name: "recipients", typ: "list", def: []string{}},
		{name: "enabled", typ: "bool", def: true},
	}},
	{typeName: "sql_workflow", path: "/v1/sql-workflows", desc: "Steps that depend on each other (derived-table refreshes, checks, report queries), on a schedule, run as their owner.", fields: []field{
		{name: "name", typ: "string", required: true},
		{name: "steps", typ: "json", required: true, desc: `jsonencode([{name = "refresh", type = "derived", table = "revenue"}, {name = "check", type = "check", query = cairn_saved_query.x.id, op = ">", value = 0, after = ["refresh"]}])`},
		{name: "cron", typ: "string", desc: "five fields, UTC"},
		{name: "recipients", typ: "list", def: []string{}},
		{name: "notify", typ: "string", def: "failure", desc: "failure or always"},
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
		case "float":
			attrs[f.name] = schema.Float64Attribute{Required: f.required, Optional: opt, Computed: comp, Description: f.desc}
		case "json":
			a := schema.StringAttribute{Required: f.required, Optional: opt, Computed: comp, Description: f.desc}
			if f.computed {
				a.PlanModifiers = append(a.PlanModifiers, stringplanmodifier.UseStateForUnknown(), subsetOfState{})
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
			if f.computed {
				a.PlanModifiers = append(a.PlanModifiers, listplanmodifier.UseStateForUnknown())
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
				out[f.key()] = v.ValueString()
			}
		case "bool":
			var v types.Bool
			diags.Append(get(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.key()] = v.ValueBool()
			}
		case "int":
			var v types.Int64
			diags.Append(get(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.key()] = v.ValueInt64()
			}
		case "float":
			var v types.Float64
			diags.Append(get(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				out[f.key()] = v.ValueFloat64()
			}
		case "json":
			var v types.String
			diags.Append(get(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				var doc any
				if err := json.Unmarshal([]byte(v.ValueString()), &doc); err != nil {
					diags.AddAttributeError(p, "not JSON", err.Error())
				}
				out[f.key()] = doc
			}
		case "list":
			var v types.List
			diags.Append(get(ctx, p, &v)...)
			if !v.IsNull() && !v.IsUnknown() {
				var ss []string
				diags.Append(v.ElementsAs(ctx, &ss, false)...)
				out[f.key()] = ss
			}
		}
	}
	return out, diags
}

type setter func(context.Context, path.Path, any) diag.Diagnostics

// store writes what Cairn returned into the state. “prior“ is the plan (or the state being refreshed):
// a JSON document Cairn holds everything of is kept as written, so Cairn's own additions are not drift.
func (r *kindResource) store(ctx context.Context, obj map[string]any, set setter, prior getter) diag.Diagnostics {
	var diags diag.Diagnostics
	diags.Append(set(ctx, path.Root("id"), types.StringValue(fmt.Sprint(obj[r.k.idField()])))...)
	for _, f := range r.k.fields {
		v, present := obj[f.key()]
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
				if f.onlySet && (was.IsNull() || was.IsUnknown()) {
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

// find reads one resource by id (Cairn lists them); nil: it is gone.
func (r *kindResource) find(ctx context.Context, id string) (map[string]any, error) {
	res, err := r.c.call(ctx, "GET", r.k.path, nil)
	if err != nil {
		return nil, err
	}
	for _, m := range r.k.items(res) {
		if fmt.Sprint(m[r.k.idField()]) == id {
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
	case r.k.putKey != "":
		res, err = r.c.call(ctx, "PUT", r.k.itemPath(fmt.Sprint(body[r.k.putKey])), body)
	case id == "":
		res, err = r.c.call(ctx, "POST", r.k.path, body)
	default:
		res, err = r.c.call(ctx, "PUT", r.k.itemPath(id), body)
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
		return r.find(ctx, fmt.Sprint(m[r.k.idField()]))
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
	resp.Diagnostics.Append(r.store(ctx, obj, resp.State.SetAttribute, req.Plan.GetAttribute)...)
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
	resp.Diagnostics.Append(r.store(ctx, obj, resp.State.SetAttribute, req.State.GetAttribute)...)
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
	resp.Diagnostics.Append(r.store(ctx, obj, resp.State.SetAttribute, req.Plan.GetAttribute)...)
}

func (r *kindResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var id types.String
	resp.Diagnostics.Append(req.State.GetAttribute(ctx, path.Root("id"), &id)...)
	if resp.Diagnostics.HasError() {
		return
	}
	_, err := r.c.call(ctx, "DELETE", r.k.itemPath(id.ValueString()), nil)
	var ae *apiError
	if err != nil && !(errors.As(err, &ae) && ae.status == 404) {
		resp.Diagnostics.AddError("cairn: delete "+r.k.typeName, err.Error())
	}
}

func (r *kindResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	resource.ImportStatePassthroughID(ctx, path.Root("id"), req, resp)
}
