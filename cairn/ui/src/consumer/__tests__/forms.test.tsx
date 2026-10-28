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
import {
  FormTasksList,
  FormsAdmin,
  draftToSpec,
  toUserUrn,
} from "../components/Forms";
import { TrustBadges } from "../components/TrustBadges";
import { CairnProvider } from "../context/CairnProvider";
import { DS, MockCall, UI_CONFIG, makeClient } from "./mockApi";
import { installMatchMedia } from "./setup";

beforeAll(installMatchMedia);

const TASK = {
  form_id: "form_1",
  form_name: "PII ownership",
  urn: DS,
  state: "PENDING",
  kind: "verification",
  assigned_ms: 1,
  due_ms: 2,
};
const DETAIL = {
  ...TASK,
  assignees: ["urn:li:corpuser:ana"],
  completed_by: null,
  verified_by: null,
  answers: { desc: "Orders" },
  questions: [
    {
      id: "desc",
      type: "description",
      title: "What is in it?",
      required: true,
      allowed: [],
      multiple: true,
    },
    {
      id: "retention",
      type: "structured_property",
      title: "Retention",
      required: true,
      allowed: ["1y", "7y"],
      multiple: false,
      property_urn: "urn:li:structuredProperty:r",
    },
    {
      id: "dpia",
      type: "yes_no",
      title: "DPIA done?",
      required: false,
      allowed: [],
      multiple: true,
    },
  ],
};

function wrap(
  node: React.ReactNode,
  routes: Record<string, unknown>,
  calls: MockCall[],
) {
  const { client } = makeClient(
    { "GET /ui/config": UI_CONFIG, ...routes },
    calls,
  );
  return render(
    <MemoryRouter>
      <CairnProvider client={client} cache={new QueryCache()}>
        {node}
      </CairnProvider>
    </MemoryRouter>,
  );
}

describe("completing a form task", () => {
  it("shows pre-filled answers and submits with sign-off", async () => {
    const calls: MockCall[] = [];
    wrap(
      <FormTasksList />,
      {
        "GET /governance/forms/tasks": [TASK],
        [`GET /governance/forms/form_1/tasks/${DS}`]: DETAIL,
        [`POST /governance/forms/form_1/tasks/${DS}`]: { state: "VERIFIED" },
      },
      calls,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Complete" }));
    const desc = (await screen.findByLabelText(
      "What is in it?",
    )) as HTMLTextAreaElement;
    expect(desc.value).toBe("Orders");
    fireEvent.change(desc, { target: { value: "One row per order." } });
    fireEvent.click(screen.getByLabelText("Yes"));
    fireEvent.click(screen.getByRole("checkbox", { name: /I confirm/ }));
    fireEvent.click(screen.getByRole("button", { name: "Submit and verify" }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "POST")).toBe(true),
    );
    const body = calls.find((c) => c.method === "POST")!.body as {
      answers: Record<string, unknown>;
      verify: boolean;
    };
    expect(body).toEqual({
      answers: { desc: "One row per order.", dpia: true },
      verify: true,
    });
  });

  it("shows the server refusal in the drawer", async () => {
    wrap(
      <FormTasksList />,
      {
        "GET /governance/forms/tasks": [TASK],
        [`GET /governance/forms/form_1/tasks/${DS}`]: DETAIL,
        [`POST /governance/forms/form_1/tasks/${DS}`]: () =>
          new Response(
            JSON.stringify({ detail: "Retention: an answer is required" }),
            { status: 400 },
          ),
      },
      [],
    );
    fireEvent.click(await screen.findByRole("button", { name: "Complete" }));
    await screen.findByLabelText("What is in it?");
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(
      await screen.findByText("Retention: an answer is required"),
    ).toBeTruthy();
  });
});

describe("managing forms", () => {
  it("turns the simple selector into one policy condition", () => {
    const spec = draftToSpec({
      name: " PII ",
      kind: "completion",
      dueDays: 7,
      classification: "PII",
      domain: "finance",
      platform: "snowflake",
      tag: "",
      ownershipTypes: ["DATAOWNER"],
      fallbackUsers: ["ops"],
      questions: [
        {
          id: "",
          type: "text",
          title: "Notes",
          required: false,
          allowed: [],
          multiple: true,
        },
      ],
    });
    expect(spec.name).toBe("PII");
    expect(spec.select).toEqual({
      all: [
        { attr: "resource.classification", op: "contains", value: "PII" },
        {
          attr: "resource.domains",
          op: "contains",
          value: "urn:li:domain:finance",
        },
        { attr: "resource.platform", op: "eq", value: "snowflake" },
      ],
    });
    expect(spec.assignees.users).toEqual(["urn:li:corpuser:ops"]);
    expect(spec.questions[0].id).toBe("q1");
    expect(toUserUrn("urn:li:corpGroup:x")).toBe("urn:li:corpGroup:x");
  });

  it("creates a form, previews the assignment and assigns", async () => {
    const calls: MockCall[] = [];
    const form = {
      id: "form_9",
      name: "PII ownership",
      state: "ACTIVE",
      created_by: "x",
      created_ms: 1,
      spec: { kind: "completion", questions: [{}, {}] },
      progress: {
        assets: 0,
        PENDING: 0,
        COMPLETED: 0,
        VERIFIED: 0,
        done_pct: 0,
      },
    };
    wrap(
      <FormsAdmin />,
      {
        "GET /governance/forms": [form],
        "POST /governance/forms": form,
        "POST /governance/forms/form_9/assign": (_b: unknown, url: URL) => ({
          matched: 3,
          assigned: ["a", "b", "c"],
          reassigned: [],
          withdrawn: [],
          without_assignee: ["c"],
          dry_run: url.searchParams.get("dry_run") === "true",
        }),
      },
      calls,
    );
    fireEvent.click(await screen.findByRole("button", { name: "New form" }));
    fireEvent.change(screen.getByLabelText("Form name"), {
      target: { value: "PII ownership" },
    });
    fireEvent.change(screen.getByLabelText("Classification"), {
      target: { value: "PII" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create form" }));
    await waitFor(() =>
      expect(
        calls.some(
          (c) => c.method === "POST" && c.path === "/governance/forms",
        ),
      ).toBe(true),
    );
    const created = calls.find(
      (c) => c.path === "/governance/forms" && c.method === "POST",
    )!.body as { questions: unknown[]; select: unknown };
    expect(created.questions).toHaveLength(2);
    fireEvent.click(await screen.findByRole("button", { name: "Assign…" }));
    const dialog = (await screen.findByText(/dataset\(s\) match/)).closest(
      ".ant-modal",
    ) as HTMLElement;
    expect(
      within(dialog).getByText(/3 dataset\(s\) match\. 3 will get a new task/),
    ).toBeTruthy();
    expect(
      within(dialog).getByText(/1 dataset\(s\) have no matching owner/),
    ).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Assign" }));
    await waitFor(() =>
      expect(
        calls
          .filter((c) => c.path === "/governance/forms/form_9/assign")
          .map((c) => c.url.searchParams.get("dry_run")),
      ).toEqual(["true", "false"]),
    );
  });
});

it("a finished form shows 100%, not just a tick", async () => {
  const form = {
    id: "form_2",
    name: "Done form",
    state: "ACTIVE",
    created_by: "x",
    created_ms: 1,
    spec: { kind: "completion", questions: [{}] },
    progress: {
      assets: 1,
      PENDING: 0,
      COMPLETED: 1,
      VERIFIED: 0,
      done_pct: 100,
    },
  };
  wrap(<FormsAdmin />, { "GET /governance/forms": [form] }, []);
  const row = (await screen.findByText("Done form")).closest(
    "tr",
  ) as HTMLElement;
  expect(within(row).getByText("100%")).toBeTruthy();
});

it("trust badges show compliance", () => {
  render(<TrustBadges compliance="INCOMPLETE" qualityStatus="PASS" />);
  expect(screen.getByText("Forms open")).toBeTruthy();
});
