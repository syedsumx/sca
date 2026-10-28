import { Alert, Button, Input, Modal, Space, Table, Tag, message } from "antd";
import React, { useState } from "react";

import { useConsumerApi } from "../api/hooks";
import type { BulkApplyResult, BulkChange, BulkPreview } from "../api/types";
import { Muted } from "./tokens";

const WHAT: Record<BulkChange["what"], string> = {
  description: "Description",
  tags: "Tags",
  glossary_terms: "Glossary terms",
  owners: "Owners",
  domain: "Domain",
};

const short = (v: string) =>
  v.replace(/^urn:li:(corpuser|corpGroup|tag|glossaryTerm|domain):/, "");

/** Dataset name out of a dataset urn: urn:li:dataset:(platform,name,ENV). */
export function datasetName(urn: string): string {
  const m = /^urn:li:dataset:\([^,]+,(.+),[^,]+\)$/.exec(urn);
  return m ? m[1] : urn;
}

/** One change as a sentence fragment: what is added, removed or replaced. */
export function describeChange(c: BulkChange): React.ReactNode {
  if (c.add || c.remove)
    return (
      <>
        {(c.add ?? []).map((v) => (
          <Tag key={`+${v}`} color="green">
            + {short(v)}
          </Tag>
        ))}
        {(c.remove ?? []).map((v) => (
          <Tag key={`-${v}`} color="red">
            − {short(v)}
          </Tag>
        ))}
      </>
    );
  const to = c.to as string | null;
  if (!to) return <Muted>cleared</Muted>;
  return c.what === "domain"
    ? short(to)
    : `“${to.length > 80 ? `${to.slice(0, 80)}…` : to}”`;
}

function ChangeTable({
  rows,
  caption,
}: {
  rows: BulkChange[];
  caption: string;
}) {
  return (
    <div role="region" aria-label={caption}>
      <Table<BulkChange>
        size="small"
        rowKey={(c) => `${c.urn}|${c.field}|${c.what}`}
        dataSource={rows}
        pagination={
          rows.length > 10 ? { pageSize: 10, showSizeChanger: false } : false
        }
        columns={[
          { title: "Asset", render: (_, c) => datasetName(c.urn) },
          {
            title: "Column",
            dataIndex: "field",
            render: (f: string) => f || <Muted>—</Muted>,
          },
          { title: "Field", render: (_, c) => WHAT[c.what] },
          { title: "Change", render: (_, c) => describeChange(c) },
        ]}
      />
    </div>
  );
}

function download(name: string, text: string) {
  const url = URL.createObjectURL(
    new Blob([text], { type: "text/csv;charset=utf-8" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Export the current search to CSV, and import an edited CSV with a preview, apply and undo. */
export function BulkEdit({ query }: { query: string }) {
  const { client, cache } = useConsumerApi();
  const [open, setOpen] = useState(false);
  const [csv, setCsv] = useState("");
  const [preview, setPreview] = useState<BulkPreview | null>(null);
  const [applied, setApplied] = useState<BulkApplyResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setCsv("");
    setPreview(null);
    setApplied(null);
    setError(null);
  };

  const exportCsv = () =>
    run(async () => {
      download("cairn-metadata.csv", await client.bulkExport(query));
    });

  const readFile = (file: File) => file.text().then((t) => setCsv(t));

  const doPreview = () =>
    run(async () => {
      setPreview(await client.bulkImport(csv));
    });

  const doApply = () =>
    run(async () => {
      const out = await client.bulkApply(preview!.batch_id);
      setApplied(out);
      cache.invalidate("gql:");
      cache.invalidate("/collab/enrich");
      message.success(`${out.applied} change(s) applied`);
    });

  const doUndo = () =>
    run(async () => {
      const out = await client.bulkUndo(applied!.batch_id);
      cache.invalidate("gql:");
      cache.invalidate("/collab/enrich");
      message.success(`Undone: ${out.restored} aspect(s) restored`);
      setOpen(false);
      reset();
    });

  const footer = applied ? (
    <Space>
      <Button onClick={doUndo} loading={busy}>
        Undo this batch
      </Button>
      <Button type="primary" onClick={() => (setOpen(false), reset())}>
        Done
      </Button>
    </Space>
  ) : preview ? (
    <Space>
      <Button onClick={() => setPreview(null)}>Back</Button>
      <Button
        type="primary"
        disabled={!preview.changes.length}
        loading={busy}
        onClick={doApply}
      >
        Apply {preview.changes.length} change(s)
      </Button>
    </Space>
  ) : (
    <Button
      type="primary"
      disabled={!csv.trim()}
      loading={busy}
      onClick={doPreview}
    >
      Preview changes
    </Button>
  );

  return (
    <>
      <Space size={4}>
        <Button size="small" onClick={exportCsv} loading={busy && !open}>
          Export CSV
        </Button>
        <Button size="small" onClick={() => setOpen(true)}>
          Bulk edit…
        </Button>
      </Space>
      <Modal
        title="Bulk edit from a spreadsheet"
        open={open}
        width={820}
        onCancel={() => (setOpen(false), reset())}
        footer={footer}
      >
        {error && (
          <Alert
            type="error"
            showIcon
            message={error}
            style={{ marginBottom: 12 }}
          />
        )}
        {!preview && (
          <>
            <Muted style={{ display: "block", marginBottom: 8 }}>
              Export CSV, edit the description, owners, tags, glossary_terms or
              domain cells, then load the file here. Separate several values
              with “;”. Leave a cell empty to keep it, or put “-” to clear it.
            </Muted>
            <input
              type="file"
              accept=".csv,text/csv"
              aria-label="CSV file"
              onChange={(e) =>
                e.target.files?.[0] && void readFile(e.target.files[0])
              }
            />
            <Input.TextArea
              aria-label="CSV text"
              rows={8}
              style={{ marginTop: 8, fontFamily: "monospace", fontSize: 12 }}
              placeholder="…or paste the CSV here"
              value={csv}
              onChange={(e) => setCsv(e.target.value)}
            />
          </>
        )}
        {preview && !applied && (
          <>
            <p>
              {preview.rows} row(s) read. {preview.changes.length} change(s) to
              apply
              {preview.denied.length
                ? `, ${preview.denied.length} you are not allowed to make`
                : ""}
              .
            </p>
            {preview.changes.length ? (
              <ChangeTable rows={preview.changes} caption="Changes to apply" />
            ) : (
              <Alert
                type="info"
                message="Nothing to change: the file matches the catalog."
              />
            )}
            {preview.denied.length > 0 && (
              <>
                <h4 style={{ marginTop: 12 }}>Not allowed, will be skipped</h4>
                <ChangeTable
                  rows={preview.denied}
                  caption="Changes not allowed"
                />
              </>
            )}
            {preview.errors.length > 0 && (
              <Alert
                type="warning"
                style={{ marginTop: 12 }}
                message={`${preview.errors.length} row problem(s)`}
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {preview.errors.slice(0, 20).map((e) => (
                      <li key={e}>{e}</li>
                    ))}
                  </ul>
                }
              />
            )}
          </>
        )}
        {applied && (
          <Alert
            type="success"
            showIcon
            message={`${applied.applied} change(s) applied`}
            description={
              applied.already_current
                ? `${applied.already_current} were already current or no longer allowed, and were skipped.`
                : "You can undo the whole batch until someone else edits the same assets."
            }
          />
        )}
      </Modal>
    </>
  );
}
