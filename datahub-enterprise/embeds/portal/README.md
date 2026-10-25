# Portals, wikis and custom apps

Include the SDK once per page, then place the elements where you need them. They render in a shadow
DOM (no CSS leaks either way), fetch with the token only (no cookies), and re-render when their
attributes change.

```html
<script src="https://DATAHUB_HOST/dhe-ui/embed/sdk.js" defer></script>

<dhe-asset-card    urn="URN" token="TOKEN"></dhe-asset-card>
<dhe-quality-badge urn="URN" token="TOKEN"></dhe-quality-badge>
<dhe-lineage       urn="URN" token="TOKEN" depth="2" theme="dark"></dhe-lineage>
```

| Attribute | Meaning |
|---|---|
| `urn` | DataHub URN the token was issued for |
| `token` | Embed token (Workspace → Settings → Embeds) |
| `base-url` | DHE API base. Defaults to `data-dhe-base` on the script tag, else `<script origin>/dhe/api` |
| `theme` | `light` (default) or `dark` |
| `depth` | Lineage hops each way (`dhe-lineage` only, default 2) |

If the SDK is served from a different host than the API, set it once:
`<script src="https://cdn.example.com/dhe/sdk.js" data-dhe-base="https://DATAHUB_HOST/dhe/api" defer>`.

**Confluence / Notion / SharePoint** usually allow only iframes. Use the iframe URL from the embed
builder (`/dhe/api/embed/v1/frame/card?urn=…&token=…`) in their iframe/embed macro and add the wiki's
origin to `DHE_EMBED_ALLOWED_ORIGINS`.

**Styling.** The elements expose CSS custom properties on the host: `--dhe-fg`, `--dhe-bg`,
`--dhe-border`, `--dhe-accent`.
