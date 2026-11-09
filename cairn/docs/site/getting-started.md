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

## A guided start

The first time you open Cairn's home page, it asks what you mostly do here: **Analyst**, **Data engineer**, **Data steward**,
**Admin**, or **Just exploring**. If your profile already names a role, that answer is suggested. Your answer
shapes three things, and you can change it at any time under **Get started** in the sidebar:

* **A checklist.** **Get started** lists a few first tasks for your role, each with a link to where it is done:
  * Analyst: find a dataset, run a query, save it, build a dashboard, ask a question.
  * Data engineer: connect a source, create a pipe or a flow, schedule it, add a quality check.
  * Data steward: review the ontology's proposals, add a glossary term, set an owner, review access requests.
  * Admin: single sign-on, groups, policies, the AI gateway, budgets.

  A step ticks itself when Cairn sees you did it (for example, once you have a saved query), and you can tick any
  step yourself. Your progress is kept in your profile, so it is the same in every browser, in Cairn Desktop and on
  your phone. **Hide** puts the checklist away; it stays under **Get started**. An administrator can turn the
  question and the checklist off for everyone.
* **The sidebar.** A **For you** group at the top holds the pages your role uses most and the ones you pinned. The
  groups your role rarely needs are folded away; **Show everything** brings them back.
* **The command palette** ranks the actions your role uses most first.

The sidebar never offers a page you can't use at all: areas only administrators can open are left out for everyone
else. That is a convenience, not the control: Cairn checks every request, whatever the sidebar shows.

## Command palette and shortcuts

Press **⌘K** (on Windows and Linux, **Ctrl+K**), or **Search or jump to…** at the top, to open the command palette. Type a
few letters of what you want: letters can be skipped, so `sqwf` finds **SQL workflows**.

* **Pages and tabs.** Every page and every tab of the platform hubs (for example **Trust & security › Secret
  scopes**).
* **Actions.** **New saved query**, **New flow**, **Run flow…**, **Ask the assistant**, and a **Create …** for each
  form in the hubs (**Create secret scope**, **Create notification route**, …).
* **Recent.** The pages you opened last, on this device.
* **Catalog.** Datasets, glossary terms and dashboards from the catalog, found as you type. Cairn searches as you,
  so you only see what you may see.
* **Ask.** The last entry, **Ask: …**, hands what you typed to the assistant.

Use **↑** and **↓** to move, **Enter** to open, **Esc** to close. Press **?** anywhere (outside a text field) for the
list of keyboard shortcuts:

| Keys | What it does |
|---|---|
| ⌘K / Ctrl+K | Command palette |
| / | Search the catalog |
| ⌘J / Ctrl+J | The assistant |
| ? | Keyboard shortcuts |
| g then h | Get started |
| Esc | Close a dialog, panel or palette |

## Help on every page

The **?** at the top of every page (and next to the title of the platform hubs and most pages) opens the part of these docs about that
page, beside your work, with **Open full docs** and **Ask the assistant about this page** (which starts a question
with the page's name and section). The help is part of the app, so it also opens without a connection.

## Customising the sidebar

**Customise sidebar** (at the bottom of the sidebar) lets you pin a page to **For you** or hide one you never use.
Your choices are kept in your profile and follow you to every device. **Show everything** shows all the pages you
may use, including the ones you hid; **Reset** goes back to your role's defaults. The platform hubs remember the
tab you used last.

## Find data

* Type in the search bar (`/`), or open the command palette (`⌘K`). Results show trust signals at a glance:
  certification, quality status, PII and freshness.
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
