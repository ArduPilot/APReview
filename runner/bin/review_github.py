"""One bounded GitHub transport. Replay never falls back to the network.

Recordings contain public responses and request identities, never tokens.
Writes require explicit construction with writes=True; discovery cannot write.
"""

import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode

from review_store import atomic, digest, read


class GitHub:
    def __init__(self, directory=None, mode="live", accounts=None, writes=False):
        if mode not in ("live", "record", "replay"):
            raise ValueError("unknown GitHub transport mode")
        self.directory = Path(directory) if directory else None
        if mode != "live" and self.directory is None:
            raise ValueError("record/replay needs a directory")
        self.mode, self.accounts, self.writes = mode, accounts or {}, writes

    def request(
        self, endpoint, *, account="read", method="GET", payload=None, deadline=None, text=False
    ):
        query = (payload or {}).get("query", "").lstrip()
        reading = method == "GET" or (endpoint == "graphql" and query.startswith("query"))
        if not reading and not self.writes:
            raise PermissionError("GitHub writes disabled at adapter boundary")
        request = dict(
            endpoint=endpoint, account=account, method=method, payload=payload, text=text
        )
        path = self.directory / (digest(request) + ".json") if self.directory else None
        if self.mode == "replay":
            record = read(path)
            if record is None or record["request"] != request:
                raise OSError("missing replay request: " + json.dumps(request))
            return record["response"]
        timeout = min(20, (deadline - time.monotonic()) if deadline else 20)
        if timeout <= 0:
            raise TimeoutError("GitHub deadline")
        env = dict(os.environ)
        settings = self.accounts.get(account, {})
        if account == "project" or settings.get("project"):
            env.pop("GH_TOKEN", None)
            env.pop("GITHUB_TOKEN", None)
        if settings.get("config_dir"):
            env.pop("GH_TOKEN", None)
            env.pop("GITHUB_TOKEN", None)
            env["GH_CONFIG_DIR"] = settings["config_dir"]
        token = settings.get("token_env")
        if token:
            if not os.environ.get(token):
                raise OSError("missing credential: " + account)
            env["GH_TOKEN"] = os.environ[token]
        command = ["gh", "api", endpoint, "--method", method]
        if text:
            command += ["-H", "Accept: application/vnd.github.raw+json"]
        if payload is not None:
            command += ["--input", "-"]
        try:
            result = subprocess.run(
                command,
                input=json.dumps(payload) if payload else None,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("GitHub request timed out") from error
        if result.returncode:
            raise OSError(result.stderr.strip()[:500])
        try:
            response = result.stdout if text else json.loads(result.stdout)
        except ValueError as error:
            raise OSError("invalid GitHub JSON") from error
        if isinstance(response, dict) and response.get("errors"):
            raise OSError("GitHub returned partial data with errors")
        if self.mode == "record":
            atomic(path, dict(request=request, response=response))
        return response

    def pages(self, endpoint, *, field=None, account="read", deadline=None):
        out, seen = [], set()
        for page in range(1, 101):
            separator = "&" if "?" in endpoint else "?"
            data = self.request(
                endpoint + separator + urlencode(dict(per_page=100, page=page)),
                account=account,
                deadline=deadline,
            )
            items = data[field] if field else data
            if not isinstance(items, list):
                raise OSError("expected GitHub page array")
            signature = digest(items)
            if items and signature in seen:
                raise OSError("GitHub pagination cycle")
            seen.add(signature)
            out.extend(items)
            if len(items) < 100:
                if field and (
                    data.get("incomplete_results") or data.get("total_count", 0) > len(out)
                ):
                    raise OSError("truncated GitHub search")
                return out
        raise OSError("GitHub pagination bound exceeded")

    def thread(self, repo, number, **kwargs):
        out = []
        for kind, endpoint in (
            ("comment", f"issues/{number}/comments"),
            ("review_comment", f"pulls/{number}/comments"),
            ("review", f"pulls/{number}/reviews"),
        ):
            for c in self.pages(f"repos/{repo}/{endpoint}", **kwargs):
                out.append(
                    dict(
                        kind=kind,
                        id=c["id"],
                        login=(c.get("user") or {}).get("login"),
                        at=c.get("submitted_at") or c.get("created_at"),
                        body=c.get("body") or "",
                        url=c.get("html_url"),
                    )
                )
        return out

    def graphql(self, query, account="project", deadline=None, **variables):
        return self.request(
            "graphql",
            method="POST",
            account=account,
            deadline=deadline,
            payload=dict(query=query, variables=variables),
        )["data"]
