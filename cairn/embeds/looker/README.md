# Looker

## Dashboard tile (iframe)

Looker dashboards (LookML) can render HTML in a text tile or a single-value visualization. Use the
iframe URL from Cairn Core → Workspace → Settings → Embeds:

```lookml
- name: catalog_trust
  type: text
  title_text: ""
  body_text: |
    <iframe src="https://CAIRN_HOST/cairn/api/embed/v1/frame/card?urn=URN&token=TOKEN"
            style="border:0;width:100%;height:190px" title="Cairn Core trust"></iframe>
```

Add `https://<your-instance>.looker.com` to `CAIRN_EMBED_ALLOWED_ORIGINS`. If your Looker admin has
disabled iframes in text tiles, use the Looker extension route below.

## Per-explore trust badge (LookML `html:`)

To show quality next to a field, point a dimension's `html:` at the quality frame:

```lookml
dimension: orders_trust {
  sql: 1 ;;
  html: <iframe src="https://CAIRN_HOST/cairn/api/embed/v1/frame/quality?urn=URN&token=TOKEN"
                style="border:0;height:32px;width:240px"></iframe> ;;
}
```

## Looker extension framework

In an extension (React), load the web components and render them directly:

```html
<script src="https://CAIRN_HOST/cairn-ui/embed/sdk.js" defer></script>
<cairn-asset-card urn="URN" token="TOKEN" base-url="https://CAIRN_HOST/cairn/api"></cairn-asset-card>
```

Add `CAIRN_HOST` to the extension's `external_api_urls` in `manifest.lkml`.
