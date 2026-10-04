# fabric-deploy – shared Fabric TEST deployment for FreeFabricDemo Power BI projects

One reusable GitHub Actions workflow and two scripts that every Power BI project of the organisation calls to publish
its Fabric-source build to the Fabric **TEST** workspace. A new project needs no Azure or GitHub configuration.

```
project repo (main) ──push──► .github/workflows/deploy-test.yml (caller, ~15 lines)
                                └─ uses FreeFabricDemo/fabric-deploy/.github/workflows/deploy.yml@main
                                     1. azure/login – service principal, OIDC (no secret)
                                     2. scripts/load_warehouse.py – project tables in its own schema (dbo_<project>)
                                     3. scripts/deploy_test.py    – fabric-cicd publish of PowerBI/Fabric,
                                                                    bind to the cloud connection, refresh + status
```

This repository is public on purpose: it holds no secrets and no endpoints (they live in the project repositories),
and private project repositories can call a public workflow and check out its scripts without extra tokens.

## Connect a new project

1. Repository in `FreeFabricDemo` with the Fabric variant of the build in `PowerBI/Fabric` (framework skill
   `powerbi-fabric-test-cicd`). Switch it to the organisation's OIDC subject template - repositories ignore it by
   default (`use_default: true`), also new ones:

   ```bash
   gh api -X PUT repos/FreeFabricDemo/<repo>/actions/oidc/customization/sub -F use_default=false
   gh api repos/FreeFabricDemo/<repo>/actions/oidc/customization/sub   # include_claim_keys: repository_owner, job_workflow_ref
   ```
2. `Fabric/test_environment.json` (no secrets):

   ```json
   {
     "warehouse_server": "<sql endpoint>.datawarehouse.fabric.microsoft.com",
     "warehouse_database": "GSC_TEST",
     "warehouse_schema": "dbo_<project>",
     "semantic_model": "<unique model name in the workspace>",
     "snapshot_default": "yyyy-mm-dd", "compare_default": "yyyy-mm-dd", "year_default": 2026
   }
   ```

   Optional: `"connection_id"` (pin the cloud connection), `"legacy_schemas": ["dbo"]` (drop the project's tables
   from an old schema before loading).

  Projects may set `"data_source": "synapse"` and add a `"synapse"` object with `server`, `database`, `schema`, and
  the allowlisted Gold `tables`. The loader then introspects those views, recreates only those table names in the
  project's Warehouse schema, streams rows in batches, and checks destination row counts. The default remains
  `"data_source": "csv"`; existing projects are unchanged. The GitHub Actions service principal must have database
  `CONNECT` and `Storage Blob Data Reader` on the Synapse Gold account.
3. `Fabric/warehouse_schema.sql`, `Fabric/schema.json`, `Fabric/test_data/<TABLE>.csv` (the project's export script).
4. `.github/workflows/deploy-test.yml`:

   ```yaml
   name: Deploy to Fabric TEST
   on:
     push:
       branches: [main]
       paths: [PowerBI/Fabric/**, Fabric/**, .github/workflows/deploy-test.yml]
     workflow_dispatch:
       inputs:
         load_data: { description: "Reload the test data", type: boolean, default: true }
         debug: { description: "Verbose fabric-cicd log", type: boolean, default: false }
   concurrency:
     group: fabric-test-${{ github.repository }}
     cancel-in-progress: false
   jobs:
     deploy:
       uses: FreeFabricDemo/fabric-deploy/.github/workflows/deploy.yml@main
       permissions: { id-token: write, contents: read }
       with:
         load_data: ${{ github.event_name == 'push' || inputs.load_data }}
         debug: ${{ inputs.debug == true }}
       secrets: inherit
   ```

Nothing else (no Azure change): the secrets are organisation secrets, the federated credential covers every
repository of the organisation that uses the template, the cloud connection is found by `server;database`.

## Rules for a shared workspace and Warehouse

- **Own schema per project** (`dbo_<project>`), never `dbo`: a reload drops and recreates only that schema's tables.
- **Unique model / report names** in the workspace, or projects overwrite each other.
- **Never unpublish orphan items** (`unpublish_all_orphan_items`): in a shared workspace it deletes other projects.
- Changes to this repository affect every project on its next deployment - test with one project (manual run) first.

## One-time setup (done)

| Item | Setting |
|---|---|
| Organisation secrets | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `FABRIC_TEST_WORKSPACE_ID` – Actions, all repositories |
| OIDC subject template (organisation) | `gh api -X PUT orgs/FreeFabricDemo/actions/oidc/customization/sub -f 'include_claim_keys[]=repository_owner' -f 'include_claim_keys[]=job_workflow_ref'` (token scope `admin:org`) |
| Federated credential (app registration) | issuer `https://token.actions.githubusercontent.com`, subject `repository_owner:FreeFabricDemo:job_workflow_ref:FreeFabricDemo/fabric-deploy/.github/workflows/deploy.yml@refs/heads/main` |
| Workspace access | service principal = Contributor (or Member) of the TEST workspace |
| Fabric admin portal | *Service principals can use Fabric APIs* (and *Power BI APIs*) for the principal / its group |
| Cloud connection | type SQL Server, server = Warehouse endpoint, database = Warehouse, OAuth 2.0 of the owner, privacy Organizational; *Manage users* → service principal = User |

`repository_owner` in the subject keeps repositories outside the organisation out: they could call this public
workflow, but their tokens carry their own owner. Only the `main` branch of this repository is trusted
(`@refs/heads/main`); a caller that uses another ref of the workflow cannot log in.

### Cloud connection (credentials of the TEST models)

1. Fabric → ⚙ *Settings* → *Manage connections and gateways* → *+ New* → **Cloud**.
2. Type **SQL Server**, server = Warehouse SQL endpoint, database = Warehouse name.
3. Authentication **OAuth 2.0** → *Edit credentials* → sign in. Privacy level *Organizational*. *Create*.
4. *Manage users* → add the service principal with **User** permission.

The OAuth token expires when the owner's sign-in is no longer valid (password change, long inactivity) – then
*Edit credentials* again.

## Failures

| Symptom | Cause / fix |
|---|---|
| `AADSTS700213` / `AADSTS70021` at Azure login, subject `repo:FreeFabricDemo@<id>/<repo>@<id>:…` | the repository still uses the default subject - step 1 (`use_default=false`) |
| Same error, subject `repository_owner:…:job_workflow_ref:…` | the federated credential is missing or different, or the workflow was not called `@main` |
| No matching connection | connection not shared with the principal or other server / database; the script lists the visible connections; pin `connection_id` |
| Refresh fails with a credentials error | the owner's OAuth token on the connection expired → *Edit credentials* |
| `CREATE SCHEMA` denied | create the project schema once by hand in the Warehouse |

## Local run

```bash
pip install -r requirements.txt   # + ODBC Driver 18 for SQL Server; az login with access to the workspace
cd <project repo>
python3 <path>/fabric-deploy/scripts/load_warehouse.py --fabric-dir Fabric
python3 <path>/fabric-deploy/scripts/deploy_test.py --build-dir PowerBI/Fabric --fabric-dir Fabric --workspace-id <id>
```
