// Package provider is Cairn's Terraform provider.
package provider

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/provider/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// New returns the provider factory.
func New(version string) func() provider.Provider {
	return func() provider.Provider { return &cairnProvider{version: version} }
}

type cairnProvider struct{ version string }

type providerModel struct {
	URL    types.String `tfsdk:"url"`
	Token  types.String `tfsdk:"token"`
	CAFile types.String `tfsdk:"ca_file"`
}

func (p *cairnProvider) Metadata(_ context.Context, _ provider.MetadataRequest, resp *provider.MetadataResponse) {
	resp.TypeName = "cairn"
	resp.Version = p.version
}

func (p *cairnProvider) Schema(_ context.Context, _ provider.SchemaRequest, resp *provider.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Cairn: pipes, streams, derived tables and table maintenance, through Cairn's API as the token's user.",
		Attributes: map[string]schema.Attribute{
			"url":     schema.StringAttribute{Optional: true, Description: "Cairn's API (default: CAIRN_URL)."},
			"token":   schema.StringAttribute{Optional: true, Sensitive: true, Description: "A Cairn token (default: CAIRN_TOKEN, or the file CAIRN_TOKEN_FILE)."},
			"ca_file": schema.StringAttribute{Optional: true, Description: "A CA for Cairn's certificate (default: CAIRN_CA_FILE, else the system's)."},
		},
	}
}

func pick(v types.String, env string) string {
	if !v.IsNull() && !v.IsUnknown() && v.ValueString() != "" {
		return v.ValueString()
	}
	return os.Getenv(env)
}

func (p *cairnProvider) Configure(ctx context.Context, req provider.ConfigureRequest, resp *provider.ConfigureResponse) {
	var m providerModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	url, token, ca := pick(m.URL, "CAIRN_URL"), pick(m.Token, "CAIRN_TOKEN"), pick(m.CAFile, "CAIRN_CA_FILE")
	if token == "" && os.Getenv("CAIRN_TOKEN_FILE") != "" {
		b, err := os.ReadFile(os.Getenv("CAIRN_TOKEN_FILE"))
		if err != nil {
			resp.Diagnostics.AddError("cairn: token file", err.Error())
			return
		}
		token = strings.TrimSpace(string(b))
	}
	if url == "" || token == "" {
		resp.Diagnostics.AddError("cairn: not configured", "set url and token (or CAIRN_URL and CAIRN_TOKEN)")
		return
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	if ca != "" {
		pem, err := os.ReadFile(ca)
		if err != nil {
			resp.Diagnostics.AddError("cairn: ca_file", err.Error())
			return
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			resp.Diagnostics.AddError("cairn: ca_file", "no certificates in "+ca)
			return
		}
		tr.TLSClientConfig = &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	}
	c := &client{base: strings.TrimRight(url, "/"), token: token, http: &http.Client{Transport: tr, Timeout: 60 * time.Second}}
	resp.ResourceData = c
	resp.DataSourceData = c
}

func (p *cairnProvider) Resources(_ context.Context) []func() resource.Resource {
	out := []func() resource.Resource{}
	for _, k := range kinds {
		k := k
		out = append(out, func() resource.Resource { return &kindResource{k: k} })
	}
	return out
}

func (p *cairnProvider) DataSources(_ context.Context) []func() datasource.DataSource {
	out := []func() datasource.DataSource{}
	for _, k := range kinds {
		k := k
		out = append(out, func() datasource.DataSource { return &kindData{k: k} })
	}
	return out
}

type client struct {
	base, token string
	http        *http.Client
}

// apiError is Cairn's refusal, with its status (404: the resource is gone).
type apiError struct {
	status int
	msg    string
}

func (e *apiError) Error() string { return fmt.Sprintf("cairn: %d %s", e.status, e.msg) }

func (c *client) call(ctx context.Context, method, path string, body any) (any, error) {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, rd)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "terraform-provider-cairn")
	res, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(res.Body, 16<<20))
	if res.StatusCode >= 300 {
		msg := string(data)
		var e struct {
			Detail any `json:"detail"`
		}
		if json.Unmarshal(data, &e) == nil && e.Detail != nil {
			msg = fmt.Sprint(e.Detail)
		}
		return nil, &apiError{res.StatusCode, msg}
	}
	if len(data) == 0 {
		return nil, nil
	}
	var out any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	return out, nil
}
