# Tableau

Two options. Both show the Cairn Core trust card for the data source behind a dashboard.

## Option A: Web Page object (no install)

1. In Cairn Core, open the asset → **Access** tab → **Embed**, or go to Workspace → Settings → Embeds.
   Choose *Card* (or *Quality*/*Lineage*), set the audience origin to your Tableau origin, and copy the
   **iframe URL**.
2. In the Tableau dashboard, drag **Objects → Web Page** onto the layout and paste the URL.
3. Make sure your Tableau origin is in `CAIRN_EMBED_ALLOWED_ORIGINS`.

## Option B: Dashboard extension (`cairn-trust.trex`)

Use this to publish a curated, admin-approved extension.

1. Edit `cairn-trust.trex`: replace `CAIRN_HOST`, `URN` and `TOKEN` in `<source-location>` (keep the
   `&amp;` escaping), and change the `<author>` details.
2. Tableau Server/Cloud admins: **Settings → Extensions → Add URL to safe list**:
   `https://CAIRN_HOST/cairn/api/embed/v1/frame/` with *Allow full data access* **off**. The
   extension reads no workbook data.
3. In Tableau Desktop or web authoring: **Objects → Extension → Access Local Extensions** and select
   the `.trex`.

Tableau requires `https://` for extension URLs (`http://localhost` is allowed for testing). The card
reads `?theme=dark` if your dashboard uses a dark background.

> The extension page does not call the Tableau Extensions API, so it cannot pick the asset from the
> worksheet automatically. One `.trex` (or one Web Page object) maps to one asset.
