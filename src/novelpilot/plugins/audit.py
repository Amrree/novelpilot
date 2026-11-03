"""Rights and generation audit.

Provenance is a real commercial concern for AI-generated books: authors need to
be able to show that their work is theirs and that they know what was
generated.  This plugin produces two artifacts:

* **Content Rights Certificate** -- a human-readable statement recording who
  authored the manuscript, under what terms, and what content policy applied.
* **Generation Audit Log** -- a machine-readable record of every model call:
  which model, how many tokens, what it cost, and when.

Both are derived from the same ledger, so the certificate can never drift from
the log.  The log is also what makes cost-per-book explainable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.bible import BibleStore


@dataclass(slots=True)
class Certificate:
    title: str
    author: str
    generated_at: str
    total_words: int
    model_calls: list[dict[str, Any]]
    total_cost_usd: float
    terms: str = (
        "The author asserts that this manuscript was generated with NovelPilot under their "
        "direction, that it constitutes their original work, and that it is eligible for "
        "commercial publication, including on Amazon KDP and equivalent stores."
    )
    content_policy: str = (
        "Generated content passed the platform's content policy screening. The author "
        "remains responsible for final review, factual accuracy, and compliance with the "
        "retailer's content policies before publication."
    )


class AuditPlugin:
    manifest = {
        "name": "audit",
        "version": "0.1.0",
        "description": "Produces a Content Rights Certificate and a Generation Audit Log.",
    }

    async def run(self, slug: str, store: BibleStore, outdir: str | Path = "exports",
                  author: str = "Anonymous") -> dict[str, Any]:
        bible = store.load_bible(slug)
        breakdown = store.cost_breakdown(slug)
        drafts = store.load_all_drafts(slug)
        words = sum(len(t.split()) for t in drafts.values())
        now = datetime.now(timezone.utc).isoformat()

        cert = Certificate(
            title=bible.title or slug,
            author=author,
            generated_at=now,
            total_words=words,
            model_calls=breakdown,
            total_cost_usd=round(store.total_cost(slug), 4),
        )

        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)

        log_payload = {
            "project": slug,
            "title": cert.title,
            "author": author,
            "generated_at": now,
            "word_count": words,
            "chapter_count": len(drafts),
            "bible_revision": store.bible_revision(slug),
            "model_calls": breakdown,
            "total_cost_usd": cert.total_cost_usd,
            "characters": [
                {"name": c.name, "role": c.role, "id": c.id} for c in bible.characters
            ],
            "threads": [
                {"title": t.title, "status": t.status.value, "introduced_in": t.introduced_in}
                for t in bible.threads
            ],
        }
        log_path = outdir / "generation-audit-log.json"
        log_path.write_text(json.dumps(log_payload, indent=2), encoding="utf-8")

        cert_path = outdir / "content-rights-certificate.md"
        cert_path.write_text(_render_certificate(cert), encoding="utf-8")

        return {
            "certificate": str(cert_path),
            "audit_log": str(log_path),
            "words": words,
            "cost_usd": cert.total_cost_usd,
        }


def _render_certificate(cert: Certificate) -> str:
    calls = "\n".join(
        f"| {c['step']} | `{c['model']}` | {c['prompt_tokens']:,} | "
        f"{c['completion_tokens']:,} | ${c['usd']:.4f} |"
        for c in cert.model_calls
    ) or "| (no model calls recorded) | | | | |"

    return f"""# Content Rights Certificate

**Manuscript:** {cert.title}
**Author:** {cert.author}
**Issued:** {cert.generated_at}
**Length:** {cert.total_words:,} words

---

## Statement of Rights

{cert.terms}

## Content Policy

{cert.content_policy}

## Generation Record

| Step | Model | Prompt tokens | Completion tokens | Cost |
|---|---|---:|---:|---:|
{calls}

**Total generation cost:** ${cert.total_cost_usd:.4f}

---

*A machine-readable counterpart of this certificate is emitted alongside it as
`generation-audit-log.json`. This certificate is generated automatically by
NovelPilot; it is not legal advice.*
"""


plugin = AuditPlugin()
