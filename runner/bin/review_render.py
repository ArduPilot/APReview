"""Deterministic HTML projections of accepted bundles and page membership."""

import hashlib
from html import escape
from html.parser import HTMLParser
import json
import re

from review_store import atomic, digest, read

META = re.compile(rb'<meta name="apreview-digest" content="([0-9a-f]{64})">\n')
SORT = """<script>
for(const t of document.querySelectorAll('table.sortable')) {
 for(const [col,h] of [...t.tHead.rows[0].cells].entries()) {
  h.tabIndex=0; h.setAttribute('role','button');
  const sort=()=>{const reverse=h.getAttribute('aria-sort')==='ascending';
   for(const x of t.tHead.rows[0].cells)x.removeAttribute('aria-sort');
   h.setAttribute('aria-sort',reverse?'descending':'ascending');
   const c=new Intl.Collator(undefined,{numeric:true});
   const rows=[...t.tBodies[0].rows].map((r,i)=>[r,i]);
   rows.sort((a,b)=>{const key=r=>r[0].cells[col].dataset.sort??r[0].cells[col].textContent;
    const x=key(a),y=key(b);const d=x!==''&&y!==''&&Number.isFinite(+x)&&Number.isFinite(+y)?+x-+y:c.compare(x,y);
    return (reverse?-d:d)||a[1]-b[1];});
   for(const [r] of rows)t.tBodies[0].append(r);};
  h.onclick=sort;h.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();sort();}};
 }
}
</script>"""
STYLE = """<style>body{font-family:system-ui;max-width:1100px;margin:auto;padding:1em}table{border-collapse:collapse;width:100%}td,th{padding:.4em;border:1px solid #aaa;text-align:left}th{cursor:pointer}th::after{content:' ↕'}th[aria-sort=ascending]::after{content:' ▲'}th[aria-sort=descending]::after{content:' ▼'}pre{white-space:pre-wrap}section{margin-top:2em}.progress{border-left:4px solid #b80;padding:1em}</style>"""


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def anchor(inputs):
    return "pr" + inputs.get("manifest_key", str(inputs["number"])).replace("#", "-")


def markdown(text):
    """Small safe Markdown subset: escaped HTML, paragraphs, code and HTTPS links."""
    value = escape(text)
    value = re.sub(r"\[([^\]\n]+)\]\((https://[^\s)]+)\)", r'<a href="\2">\1</a>', value)
    value = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", value)
    value = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", value)
    return "".join("<p>" + p.replace("\n", "<br>") + "</p>\n" for p in value.split("\n\n"))


def table(headers, rows):
    return (
        '<table class="sortable"><thead><tr>'
        + "".join("<th>" + escape(h) + "</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>"
            + "".join(
                '<td data-sort="' + escape(str(key), quote=True) + '">' + value + "</td>"
                for key, value in row
            )
            + "</tr>"
            for row in rows
        )
        + "</tbody></table>\n"
    )


def bundle_at(store, pr, generation):
    return next((b for b in store.chain(pr) if b["generation"] == generation), None)


def balanced(inner):
    """Legacy sections sometimes miss a </div> or carry a spare one; the
    section boundary must not depend on the author's markup."""
    opened = len(re.findall(r"<div\b", inner))
    closed = len(re.findall(r"</div\s*>", inner))
    while closed > opened:
        inner = inner[: inner.rfind("</div")].rstrip()
        closed -= 1
    return inner + "</div>" * (opened - closed)


def core(bundle):
    if bundle.get("legacy"):
        raw = bundle["results"]["reconciliation"]["section_md"].strip()
        inner = re.sub(r'^<(?:section|div)\b[^>]*>\s*|\s*</(?:section|div)>$', '', raw)
        return '<p>Imported legacy review; original coverage retained.</p>\n' + balanced(inner)
    inputs, final = bundle["inputs"], bundle["results"]["reconciliation"]
    validation = bundle["results"]["validation"]
    counts = {
        name: sum(x["outcome"] == name for x in validation["outcomes"])
        for name in ("CONFIRM", "ADJUST", "REFUTE")
    }
    counts["NEW"] = len(validation["new"])
    title = escape(inputs.get("title", bundle["pr"]))
    url = f"https://github.com/{inputs['repository']}/pull/{inputs['number']}"
    out = f'<h2><a href="{url}">{escape(bundle["pr"])}: {title}</a></h2>\n'
    out += (
        "<p>Reviewed head <code>"
        + escape(inputs["head"])
        + "</code>; base <code>"
        + escape(inputs["base"])
        + "</code>; merge base <code>"
        + escape(inputs["merge_base"])
        + "</code>.</p>\n"
    )
    out += "<p>Coverage: primary review, independent cold pass, validation, reconciliation.</p>\n"
    out += table(
        ["Verdict", "Summary"],
        [[(final["verdict"], escape(final["verdict"])), ("", escape(final["summary"]))]],
    )
    out += table(
        ["Validation outcome", "Count"], [[(name, name), (n, str(n))] for name, n in counts.items()]
    )
    if final["outcomes"]:
        out += table(
            ["Finding", "Disposition", "Blocking", "Actionable"],
            [
                [
                    (x["id"], escape(x["id"])),
                    (x["disposition"], escape(x["disposition"])),
                    (int(x["blocking"]), str(x["blocking"])),
                    (int(x["actionable"]), str(x["actionable"])),
                ]
                for x in final["outcomes"]
            ],
        )
    out += markdown(final["section_md"])
    return out


def section(bundle, annotation=""):
    body = core(bundle)
    return (
        '<section id="'
        + escape(anchor(bundle["inputs"]))
        + '" data-pr="'
        + escape(bundle["pr"])
        + '" data-generation="'
        + str(bundle["generation"])
        + '" data-section-digest="'
        + sha(body.encode())
        + '">\n'
        + '<div class="review-core">\n'
        + body
        + "</div>\n"
        + annotation
        + "</section>\n"
    )


def seal(body):
    raw = (
        '<!doctype html>\n<html><head><meta charset="utf-8">\n'
        + STYLE
        + "\n</head>\n<body>\n"
        + body
        + SORT
        + "\n</body></html>\n"
    ).encode("utf-8")
    meta = b'<meta name="apreview-digest" content="' + sha(raw).encode() + b'">\n'
    return raw.replace(b"</head>", meta + b"</head>", 1)


class Sections(HTMLParser):
    """Recover each section's review-core bytes so the digest can be checked.
    Imported legacy sections nest divs, self-close tags and carry comments,
    so the capture tracks div depth and reproduces those forms verbatim."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.sections, self.current, self.capture = [], None, False
        self.parts, self.depth = [], 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and "data-pr" in attrs:
            self.current = attrs
        if not self.capture and tag == "div" and attrs.get("class") == "review-core":
            self.capture = True
            self.parts, self.depth = [], 0
        elif self.capture:
            if tag == "div":
                self.depth += 1
            self.parts.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        if self.capture:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.capture and tag == "div" and self.depth == 0:
            self.capture = False
            self.current["computed"] = sha("".join(self.parts).removeprefix("\n").encode())
        elif self.capture:
            if tag == "div":
                self.depth -= 1
            self.parts.append("</" + tag + ">")
        if tag == "section" and self.current:
            self.sections.append(self.current)
            self.current = None

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)

    def handle_comment(self, data):
        if self.capture:
            self.parts.append("<!--" + data + "-->")

    def handle_entityref(self, name):
        self.handle_data("&" + name + ";")

    def handle_charref(self, name):
        self.handle_data("&#" + name + ";")


def verify(raw, expected=()):
    matches = list(META.finditer(raw))
    if len(matches) != 1 or b"\r" in raw:
        raise OSError("missing or noncanonical page digest")
    digest_value = matches[0][1].decode()
    if sha(META.sub(b"", raw)) != digest_value:
        raise OSError("page digest mismatch")
    parser = Sections()
    parser.feed(raw.decode("utf-8"))
    sections = []
    for row in parser.sections:
        if row.get("computed") != row["data-section-digest"]:
            raise OSError("section digest mismatch")
        sections.append(
            dict(
                pr=row["data-pr"],
                generation=int(row["data-generation"]),
                digest=row["data-section-digest"],
            )
        )
    if any(item not in sections for item in expected):
        raise OSError("served section identity or digest mismatch")
    if len({r["pr"] for r in sections}) != len(sections):
        raise OSError("duplicate PR section")
    return dict(page_digest=digest_value, sections=sections)


class Renderer:
    def __init__(self, store):
        self.store = store

    def render(self, target, retained=None):
        rows = read(self.store.root / "membership" / (digest(target) + ".json"), {})
        bundles, annotations, pending = [], {}, []
        if retained:
            bundles = [retained]
        else:
            for pr, row in sorted(rows.items()):
                if row.get("removed"):
                    continue
                bundle = (
                    bundle_at(self.store, pr, row["generation"]) if row.get("generation") is not None else None
                )
                if bundle:
                    bundles.append(bundle)
                progress = row.get("progress")
                claim = self.store.claim(pr)
                if claim:
                    progress = (
                        "accepted"
                        if bundle and bundle["generation"] >= claim["generation"]
                        else claim["status"]
                    )
                annotation = ""
                ci = row.get("ci")
                if ci and bundle:
                    previous = bundle["inputs"].get("ci")
                    annotation += (
                        "<p>CI: "
                        + escape(ci["state"])
                        + " at "
                        + escape(ci["head"])
                        + ", "
                        + escape(ci["at"])
                        + (
                            " (CI changed)"
                            if ci["state"] != (previous or {}).get("state", "unknown")
                            else ""
                        )
                        + "</p>\n"
                    )
                if progress:
                    annotation += '<div class="progress">' + escape(progress) + "</div>\n"
                annotations[pr] = annotation
                if not bundle:
                    pending.append(
                        '<div class="progress">'
                        + escape(pr + ": " + (progress or "in progress; no accepted review"))
                        + "</div>\n"
                    )
        generated = max(
            (b["inputs"].get("configuration", {}).get("date", "") for b in bundles), default=""
        )
        heads = " ".join(
            b["inputs"].get("manifest_key", str(b["inputs"]["number"])) + ":" + b["inputs"]["head"]
            for b in bundles
        )
        label = target.split("/")[-2]
        body = f'<!-- reviewprs-manifest v1 label="{escape(label)}" generated="{escape(generated)}" heads="{escape(heads)}" -->\n'
        body += (
            "<h1>PR reviews</h1><p>Review date: "
            + escape(generated)
            + "; call/archive: "
            + escape(target)
            + "</p>\n"
        )
        contents = []
        for b in bundles:
            i, final = b["inputs"], b["results"]["reconciliation"]
            rank = {"ACCEPT": 0, "COMMENT": 1, "REQUEST CHANGES": 2}.get(final.get("verdict"), 3)
            ci = rows.get(b["pr"], {}).get("ci") or i.get("ci") or {"state": "unknown"}
            ci_rank = {"none": -1, "unknown": 0, "passing": 1, "pending": 2, "failing": 3}[
                ci["state"]
            ]
            contents.append(
                [
                    (
                        i["number"],
                        '<a href="#'
                        + escape(anchor(i))
                        + '">'
                        + escape(i.get("manifest_key", str(i["number"])))
                        + "</a>",
                    ),
                    (i.get("author", ""), escape(i.get("author", ""))),
                    (rank, final.get("verdict", "LEGACY")),
                    (ci_rank, escape(ci["state"])),
                ]
            )
        body += (
            table(["PR", "Author", "Verdict", "CI"], contents)
            + "<p>Headings are clickable; Enter or Space also sorts.</p>\n"
        )
        for b in bundles:
            annotation = annotations.get(b["pr"], "")
            i = b["inputs"]
            snapshot = b.get("reconciliation_snapshot", {})
            if snapshot.get("head") and snapshot["head"] != i["head"]:
                annotation += (
                    "<p>Moved during run: reviewed "
                    + escape(i["head"])
                    + "; observed "
                    + escape(snapshot["head"])
                    + ". New head is not covered.</p>\n"
                )
            receipts = (
                []
                if retained
                else [
                    read(self.store.root / "receipts" / (intent["id"] + ".json"), {})
                    for intent in b["intents"]
                    if intent["kind"] in ("comment", "note")
                ]
            )
            annotation += (
                "<p>Posting action: "
                + escape(", ".join(r.get("state", "owed") for r in receipts) or "owed")
                + "</p>\n"
            )
            for r in receipts:
                if r.get("manual_command"):
                    annotation += "<pre>" + escape(r["manual_command"]) + "</pre>\n"
            body += section(b, annotation)
        body += "".join(pending)
        totals = {
            v: sum(b["results"]["reconciliation"].get("verdict") == v for b in bundles)
            for v in ("ACCEPT", "COMMENT", "REQUEST CHANGES")
        }
        body += "<h2>Summary</h2>" + table(
            ["Verdict", "PR count"], [[(v, v), (n, str(n))] for v, n in totals.items()]
        )
        return seal(body)

    def landing(self, date, pages):
        """Caller owns the landing page region. Keep served old routes."""
        path = self.store.root / "landing" / (date + ".json")
        previous = read(path, {})
        served = {a: [] for p in pages.values() for a in p["anchors"]}
        for label, page in pages.items():
            for a in page["anchors"]:
                served[a].append(label)
        priority = {"DevCallTopic": 0, "DevCallEU": 1, "AIReview": 2}
        routes = {}
        for a, labels in served.items():
            candidates = [
                pages[label]["path"]
                for label in sorted(labels, key=lambda l: (priority.get(l, 3), l))
            ]
            routes[a] = previous[a] if previous.get(a) in candidates else candidates[0]
        for a in previous.keys() - routes.keys():
            routes[a] = None
        atomic(path, routes)
        body = (
            '<!-- reviewprs-manifest v1 heads="" -->\n<h1>Reviews for ' + escape(date) + "</h1>\n"
        )
        for label, page in sorted(pages.items()):
            body += '<p><a href="' + escape(page["path"]) + '">' + escape(label) + "</a></p>\n"
        for a, target in sorted(routes.items()):
            body += '<p id="' + escape(a) + '">'
            body += (
                ('<a href="' + escape(target + "#" + a) + '">Open ' + escape(a) + "</a>")
                if target
                else "Review unavailable: " + escape(a)
            )
            body += "</p>\n"
        mapping = json.dumps(routes, sort_keys=True).replace("<", "\\u003c")
        body += (
            "<script>const routes="
            + mapping
            + ';const a=decodeURIComponent(location.hash.slice(1));if(routes[a])location.replace(routes[a]+"#"+encodeURIComponent(a));</script>\n'
        )
        return seal(body)
