"""Deterministic HTML projections of accepted bundles and page membership."""

import hashlib
from html import escape, unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import unquote

from review_lock import canonical
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
STYLE = """<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
     max-width:1200px;margin:0 auto;padding:24px;line-height:1.5;color:#1b1f23;background:#fff}
h1{border-bottom:2px solid #d0d7de;padding-bottom:8px}
h2{margin-top:32px;border-bottom:1px solid #d0d7de;padding-bottom:4px}
h3{margin-bottom:4px}
code{background:#f6f8fa;padding:1px 4px;border-radius:4px;font-size:90%}
.meta{color:#57606a;font-size:90%;margin:2px 0}
.meta span{margin-right:16px}
.banner{background:#ddf4ff;border:1px solid #54aeff;border-radius:6px;padding:10px 14px;margin:14px 0}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin:12px 0}
th,td{border:1px solid #d0d7de;padding:6px 10px;text-align:left;vertical-align:top;font-size:92%}
th{background:#f6f8fa;cursor:pointer;position:relative;user-select:none;white-space:nowrap}
th:focus{outline:2px solid #0969da;outline-offset:-2px}
th::after{content:"\\2195";opacity:.35;margin-left:6px;font-size:90%}
th[aria-sort="ascending"]::after{content:"\\25B2";opacity:1}
th[aria-sort="descending"]::after{content:"\\25BC";opacity:1}
tr:nth-child(even) td{background:#fbfcfd}
.hint{color:#57606a;font-size:88%;font-style:italic;margin-top:-6px}
.pr{border:1px solid #d0d7de;border-radius:8px;padding:14px 18px;margin:20px 0;background:#fff}
.summary{background:#f6f8fa;border-left:4px solid #8c959f;padding:8px 12px;margin:10px 0}
ul.findings{margin:10px 0;padding-left:22px}
ul.findings li{margin:8px 0}
.tag{font-weight:700;font-size:80%;padding:1px 6px;border-radius:4px;color:#fff;margin-right:6px}
.f-bug .tag{background:#cf222e}
.f-issue .tag{background:#bc4c00}
.f-note .tag{background:#0969da}
.f-good .tag{background:#1a7f37}
.v-approve{color:#1a7f37;font-weight:700}
.v-comment{color:#9a6700;font-weight:700}
.v-request{color:#cf222e;font-weight:700}
.ci-pass{color:#1a7f37}
.ci-fail{color:#cf222e;font-weight:700}
.ci-none{color:#57606a}
.ci-pend{color:#9a6700}
.new{background:#dafbe1;border-left:4px solid #1a7f37}
.deferred{background:#fff8c5;border-left:4px solid #bf8700}
.draft{display:inline-block;font-weight:700;font-size:78%;padding:1px 6px;border-radius:4px;background:#f6f8fa;border:1px solid #d0d7de;color:#57606a}
.verdict{background:#f6f8fa;border-left:4px solid #8c959f;padding:8px 12px;margin:10px 0}
.src{font-style:italic}
pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px 12px;overflow-x:auto;font-size:88%}
pre code{background:none;padding:0}
.ci-note{color:#57606a;font-size:88%;font-style:italic}
.ci-updated{background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;padding:8px 12px;margin:10px 0;font-size:92%}
.f-ok .tag{background:#1a7f37}
section{display:block}
.progress{background:#fff8c5;border-left:4px solid #bf8700;padding:6px 12px;margin:8px 0}
.annot{color:#57606a;font-size:88%;margin:-12px 0 20px 4px}
</style>"""


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


VERDICT_CLASS = {"ACCEPT": "v-approve", "APPROVE": "v-approve", "COMMENT": "v-comment",
                 "REQUEST CHANGES": "v-request"}


def verdict_span(verdict):
    if not verdict:
        return "&mdash;"
    word = "APPROVE" if verdict == "ACCEPT" else verdict
    return '<span class="%s">%s</span>' % (VERDICT_CLASS.get(verdict, "v-comment"), escape(word))


CI_SPAN = {"passing": ("ci-pass", "&#10003; passing"), "failing": ("ci-fail", "&#10007; failing"),
           "pending": ("ci-pend", "&#8230; pending"), "none": ("ci-none", "no CI"),
           "unknown": ("ci-none", "unknown")}


def ci_span(ci):
    cls, text = CI_SPAN.get((ci or {}).get("state", "unknown"), CI_SPAN["unknown"])
    return '<span class="%s">%s</span>' % (cls, text)


def legacy_facts(raw):
    """Title and author as the command's own section heading states them,
    and the verdict only where its meta line names one."""
    facts = {}
    m = re.search(r"<h3>.*?&mdash;\s*(.*?)</h3>", raw, re.S)
    if m:
        facts["title"] = unescape(re.sub(r"<[^>]+>", "", m[1])).strip()
    m = re.search(r"Author:\s*<strong>(.*?)</strong>", raw)
    if m:
        facts["author"] = unescape(m[1])
    m = re.search(r"Verdict:\s*<span[^>]*>(.*?)</span>", raw)
    if m:
        word = unescape(re.sub(r"<[^>]+>", "", m[1])).strip().upper()
        facts["verdict"] = "ACCEPT" if word in ("APPROVE", "ACCEPT") else word
    return facts


def comment_receipts(store, bundle):
    """The comment and note receipts a page shows for a bundle."""
    if not bundle or bundle.get("legacy"):
        return []
    return [read(store.root / "receipts" / (intent["id"] + ".json"), {})
            for intent in bundle["intents"] if intent["kind"] in ("comment", "note")]


def row_view(row, bundle, claim, receipts, recorded):
    """Everything a page shows that depends on one membership row: whether it
    is listed, the generation, CI state and the day it was seen, the progress
    note (decided by the claim when there is one), comment links or held
    commands, and the handoff's corrections for a legacy review. Two equal
    views render the same section."""
    if not row or row.get("removed"):
        return None
    ci = row.get("ci") or {}
    progress = row.get("progress")
    if claim:
        progress = "accepted" if bundle and bundle["generation"] >= claim["generation"] else claim["status"]
    return [row.get("generation"), ci.get("state"), (ci.get("at") or "")[:10], progress,
            [[r.get("url"), r.get("manual_command")] for r in receipts],
            (recorded or {}).get(bundle["pr"]) if bundle and bundle.get("legacy") else None]


def facts(bundle, recorded=None):
    if bundle.get("legacy"):
        out = legacy_facts(bundle["results"]["reconciliation"]["section_md"])
        out.update((recorded or {}).get(bundle["pr"], {}))
        return out
    inputs = bundle["inputs"]
    return dict(title=inputs.get("title", ""), author=inputs.get("author", ""),
                verdict=bundle["results"]["reconciliation"].get("verdict"))


FINDING = {(True, True): ("f-bug", "BLOCKING"), (True, False): ("f-bug", "BLOCKING"),
           (False, True): ("f-issue", "ISSUE"), (False, False): ("f-note", "NOTE")}


def core(bundle):
    if bundle.get("legacy"):
        # the command's own card, as it was published; its id moves to the
        # enclosing section so the anchor is not duplicated
        raw = bundle["results"]["reconciliation"]["section_md"].strip()
        raw = re.sub(r'^(<(?:div|section)\b[^>]*?)\s+id="[^"]*"', r"\1", raw, count=1)
        if raw.startswith("<section"):
            raw = re.sub(r"^<section\b", '<div class="pr"', raw, count=1)
            raw = re.sub(r"</section>\s*$", "</div>", raw)
        return balanced(raw)
    inputs, final = bundle["inputs"], bundle["results"]["reconciliation"]
    url = f"https://github.com/{inputs['repository']}/pull/{inputs['number']}"
    key = inputs.get("manifest_key", str(inputs["number"]))
    label = "#" + key if key.isdecimal() else key.replace("-", "#")
    out = '<div class="pr">\n'
    out += f'<h3><a href="{url}">{escape(label)}</a> &mdash; {escape(inputs.get("title", ""))}</h3>\n'
    out += ('<p class="meta"><span>Author: <strong>' + escape(inputs.get("author", "")) + "</strong></span>"
            + "<span>Repo: <code>" + escape(inputs["repository"]) + "</code></span>"
            + "<span>Head: <code>" + escape(inputs["head"][:10]) + "</code></span>"
            + "<span>CI: " + ci_span(inputs.get("ci")) + "</span>"
            + "<span>Verdict: " + verdict_span(final.get("verdict")) + "</span></p>\n")
    date = inputs.get("configuration", {}).get("date", "")
    out += ('<p class="meta"><span><a href="' + url + '/files">diff</a></span>'
            + "<span>Reviewed by: Claude + Codex cold pass + Codex validation, reconciled by Claude</span>"
            + ("<span>Reviewed " + escape(date) + "</span>" if date else "") + "</p>\n")
    out += '<p class="summary">' + escape(final.get("summary", "")) + "</p>\n"
    findings = [x for x in final.get("outcomes", []) if x.get("disposition") != "refuted"]
    if findings:
        out += '<ul class="findings">\n'
        for x in findings:
            cls, tag = FINDING[(bool(x.get("blocking")), bool(x.get("actionable")))]
            out += ('<li class="' + cls + '"><span class="tag">' + tag + "</span> "
                    + escape(x.get("rationale", "")) + "</li>\n")
        out += "</ul>\n"
    out += markdown(final.get("section_md", ""))
    return out + "</div>\n"


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


def seal(body, title="PR reviews"):
    raw = (
        '<!doctype html>\n<html><head><meta charset="utf-8">\n'
        + '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        + "<title>" + escape(title) + "</title>\n"
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
        return seal(self.body(target, retained), self.title(target, retained))

    @staticmethod
    def title(target, retained=None):
        path = target.split("/", 1)[1] if "/" in target else target
        m = re.match(r"DevCallReviews/(?:(\d{4}[-_]\d{2}[-_]\d{2})/)?([^/]+)/devcall_pr_reviews.html$", path)
        if retained:
            key = retained["inputs"].get("manifest_key", str(retained["inputs"]["number"]))
            return "ArduPilot PR review: " + (key if not key.isdecimal() else "#" + key)
        if m and m[2] != "followups":
            return "ArduPilot %s PR reviews" % m[2]
        m = re.match(r"UserReviews/(.+)\.html$", path)
        if m:
            return "ArduPilot PR reviews \u2014 " + unquote(m[1])
        if path.startswith("RsyncReviews/"):
            return "rsync PR reviews"
        if "/followups/" in path:
            return "ArduPilot followup PR reviews"
        return "ArduPilot PR reviews"

    def body(self, target, retained=None):
        rows = read(self.store.root / "membership" / (digest(target) + ".json"), {})
        bundles, annotations, pending = [], {}, []
        # what each row put on this page, read once and used for both
        self.views, shown_receipts = {}, {}
        recorded = read(self.store.root / "legacy-facts.json", {})
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
                shown_receipts[pr] = comment_receipts(self.store, bundle)
                self.views[pr] = row_view(row, bundle, claim, shown_receipts[pr], recorded)
                annotation = ""
                ci = row.get("ci")
                if ci and bundle:
                    previous = (bundle["inputs"].get("ci") or {}).get("state", "unknown")
                    if ci["state"] != previous and not bundle.get("legacy"):
                        annotation += ('<div class="ci-updated">CI updated ' + escape(ci["at"][:10])
                                       + ": " + escape(previous) + " &rarr; " + ci_span(ci) + "</div>\n")
                if progress and progress not in ("accepted", "reuse", "reused"):
                    annotation += '<div class="progress">Review ' + escape(progress) + "</div>\n"
                annotations[pr] = annotation
                if not bundle:
                    pending.append(
                        '<div class="pr deferred"><p>' + escape(pr[3:]) + ": "
                        + escape(progress or "review in progress") + "</p></div>\n"
                    )
        dates = [b["inputs"].get("configuration", {}).get("date", "") for b in bundles if not b.get("legacy")]
        generated = max(dates, default="")
        heads = " ".join(
            b["inputs"].get("manifest_key", str(b["inputs"]["number"])) + ":" + b["inputs"]["head"]
            for b in bundles
        )
        label = target.split("/")[-2]
        body = f'<!-- reviewprs-manifest v1 label="{escape(label)}" generated="{escape(generated)}" heads="{escape(heads)}" -->\n'
        body += "<h1>" + escape(self.title(target, retained)) + "</h1>\n"
        call = re.search(r"/(\d{4})[-_](\d{2})[-_](\d{2})/", target)
        meta = []
        if generated:
            meta.append("Review date: <strong>" + escape(generated) + "</strong>")
        if call and label in ("DevCallTopic", "DevCallEU"):
            meta.append("for the <code>" + escape(label) + "</code> call on <strong>"
                        + "-".join(call.groups()) + "</strong>")
        if meta:
            body += '<p class="meta">' + " &middot; ".join(meta) + "</p>\n"
        legacy = sum(1 for b in bundles if b.get("legacy"))
        if not retained:
            body += ('<div class="banner"><b>%d PRs on this page</b>: %d reviewed by the review '
                     "pipeline, %d carried over from the earlier report%s.</div>\n" % (
                         len(bundles), len(bundles) - legacy, legacy,
                         ", %d awaiting review" % len(pending) if pending else ""))
        contents = []
        verdicts = []
        for b in bundles:
            i = b["inputs"]
            f = facts(b, recorded)
            verdicts.append(f.get("verdict"))
            key = i.get("manifest_key", str(i["number"]))
            label_pr = "#" + key if key.isdecimal() else key.replace("-", "#")
            ci = rows.get(b["pr"], {}).get("ci") or i.get("ci") or {"state": "unknown"}
            url = f"https://github.com/{i['repository']}/pull/{i['number']}"
            rank = {"ACCEPT": 0, "COMMENT": 1, "REQUEST CHANGES": 2}.get(f.get("verdict"), 3)
            ci_rank = {"passing": 0, "pending": 1, "failing": 2}.get(ci["state"], 3)
            contents.append([
                (i["number"], '<a href="#' + escape(anchor(i)) + '">' + escape(label_pr) + "</a>"),
                (i["repository"], escape(i["repository"].split("/")[1])),
                (f.get("title", ""), '<a href="' + url + '">' + escape(f.get("title", "")) + "</a>"),
                (f.get("author", ""), escape(f.get("author", ""))),
                (ci_rank, ci_span(ci)),
                (rank, verdict_span(f.get("verdict"))),
            ])
        if not retained:
            body += ("<h2>Contents</h2>\n" + '<div class="tablewrap">'
                     + table(["PR", "Repo", "Title", "Author", "CI", "Verdict"], contents) + "</div>\n"
                     + '<p class="hint">Every table on this page is click-to-sort &mdash; click a heading '
                     "to sort by it, click again to reverse.</p>\n<h2>Reviews</h2>\n")
        for b in bundles:
            annotation = annotations.get(b["pr"], "")
            i = b["inputs"]
            snapshot = b.get("reconciliation_snapshot", {})
            if snapshot.get("head") and snapshot["head"] != i["head"]:
                annotation += (
                    '<p class="annot">Moved during the review: reviewed <code>' + escape(i["head"][:10])
                    + "</code>, now <code>" + escape(snapshot["head"][:10])
                    + "</code>. The new head is not covered.</p>\n"
                )
            receipts = ([] if retained or b.get("legacy") else shown_receipts[b["pr"]]
                        if b["pr"] in shown_receipts else comment_receipts(self.store, b))
            for r in receipts:
                if r.get("url"):
                    annotation += '<p class="annot">Comment: <a href="' + escape(r["url"]) + '">posted</a></p>\n'
                elif r.get("manual_command"):
                    annotation += '<p class="annot">Comment held for a human:</p><pre>' + escape(r["manual_command"]) + "</pre>\n"
            body += section(b, annotation)
        body += "".join(pending)
        if not retained:
            totals = {v: sum(x == v for x in verdicts) for v in ("ACCEPT", "COMMENT", "REQUEST CHANGES")}
            body += "<h2>Summary</h2>\n" + table(
                ["Verdict", "PRs"], [[(v, verdict_span(v)), (n, str(n))] for v, n in totals.items()]
            )
        return body

    def landing(self, date, pages, commit=True):
        """Caller owns the landing page region. Keep served old routes. With
        commit false the routes are left in self.pending for the caller to
        save once the page is really published; a probe render saves nothing."""
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
        # The dated page is the call's report itself, as the command's was:
        # the highest-priority label's full page, with the others linked and
        # their anchors routed.
        top = min(pages, key=lambda l: (priority.get(l, 3), l)) if pages else None
        nav = "".join(
            '<p>Also for ' + escape(date) + ': <a href="' + escape(page["path"]) + '">'
            + escape(label) + "</a></p>\n"
            for label, page in sorted(pages.items()) if label != top
        )
        if top and pages[top].get("target"):
            body = self.body(pages[top]["target"])
            head = body.index("</p>\n", body.index("<h1>")) + len("</p>\n")
            body = body[:head] + nav + body[head:]
            # the sections this body really has: the label's receipt was read
            # earlier, and its membership may have changed since
            local = set(re.findall(r'<section id="([^"]+)"', body))
            # a section served here is a route to keep even before a label
            # receipt records it
            for a in local:
                if routes.get(a) is None:
                    routes[a] = pages[top]["path"]
        else:
            body = (
                '<!-- reviewprs-manifest v1 heads="" -->\n<h1>Reviews for ' + escape(date) + "</h1>\n"
            )
            for label, page in sorted(pages.items()):
                body += '<p><a href="' + escape(page["path"]) + '">' + escape(label) + "</a></p>\n"
            local = set()
        for a, target in sorted(routes.items()):
            if a in local:
                continue
            body += '<p id="' + escape(a) + '">'
            body += (
                ('<a href="' + escape(target + "#" + a) + '">Open ' + escape(a) + "</a>")
                if target
                else "Review unavailable: " + escape(a)
            )
            body += "</p>\n"
        # the saved history keeps every route; only the redirect skips the
        # sections this page serves itself
        redirect = {a: t for a, t in routes.items() if a not in local}
        mapping = json.dumps(redirect, sort_keys=True).replace("<", "\\u003c")
        body += (
            "<script>const routes="
            + mapping
            + ';const a=decodeURIComponent(location.hash.slice(1));if(routes[a])location.replace(routes[a]+"#"+encodeURIComponent(a));</script>\n'
        )
        self.pending = (path, routes)
        if commit:
            atomic(path, routes)
        return seal(body)


def served_copy(store, target):
    """The bytes of a page as its last confirmed upload left it, read from the
    store, or None: the local source must match the confirmed digest, so a
    later failed or unfinished upload never passes for what is served."""
    directory = store.root / "pages" / digest(canonical(target))
    confirmed = read(directory / "confirmed.json")
    if not confirmed:
        return None
    try:
        raw = (directory / canonical(target).rsplit("/", 1)[1]).read_bytes()
    except OSError:
        return None
    try:
        return raw if verify(raw)["page_digest"] == confirmed.get("page_digest") else None
    except (OSError, ValueError, KeyError):
        return None
