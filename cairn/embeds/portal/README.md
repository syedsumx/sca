# Portals, wikis and custom apps

Include the SDK once per page, then place the elements where you need them. They render in a shadow
DOM (no CSS leaks either way), fetch with the token only (no cookies), and re-render when their
attributes change.

```html
<script src="https://CAIRN_HOST/cairn-ui/embed/sdk.js" defer></script>

<cairn-asset-card    urn="URN" token="TOKEN"></cairn-asset-card>
<cairn-quality-badge urn="URN" token="TOKEN"></cairn-quality-badge>
<cairn-lineage       urn="URN" token="TOKEN" depth="2" theme="dark"></cairn-lineage>
```

| Attribute | Meaning |
|---|---|
| `urn` | Cairn Core URN the token was issued for |
| `token` | Embed token (Workspace → Settings → Embeds) |
| `base-url` | Cairn API base. Defaults to `data-cairn-base` on the script tag, else `<script origin>/cairn/api` |
| `theme` | `light` (default) or `dark` |
| `depth` | Lineage hops each way (`cairn-lineage` only, default 2) |

If the SDK is served from a different host than the API, set it once:
`<script src="https://cdn.example.com/cairn/sdk.js" data-cairn-base="https://CAIRN_HOST/cairn/api" defer>`.

**Confluence / Notion / SharePoint** usually allow only iframes. Use the iframe URL from the embed
builder (`/cairn/api/embed/v1/frame/card?urn=…&token=…`) in their iframe/embed macro and add the wiki's
origin to `CAIRN_EMBED_ALLOWED_ORIGINS`.

**Styling.** The elements expose CSS custom properties on the host: `--cairn-fg`, `--cairn-bg`,
`--cairn-border`, `--cairn-accent`.
