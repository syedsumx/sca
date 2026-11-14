"""The fine-tuning worker: run by Cairn as ``python -I -m cairn.ml.finetune_train <job.json>`` in a process of its
own, with no network (models come from ``CAIRN_FINETUNE_MODEL_DIR``) and none of Cairn's environment.

``job.json``: ``{"mode": "train" | "generate", "base": <model folder>, "data": <train.jsonl>, "out": <folder>,
"epochs", "learning_rate", "lora", "max_length", "batch_size"}``; for ``generate``, ``"adapter"`` (the trained
folder), ``"prompts"`` and ``"max_new_tokens"``. Training writes the model (a LoRA adapter, or the whole model)
and the tokenizer to ``out`` and ``metrics.json`` beside them; generating prints JSON on stdout.
"""

from __future__ import annotations

import json
import math
import random
import sys
import time


def _load(base: str):
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(base, local_files_only=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(base, local_files_only=True)
    return tok, model


def train(job: dict) -> dict:
    import torch

    torch.manual_seed(0)
    random.seed(0)
    tok, model = _load(job["base"])
    if job.get("lora", True):
        from peft import LoraConfig, TaskType, get_peft_model

        model = get_peft_model(model, LoraConfig(task_type=TaskType.CAUSAL_LM, r=int(job.get("lora_rank", 8)),
                                                 lora_alpha=int(job.get("lora_rank", 8)) * 2, lora_dropout=0.0))
    examples = [json.loads(line) for line in open(job["data"]) if line.strip()]
    texts = [e["prompt"] + e["completion"] + (tok.eos_token or "") for e in examples]
    prompt_lens = [len(tok(e["prompt"])["input_ids"]) for e in examples]
    max_len = int(job.get("max_length", 256))
    batch = max(1, int(job.get("batch_size", 4)))
    optim = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=float(job["learning_rate"]))
    model.train()
    losses, started = [], time.time()
    order = list(range(len(texts)))
    for _ in range(int(job["epochs"])):
        random.shuffle(order)
        epoch = []
        for i in range(0, len(order), batch):
            idx = order[i:i + batch]
            enc = tok([texts[j] for j in idx], return_tensors="pt", padding=True, truncation=True, max_length=max_len)
            labels = enc["input_ids"].clone()
            labels[enc["attention_mask"] == 0] = -100
            for row, j in enumerate(idx):              # learn the completion, not the prompt
                labels[row, :min(prompt_lens[j], labels.shape[1])] = -100
            out = model(**enc, labels=labels)
            if not math.isfinite(out.loss.item()):
                raise RuntimeError("the loss is not finite: lower the learning rate")
            out.loss.backward()
            optim.step()
            optim.zero_grad()
            epoch.append(out.loss.item())
        losses.append(sum(epoch) / len(epoch))
    model.save_pretrained(job["out"])
    tok.save_pretrained(job["out"])
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    metrics = {"examples": len(texts), "epochs": int(job["epochs"]), "epoch_loss": [round(x, 5) for x in losses],
               "trainable_parameters": trainable, "seconds": round(time.time() - started, 1),
               "lora": bool(job.get("lora", True))}
    with open(f"{job['out']}/metrics.json", "w") as fh:
        json.dump(metrics, fh)
    return metrics


def generate(job: dict) -> dict:
    import torch

    tok, model = _load(job["base"])
    if job.get("lora", True):
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, job["adapter"])
    else:
        from transformers import AutoModelForCausalLM

        model = AutoModelForCausalLM.from_pretrained(job["adapter"], local_files_only=True)
    model.eval()
    outputs = []
    with torch.no_grad():
        for p in job["prompts"]:
            enc = tok(p, return_tensors="pt")
            gen = model.generate(**enc, max_new_tokens=int(job.get("max_new_tokens", 32)), do_sample=False,
                                 pad_token_id=tok.pad_token_id)
            outputs.append(tok.decode(gen[0][enc["input_ids"].shape[1]:], skip_special_tokens=True))
    return {"outputs": outputs}


def main() -> None:
    with open(sys.argv[1]) as fh:
        job = json.load(fh)
    out = train(job) if job["mode"] == "train" else generate(job)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
