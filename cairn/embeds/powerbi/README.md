# Power BI

Power BI shows the Cairn Core trust card through the **HTML Content** or **Web content** options.

## Power BI service (dashboard tile)

1. In Cairn Core → Workspace → Settings → Embeds, create a *Card* token with audience origin
   `https://app.powerbi.com` and copy the **iframe URL**.
2. On a Power BI **dashboard**, choose **Edit → Add a tile → Web content** and paste:

   ```html
   <iframe src="https://CAIRN_HOST/cairn/api/embed/v1/frame/card?urn=URN&token=TOKEN"
           style="border:0" width="420" height="190" title="Cairn Core trust"></iframe>
   ```

3. Add `https://app.powerbi.com` to `CAIRN_EMBED_ALLOWED_ORIGINS`.

## Reports (Desktop and service)

Report pages cannot host arbitrary iframes. Two options:

- Add a **Button** or text box with a link to the asset in Cairn Core (the card's title link), or
- use a certified "HTML Content"-style custom visual approved by your tenant admin and bind it to a
  measure that returns the iframe markup above.

Power BI Embedded and portals that host reports in your own page can use the web components
instead (see [portal](../portal/README.md)).
