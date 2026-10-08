"""Deploy a project's Fabric-source build (semantic model + report) to the Fabric TEST workspace and refresh the model.

Shared by all projects of the organisation (FreeFabricDemo/fabric-deploy, called by the reusable workflow).
Settings of the project: <fabric-dir>/test_environment.json (default ./Fabric of the calling repository).
Publishes only the items of the build folder; never unpublishes other items (the workspace is shared).

Steps:
  1. publish the items of the build folder with fabric-cicd (SemanticModel, Report),
  2. bind the model's Warehouse data source to the shareable cloud connection (OAuth2 of the owner, shared with the
     service principal - no secret in GitHub),
  3. start a refresh of the semantic model (Power BI REST API) and wait for the result,
  4. optional "dax_tests" in test_environment.json: a JSON file of the project (relative to <fabric-dir>) with
     [{"name": ..., "query": "EVALUATE ..."}]; every query runs twice through the executeQueries REST API
     (cold / warm cache) and the timings are printed. A failing query fails the run, a slow one does not.

Authentication: DefaultAzureCredential (GitHub Actions: service principal via OIDC / azure/login; locally `az login`).

Usage:  python3 scripts/deploy_test.py --build-dir PowerBI/Fabric [--fabric-dir Fabric] --workspace-id <id> [--no-refresh]
"""
import argparse
import json
import os
import re
import sys
import time

import requests
from azure.identity import DefaultAzureCredential
import fabric_cicd
from fabric_cicd import FabricWorkspace, publish_all_items

PBI = "https://api.powerbi.com/v1.0/myorg"
FABRIC = "https://api.fabric.microsoft.com/v1"


def norm(path):
    """server;database -> lower case, without the port and spaces."""
    server, _, database = path.partition(";")
    return server.strip().lower().split(",")[0] + ";" + database.strip().lower()


def bind_connection(credential, headers, workspace_id, ds, connection_id=None):
    """Bind the model's Warehouse data source to a shareable cloud connection (OAuth2, created once by the owner and
    shared with the service principal). Found by matching server;database, or given in test_environment.json."""
    sources = requests.get(f"{PBI}/groups/{workspace_id}/datasets/{ds['id']}/datasources", headers=headers, timeout=60)
    sources.raise_for_status()
    fab = {"Authorization": "Bearer " + credential.get_token("https://api.fabric.microsoft.com/.default").token}
    conns = requests.get(f"{FABRIC}/connections", headers=fab, timeout=60)
    conns.raise_for_status()
    conns = conns.json().get("value", [])
    if connection_id and not any(c["id"] == connection_id for c in conns):
        one = requests.get(f"{FABRIC}/connections/{connection_id}", headers=fab, timeout=60)
        print(f"GET connection {connection_id}: HTTP {one.status_code}")
        if one.ok:
            conns.append(one.json())
        else:
            print(f"  {one.text[:300]}")
    for src in sources.json()["value"]:
        cd = src.get("connectionDetails", {})
        path = f"{cd.get('server', '')};{cd.get('database', '')}"
        match = [c for c in conns if c["id"] == connection_id] if connection_id else \
                [c for c in conns if c.get("connectivityType") == "ShareableCloud"
                 and norm(c.get("connectionDetails", {}).get("path", "")) == norm(path)]
        if not match:
            print(f"Connections visible to the deploying identity ({len(conns)}):")
            for c in conns:
                print(f"  {c.get('displayName')} | {c.get('connectivityType')} | "
                      f"{c.get('connectionDetails', {}).get('type')} | {c.get('connectionDetails', {}).get('path')}")
            sys.exit(f"no shareable cloud connection for {path} visible to the deploying identity.\n"
                     "Create it once (README of FreeFabricDemo/fabric-deploy, 'Cloud connection') and add the service principal "
                     "as a user of the connection.")
        conn = match[0]
        body = {"connectionBinding": {"id": conn["id"], "connectivityType": "ShareableCloud",
                                      "connectionDetails": {"type": conn["connectionDetails"]["type"],
                                                            "path": conn["connectionDetails"]["path"]}}}
        r = requests.post(f"{FABRIC}/workspaces/{workspace_id}/semanticModels/{ds['id']}/bindConnection",
                          headers=fab, json=body, timeout=60)
        if r.status_code >= 300:
            sys.exit(f"bindConnection failed {r.status_code}: {r.text[:500]}")
        print(f"Bound {path} to connection '{conn.get('displayName')}' ({conn['id']})")


def refresh(credential, workspace_id, model_name, connection_id=None, timeout_s=1800):
    token = credential.get_token("https://analysis.windows.net/powerbi/api/.default").token
    headers = {"Authorization": f"Bearer {token}"}
    datasets = requests.get(f"{PBI}/groups/{workspace_id}/datasets", headers=headers, timeout=60)
    datasets.raise_for_status()
    ds = next((d for d in datasets.json()["value"] if d["name"] == model_name), None)
    if not ds:
        sys.exit(f"semantic model {model_name} not found in the workspace")
    if ds.get("configuredBy"):
        print(f"Model owner: {ds['configuredBy']}")
    bind_connection(credential, headers, workspace_id, ds, connection_id)
    r = requests.post(f"{PBI}/groups/{workspace_id}/datasets/{ds['id']}/refreshes", headers=headers,
                      json={"notifyOption": "NoNotification"}, timeout=60)
    if r.status_code not in (200, 202):
        sys.exit(f"refresh request failed {r.status_code}: {r.text}")
    print("Refresh started")
    start = time.time()
    while time.time() - start < timeout_s:
        time.sleep(20)
        h = requests.get(f"{PBI}/groups/{workspace_id}/datasets/{ds['id']}/refreshes?$top=1", headers=headers, timeout=60)
        h.raise_for_status()
        last = h.json()["value"][0]
        status = last.get("status")
        print(f"  refresh status: {status}")
        if status == "Completed":
            return
        if status in ("Failed", "Disabled", "Cancelled"):
            sys.exit("refresh failed: " + json.dumps(last.get("serviceExceptionJson") or last, indent=1))
    sys.exit("refresh did not finish in time")


def dataset_id(headers, workspace_id, model_name):
    datasets = requests.get(f"{PBI}/groups/{workspace_id}/datasets", headers=headers, timeout=60)
    datasets.raise_for_status()
    ds = next((d for d in datasets.json()["value"] if d["name"] == model_name), None)
    if not ds:
        sys.exit(f"semantic model {model_name} not found in the workspace")
    return ds["id"]


def run_dax_tests(credential, workspace_id, model_name, tests_path):
    """Run the project's DAX test queries (executeQueries REST API) twice each and print cold / warm timings."""
    tests = json.load(open(tests_path, encoding="utf-8"))
    token = credential.get_token("https://analysis.windows.net/powerbi/api/.default").token
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{PBI}/groups/{workspace_id}/datasets/{dataset_id(headers, workspace_id, model_name)}/executeQueries"
    failed = []
    print(f"DAX tests ({len(tests)}): cold s / warm s / rows")
    for t in tests:
        timings, rows, error = [], None, None
        for _ in range(2):
            start = time.time()
            r = requests.post(url, headers=headers, timeout=600, json={
                "queries": [{"query": t["query"]}], "serializerSettings": {"includeNulls": True}})
            timings.append(time.time() - start)
            body = r.json() if r.content else {}
            result = (body.get("results") or [{}])[0]
            if not r.ok or result.get("error"):
                error = json.dumps(result.get("error") or body.get("error") or body)[:600]
                break
            rows = len(result["tables"][0]["rows"])
        timing = " / ".join(f"{s:6.1f}" for s in timings)
        if error:
            failed.append(t["name"])
            print(f"  FAIL {timing}  {t['name']}: {error}")
        else:
            print(f"  ok   {timing} / {rows:>7}  {t['name']}")
    if failed:
        sys.exit(f"DAX tests failed: {', '.join(failed)}")


def show_target(credential, workspace_id):
    """Print the workspace name and the links of the published items (where to find the report)."""
    fab = {"Authorization": "Bearer " + credential.get_token("https://api.fabric.microsoft.com/.default").token}
    w = requests.get(f"{FABRIC}/workspaces/{workspace_id}", headers=fab, timeout=60)
    print(f"Workspace: {w.json().get('displayName') if w.ok else w.status_code}")
    items = requests.get(f"{FABRIC}/workspaces/{workspace_id}/items", headers=fab, timeout=60)
    for i in items.json().get("value", []) if items.ok else []:
        kind = {"Report": "reports", "SemanticModel": "datasets"}.get(i["type"])
        if kind:
            print(f"  {i['type']} {i['displayName']}: https://app.powerbi.com/groups/{workspace_id}/{kind}/{i['id']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-dir", required=True)
    ap.add_argument("--fabric-dir", default="Fabric", help="folder with test_environment.json")
    ap.add_argument("--workspace-id", default=os.environ.get("FABRIC_TEST_WORKSPACE_ID"))
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--debug", action="store_true", help="fabric-cicd debug log (API calls)")
    a = ap.parse_args()
    env = json.load(open(os.path.join(a.fabric_dir, "test_environment.json"), encoding="utf-8"))
    if a.debug or os.environ.get("RUNNER_DEBUG") == "1":
        fabric_cicd.change_log_level("DEBUG")
    # the secret may hold the id with whitespace or the whole workspace URL (.../groups/<id>/...) - keep the GUID
    m = re.search(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", a.workspace_id or "")
    if not m:
        sys.exit("workspace id missing or not a GUID (--workspace-id or FABRIC_TEST_WORKSPACE_ID)")
    if m.group(0) != a.workspace_id:
        print("note: workspace id taken from a longer value - set the secret to the bare GUID")
    a.workspace_id = m.group(0).lower()
    credential = DefaultAzureCredential()
    ws = FabricWorkspace(workspace_id=a.workspace_id, repository_directory=os.path.abspath(a.build_dir),
                         item_type_in_scope=["SemanticModel", "Report"], token_credential=credential)
    publish_all_items(ws)
    print("Published SemanticModel + Report")
    show_target(credential, a.workspace_id)
    if not a.no_refresh:
        refresh(credential, a.workspace_id, env["semantic_model"], env.get("connection_id") or None)
        print("Refresh completed")
        if env.get("dax_tests"):
            run_dax_tests(credential, a.workspace_id, env["semantic_model"], os.path.join(a.fabric_dir, env["dax_tests"]))


if __name__ == "__main__":
    main()
