"""Minimal Salesforce REST client (stdlib only): SOQL query + composite sObject collections."""
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request


class SalesforceError(RuntimeError):
    pass


def soql_str(v):
    """Quote a value for a SOQL string literal."""
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def in_list(vals):
    return ",".join(soql_str(v) for v in vals)


def chunks(xs, n):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


class Salesforce:
    def __init__(self, api_version="v60.0", target_org=None):
        self.api_version = api_version
        self.target_org = target_org or os.environ.get("SF_TARGET_ORG")
        self.instance_url, self.token = "", ""
        self._auth()

    def _auth(self):
        url, tok = os.environ.get("SF_INSTANCE_URL"), os.environ.get("SF_ACCESS_TOKEN")
        if url and tok:
            self.instance_url, self.token, self._from_env = url.rstrip("/"), tok, True
            return
        self._from_env = False
        cmd = ["sf", "org", "display", "--json"] + (["--target-org", self.target_org] if self.target_org else [])
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            raise SalesforceError("Set SF_INSTANCE_URL and SF_ACCESS_TOKEN, or install the sf CLI and log in.")
        if p.returncode != 0:
            raise SalesforceError("`sf org display` failed: " + p.stderr[:300])
        r = json.loads(p.stdout)["result"]
        self.instance_url, self.token = r["instanceUrl"].rstrip("/"), r["accessToken"]

    def _request(self, method, path, body=None):
        url = "{}/services/data/{}/{}".format(self.instance_url, self.api_version, path)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(2):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read() or "null")
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0 and not self._from_env:
                    self._auth()
                    continue
                raise SalesforceError("HTTP {} {}: {}".format(e.code, path[:60], e.read().decode()[:300]))

    def query(self, soql):
        res = self._request("GET", "query/?q=" + urllib.parse.quote(soql))
        recs = res.get("records", [])
        while res.get("nextRecordsUrl"):
            res = self._request("GET", res["nextRecordsUrl"].split("/services/data/{}/".format(self.api_version))[1])
            recs.extend(res.get("records", []))
        return recs

    def composite(self, method, records, all_or_none=False):
        """POST (insert) or PATCH (update) up to 200 records. Returns one result per record.
        A whole-request failure is reported as a failed result for every record."""
        if len(records) > 200:
            raise ValueError("composite/sobjects accepts at most 200 records")
        try:
            return self._request(method, "composite/sobjects", {"allOrNone": all_or_none, "records": records})
        except SalesforceError as e:
            return [{"success": False, "errors": [{"statusCode": "REQUEST_FAILED", "message": str(e)}]} for _ in records]
