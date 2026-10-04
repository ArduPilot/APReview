"""One bounded GitHub transport. Replay never falls back to the network.

Recordings contain public responses and request identities, never tokens.
Writes require explicit construction with writes=True; discovery cannot write.
"""

import http.client
import json
import re
import os
from pathlib import Path
import ssl
import subprocess
import threading

import review_metrics
import time
from urllib.parse import urlencode

from review_store import atomic, digest, read


class RateLimited(OSError):
    """GitHub's hourly allowance is spent; it comes back at `reset`."""

    def __init__(self, message, reset=None):
        super().__init__(message)
        self.reset = reset


class Retry(OSError):
    """A read that failed for a reason other than the request itself."""


class GitHub:
    def __init__(self, directory=None, mode="live", accounts=None, writes=False, http_cache=None):
        if mode not in ("live", "record", "replay"):
            raise ValueError("unknown GitHub transport mode")
        self.directory = Path(directory) if directory else None
        if mode != "live" and self.directory is None:
            raise ValueError("record/replay needs a directory")
        self.mode, self.accounts, self.writes = mode, accounts or {}, writes
        # REST reads over one kept-alive connection per thread with
        # conditional requests, instead of a gh process each (about 360 ms);
        # GraphQL and every write stay on gh
        self.http_cache = Path(http_cache) if http_cache else None
        self._local = threading.local()
        self._tokens = {}
        self._token_lock = threading.Lock()

    def token(self, account, env, end):
        """The account's token for github.com, as gh would use it, asked for
        once, within the request's deadline."""
        if not self._token_lock.acquire(timeout=max(0, end - time.monotonic())):
            raise TimeoutError("GitHub token busy")
        try:
            if account not in self._tokens:
                if env.get("GH_TOKEN"):
                    self._tokens[account] = env["GH_TOKEN"]
                else:
                    try:
                        out = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                                             capture_output=True, text=True, env=env,
                                             timeout=max(0.1, min(20, end - time.monotonic())))
                    except subprocess.TimeoutExpired as error:
                        raise TimeoutError("GitHub token lookup timed out") from error
                    if out.returncode or not out.stdout.strip():
                        raise OSError("no GitHub token for " + account)
                    self._tokens[account] = out.stdout.strip()
            return self._tokens[account]
        finally:
            self._token_lock.release()

    VERSION = "2022-11-28"

    def http_get(self, endpoint, account, env, timeout, text):
        """One conditional GET, finished within timeout seconds all told. An
        unchanged answer comes back as 304, which costs no rate-limit budget,
        and the cached body is returned; a cached body is never used without
        GitHub confirming it."""
        end = time.monotonic() + timeout
        accept = "application/vnd.github.raw+json" if text else "application/vnd.github+json"
        key = digest([account, env.get("GH_CONFIG_DIR"), endpoint, accept, self.VERSION])
        path = self.http_cache / key[:2] / (key + ".json")
        cached = read(path)
        headers = {"Authorization": "Bearer " + self.token(account, env, end), "Accept": accept,
                   "X-GitHub-Api-Version": self.VERSION, "User-Agent": "APReview"}
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        target = "/" + endpoint
        for _ in range(4):              # the request and up to three redirects
            response, body = self.exchange(target, headers, end)
            if response.status in (301, 302, 307, 308):
                location = response.getheader("location") or ""
                match = re.match(r"(?:https://api\.github\.com)?(/[^#]*)$", location)
                if not match:
                    raise OSError("GitHub redirect off api.github.com: " + location[:200])
                target = match[1]
                continue
            break
        else:
            raise OSError("too many GitHub redirects")
        status = response.status
        if status == 304 and cached:
            review_metrics.count("github-304", "unchanged")
            try:
                os.utime(path)
            except OSError:
                pass                    # collected meanwhile; the next read refills it
            return cached["body"]
        if status in (403, 429) and response.getheader("x-ratelimit-remaining") == "0":
            reset = response.getheader("x-ratelimit-reset")
            raise RateLimited("API rate limit exceeded (HTTP %d)" % status, float(reset) if reset else None)
        if status >= 500 or status == 429:
            raise Retry("HTTP %d" % status)
        if status >= 400:
            raise OSError("HTTP %d: %s" % (status, body[:300].decode(errors="replace")))
        if status != 200:
            raise OSError("unexpected GitHub status %d" % status)
        text_body = body.decode()
        result = text_body if text else json.loads(text_body)
        etag = response.getheader("etag")
        if etag:
            atomic(path, dict(etag=etag, body=result))
        return result

    def exchange(self, target, headers, end):
        """Send one GET on this thread's connection and read the whole answer
        before end, or fail as retryable."""
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("GitHub deadline")
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._local.connection = http.client.HTTPSConnection(
                "api.github.com", timeout=remaining, context=ssl.create_default_context())
        try:
            connection.timeout = remaining
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            chunks = []
            while True:
                if time.monotonic() >= end:
                    raise TimeoutError("GitHub answer took past its deadline")
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return response, b"".join(chunks)
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            self._local.connection = None
            if isinstance(error, TimeoutError) and time.monotonic() >= end:
                raise
            raise Retry("GitHub connection: %s" % error) from error

    def request(
        self, endpoint, *, account="read", method="GET", payload=None, deadline=None, text=False
    ):
        query = (payload or {}).get("query", "").lstrip()
        kind = review_metrics.github_class(endpoint, method, query)
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
        # A read that fails for a reason other than the request itself (a cut
        # reply, a 5xx, a dropped connection) is tried again; a write never is,
        # since it may have taken effect.
        def remaining():
            return (deadline - time.monotonic()) if deadline else 20

        for attempt in range(3 if reading else 1):
            if attempt:
                # the deadline bounds the backoff and every attempt, not just
                # the first: a caller's budget is a promise to it
                time.sleep(max(0, min(2 * attempt, remaining())))
                timeout = min(20, remaining())
                if timeout <= 0:
                    raise TimeoutError("GitHub deadline")
            started = time.monotonic()
            # only the read account: identity checks and writes for the
            # comment and project accounts go through gh, with the
            # credentials gh holds at that moment
            if (self.http_cache and method == "GET" and endpoint != "graphql" and self.mode != "replay"
                    and account == "read" and not settings):
                try:
                    response = self.http_get(endpoint, account, env, timeout, text)
                    review_metrics.count("github", kind, time.monotonic() - started)
                    review_metrics.count("github-account", account)
                    break
                except Retry as error:
                    review_metrics.count("github", kind, time.monotonic() - started)
                    if attempt == (2 if reading else 0):
                        raise OSError(str(error)) from error
                    continue
                except ValueError as error:
                    if attempt == (2 if reading else 0):
                        raise OSError("invalid GitHub JSON") from error
                    continue
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
                review_metrics.count("github", kind, time.monotonic() - started)
                review_metrics.count("github-account", account)
                if attempt < (2 if reading else 0):
                    continue
                raise TimeoutError("GitHub request timed out") from error
            review_metrics.count("github", kind, time.monotonic() - started)
            review_metrics.count("github-account", account)
            failure = None
            if result.returncode:
                failure = result.stderr.strip()[:500]
                if "rate limit exceeded" in failure.lower():
                    raise RateLimited(failure, self.rate_reset(env, deadline))
                if re.search(r"HTTP 4\d\d", failure) and "HTTP 429" not in failure:
                    raise OSError(failure)
            else:
                try:
                    response = result.stdout if text else json.loads(result.stdout)
                except ValueError:
                    failure = "invalid GitHub JSON"
            if failure is None:
                break
            if attempt == (2 if reading else 0):
                raise OSError(failure)
        if isinstance(response, dict) and response.get("errors"):
            raise OSError("GitHub returned partial data with errors")
        if self.mode == "record":
            atomic(path, dict(request=request, response=response))
        return response

    @staticmethod
    def rate_reset(env, deadline=None):
        """When the spent allowance returns; the rate endpoint itself is free."""
        timeout = min(20, (deadline - time.monotonic()) if deadline else 20)
        if timeout <= 0:
            return None
        review_metrics.count("github", "GET rate_limit")
        try:
            out = subprocess.run(["gh", "api", "rate_limit", "--jq", ".resources.core.reset"],
                                 capture_output=True, text=True, env=env, timeout=timeout)
            return float(out.stdout.strip()) if out.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

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

    THREAD = ("comment", "review_comment", "review")

    def thread(self, repo, number, kinds=THREAD, **kwargs):
        out = []
        for kind, endpoint in (
            ("comment", f"issues/{number}/comments"),
            ("review_comment", f"pulls/{number}/comments"),
            ("review", f"pulls/{number}/reviews"),
        ):
            if kind not in kinds:
                continue
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
