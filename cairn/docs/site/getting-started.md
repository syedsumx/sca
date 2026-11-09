---
title: Getting started
order: 1
---
# Getting started

## Sign in

Open Cairn in your browser and sign in with your company account (single sign-on) or the
credentials your administrator gave you. The first administrator account is `admin`.

## Two ways in

* **Workspace** (the *Workspace* group in the left navigation) is the everyday view: **Discover**
  (search, bookmarks, trending assets), **Data marketplace** (data products), **Requests**, **Governance
  studio**, **Context inbox** and the **Assistant**. Each has its own place in the navigation and
  its own address, so you can bookmark or share it.
* **Catalog** pages (Home, Glossary, Domains, Data Sources, Settings) are where stewards and
  administrators curate metadata, manage ingestion and configure access.

## Find data

* Type in the search bar (`⌘K` or `/`). Results show trust signals at a glance: certification,
  quality status, PII and freshness.
* Narrow results with facets: type, platform, domain, certification, quality, sensitivity, owner.
  The query and filters live in the URL, so a search can be shared as a link.
* Discover shows what is trending in your domain, your bookmarks, recently viewed assets and the
  featured data products.

## Understand an asset

An asset page has tabs for **Summary** (description, owners, business terms, downstream use),
**Columns** (types, classifications and whether a column is masked for you), **Discussion**,
**Quality**, **Lineage**, **Access** and **History**.

## Collaborate

* **Bookmark** assets you use and **follow** the ones you care about: you are notified about
  quality incidents, schema changes and new comments.
* **Discuss** an asset or a single column. Mention colleagues with `@name`; replies and reactions
  are threaded.

## Creating and changing things: forms

The platform hubs (Ingestion, Lakehouse, Compute & jobs, Agents, Machine learning, Trust & security, Sharing, Cost &
usage, Semantic models) list each area's items in a table. Creating, editing or running something opens a form:

* **Fields, not JSON.** Each field has a label and help. Lists take a value per entry (press Enter after each);
  schedules show what they mean as you type (`0 2 * * *` reads "Every day at 02:00 UTC"); durations show their unit
  and the same time in larger units; SQL fields complete table and column names.
* **Pickers search Cairn.** Tables, datasets, connections, warehouses, people and groups, secret scopes and secret
  references, git folders, flows and agents are chosen from what Cairn lists for you. When something is not listed,
  type it and choose **Use "…"**; for a person or a group, choose which it is.
* **Secrets stay secret.** A secret's value is typed into a password field, and Cairn never shows it again: an edit
  form leaves it empty, and typing a new value replaces it.
* **Advanced options.** Rarely used fields are under **Advanced options** at the end of the form. It opens by
  itself when one of them has a problem.
* **Raw JSON.** The **Raw JSON** switch at the top of a form shows the request as JSON. Edit it there and switch
  back: the form takes the values, and keys the form doesn't know are kept and sent.
* **Problems are shown on the field.** A missing or badly formed value is marked on its field before anything is
  sent; when Cairn refuses a value, its reason is shown on that field too.
* **Edits start from what is there**, and a field that can't change after creation is shown but not editable.
  Deleting asks first and names what will be deleted.

When something can't be done, Cairn says why in plain words and what to do next: for example that you need a
permission and whom to ask, or that a feature isn't set up yet, with a link to how an admin turns it on. The API's
own answer is under **Details**, with a button to copy it for an admin. An empty area says what it is for, offers
its **Create** button and links to its section of these docs (**Learn more**).

## Language

Choose the interface language at the top right, next to **Ask**. The choice applies to this browser. Without a
choice, Cairn uses your browser's language when it is available: English, German, French, Spanish or Japanese.
These docs are also available in German, French, Spanish and Japanese; use the language links at the top of
each page. Pages that aren't translated yet appear in English.
