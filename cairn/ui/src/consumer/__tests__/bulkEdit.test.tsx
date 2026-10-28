import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";

import { QueryCache } from "../api/cache";
import { BulkEdit, datasetName, describeChange } from "../components/BulkEdit";
import { CairnProvider } from "../context/CairnProvider";
import { DS, MockCall, UI_CONFIG, makeClient } from "./mockApi";
import { installMatchMedia } from "./setup";

beforeAll(installMatchMedia);

const PREVIEW = {
  batch_id: "bulk_1",
  rows: 3,
  changes: [
    {
      urn: DS,
      field: "",
      what: "description",
      from: "Orders",
      to: "One row per order",
      privilege: "EDIT_ENTITY_DOCS",
    },
    {
      urn: DS,
      field: "email",
      what: "tags",
      from: ["urn:li:tag:pii"],
      to: ["urn:li:tag:email"],
      add: ["urn:li:tag:email"],
      remove: ["urn:li:tag:pii"],
    },
  ],
  denied: [
    {
      urn: DS,
      field: "",
      what: "domain",
      from: null,
      to: "urn:li:domain:finance",
      privilege: "EDIT_ENTITY_DOMAINS",
    },
  ],
  errors: ["line 4: urn:li:chart:(x,y) is not a dataset urn"],
};

function wrap(routes: Record<string, unknown>, calls: MockCall[]) {
  const { client } = makeClient(
    { "GET /ui/config": UI_CONFIG, ...routes },
    calls,
  );
  return render(
    <MemoryRouter>
      <CairnProvider client={client} cache={new QueryCache()}>
        <BulkEdit query="orders" />
      </CairnProvider>
    </MemoryRouter>,
  );
}

it("previews an edited CSV, applies it and undoes it", async () => {
  const calls: MockCall[] = [];
  wrap(
    {
      "POST /bulk/import": PREVIEW,
      "POST /bulk/bulk_1/apply": {
        batch_id: "bulk_1",
        applied: 2,
        already_current: 0,
        aspects: 2,
      },
      "POST /bulk/bulk_1/undo": { batch_id: "bulk_1", restored: 2 },
    },
    calls,
  );
  fireEvent.click(screen.getByRole("button", { name: "Bulk edit…" }));
  const csv = `urn,field,description\n${DS},,One row per order\n`;
  fireEvent.change(await screen.findByLabelText("CSV text"), {
    target: { value: csv },
  });
  fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
  const table = await screen.findByRole("region", { name: "Changes to apply" });
  expect(within(table).getByText("“One row per order”")).toBeTruthy();
  expect(within(table).getByText("+ email")).toBeTruthy();
  expect(within(table).getByText("− pii")).toBeTruthy();
  expect(screen.getByText(/1 you are not allowed to make/)).toBeTruthy();
  expect(screen.getByText(/is not a dataset urn/)).toBeTruthy();
  const imported = calls.find((c) => c.path === "/bulk/import")!;
  expect(imported.body).toBe(csv);
  fireEvent.click(screen.getByRole("button", { name: "Apply 2 change(s)" }));
  expect(
    await screen.findByText("2 change(s) applied", {
      selector: ".ant-alert-message",
    }),
  ).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Undo this batch" }));
  await waitFor(() =>
    expect(calls.some((c) => c.path === "/bulk/bulk_1/undo")).toBe(true),
  );
});

it("shows the server refusal", async () => {
  wrap(
    {
      "POST /bulk/import": () =>
        new Response(
          JSON.stringify({
            detail: "the first row must be a header with a urn column",
          }),
          { status: 400 },
        ),
    },
    [],
  );
  fireEvent.click(screen.getByRole("button", { name: "Bulk edit…" }));
  fireEvent.change(await screen.findByLabelText("CSV text"), {
    target: { value: "a,b\n" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Preview changes" }));
  expect(
    await screen.findByText("the first row must be a header with a urn column"),
  ).toBeTruthy();
});

it("exports the search as CSV", async () => {
  const calls: MockCall[] = [];
  const created: string[] = [];
  URL.createObjectURL = (() => {
    created.push("x");
    return "blob:x";
  }) as typeof URL.createObjectURL;
  URL.revokeObjectURL = () => undefined;
  wrap(
    {
      "GET /bulk/export": () =>
        new Response("urn,field\n", {
          status: 200,
          headers: { "Content-Type": "text/csv" },
        }),
    },
    calls,
  );
  fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
  await waitFor(() => expect(created).toHaveLength(1));
  expect(calls[calls.length - 1].url.searchParams.get("query")).toBe("orders");
});

it("names datasets and describes changes", () => {
  expect(datasetName(DS)).toBe("analytics.public.orders");
  render(
    <>
      {describeChange({
        urn: DS,
        field: "",
        what: "domain",
        from: "urn:li:domain:x",
        to: null,
      })}
    </>,
  );
  expect(screen.getByText("cleared")).toBeTruthy();
});
