import base64
import json
import textwrap
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import viktor as vkt
from aps_automation_sdk.acc import parent_folder_from_item
from aps_automation_sdk.classes import (
    ActivityInputParameterAcc,
    ActivityJsonParameter,
    ActivityOutputParameterAcc,
    WorkItemAcc,
)
from dotenv import load_dotenv

from app.helpers import (
    DEFAULT_REVIT_VERSION,
    fetch_manifest,
    get_revit_version_from_manifest,
    get_type_parameters_signature,
)


DX_GRAPHQL_URL = "https://developer.api.autodesk.com/dataexchange/2023-05/graphql"
DA_V3 = "https://developer.api.autodesk.com/da/us-east/v3"


EXCHANGE_BY_FILE_URN_QUERY = """
query GetExchangeByFileUrn($externalProjectId: ID!, $fileUrn: ID!) {
    exchangeByFileUrn(externalProjectId: $externalProjectId, fileUrn: $fileUrn) {
        id
        name
    }
}
"""


ELEMENTS_WITH_PROPS_QUERY = """
query ElementsWithProps($exchangeId: ID!, $pagination: PaginationInput) {
    exchange(exchangeId: $exchangeId) {
        elements(pagination: $pagination) {
            pagination {
                cursor
                pageSize
            }
            results {
                id
                properties(filter: { names: ["Family Name", "Element Name"] }) {
                    results {
                        name
                        value
                    }
                }
                alternativeIdentifiers {
                    externalElementId
                }
            }
        }
    }
}
"""


load_dotenv()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def get_workitem_status(wi_id: str, token: str) -> Dict[str, Any]:
    url = f"{DA_V3}/workitems/{wi_id}"
    resp = requests.get(url, headers=bearer(token), timeout=30)
    resp.raise_for_status()
    return resp.json()


def create_type_params_json(params) -> List[Dict[str, Any]]:
    """Group assignments by parameter + group into DA config payload."""

    rows = getattr(getattr(params, "assignments_section", None), "assignments", None) or []
    grouped: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)

    for row in rows:
        param_name = row.get("parameter")
        if not param_name:
            continue
        param_group = row.get("parameter_group") or "PG_DATA"
        target = {
            "TypeName": row.get("type_name") or "",
            "FamilyName": row.get("family") or "",
            "Value": row.get("parameter_value") or "",
        }
        grouped[(param_name, param_group)].append(target)

    payload: List[Dict[str, Any]] = []
    for (name, group), targets in grouped.items():
        if not name:
            continue
        if not targets:
            continue
        payload.append(
            {
                "ParameterName": name,
                "ParameterGroup": group,
                "Targets": targets,
            }
        )

    return payload

def execute_graphql(query: str, *, token: str, region: str, variables: Optional[dict] = None, timeout: int = 30) -> dict:
    """Execute a GraphQL query against the Data Exchange API."""

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-ads-region": region,
    }
    payload = {"query": query, "variables": variables or {}}
    response = requests.post(DX_GRAPHQL_URL, headers=headers, json=payload, timeout=timeout)

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")

    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {body['errors']}")

    return body.get("data", {})


def extract_props(prop_results: Optional[dict]) -> Dict[str, Optional[str]]:
    """Convert GraphQL property results into a flat dict of name -> value."""

    properties = {}
    for prop in (prop_results or {}).get("results") or []:
        name = prop.get("name")
        value = prop.get("value")
        if name:
            properties[name] = value if value is None else str(value)
    return properties


ELEMENT_CACHE: Dict[str, List[dict]] = {}


def fetch_elements_catalog(*, exchange_id: str, token: str, region: str, page_size: int = 200) -> List[dict]:
    """Fetch all elements once, capturing family, type, and external IDs."""

    catalog: List[dict] = []
    cursor: Optional[str] = None
    page = 1

    while True:
        vkt.progress_message(message=f"Fetching elements (page {page})")
        variables = {
            "exchangeId": exchange_id,
            "pagination": {"limit": page_size, "cursor": cursor},
        }
        data = execute_graphql(ELEMENTS_WITH_PROPS_QUERY, token=token, region=region, variables=variables)
        exchange_block = data.get("exchange") or {}
        elements_block = exchange_block.get("elements") or {}
        results = elements_block.get("results") or []

        for element in results:
            props = extract_props(element.get("properties"))
            family_name = props.get("Family Name")
            element_name = props.get("Element Name")

            alt_ids = element.get("alternativeIdentifiers") or {}
            ext_id = alt_ids.get("externalElementId")
            if ext_id:
                ext_id = str(ext_id)
                print(f"externalElementId found: {ext_id}")

            catalog.append(
                {
                    "family_name": family_name,
                    "element_name": element_name,
                    "external_id": ext_id,
                }
            )

        pagination = elements_block.get("pagination") or {}
        cursor = pagination.get("cursor")
        if not cursor:
            break

        page += 1
        print(f"Processing Page: {page}")
        vkt.progress_message(message=f'Processing page:{page}')

    return catalog


@vkt.memoize
def get_elements_catalog(*, exchange_id: str, token: str, region: str) -> List[dict]:
    """Return cached catalog of elements for the exchange id."""

    if exchange_id in ELEMENT_CACHE:
        return ELEMENT_CACHE[exchange_id]

    catalog = fetch_elements_catalog(exchange_id=exchange_id, token=token, region=region)
    ELEMENT_CACHE[exchange_id] = catalog
    return catalog


def fetch_exchange_id(*, project_id: str, file_urn: str, token: str, region: str) -> str:
    """Resolve exchange id via exchangeByFileUrn."""

    variables = {"externalProjectId": project_id, "fileUrn": file_urn}
    data = execute_graphql(EXCHANGE_BY_FILE_URN_QUERY, token=token, region=region, variables=variables)
    exchange = (data or {}).get("exchangeByFileUrn") or {}
    exch_id = exchange.get("id")
    if not exch_id:
        raise vkt.UserError("No exchange id found for the selected file/project.")
    return exch_id


def get_model_info(params, **kwargs) -> Optional[dict]:
    """Return token, region, exchange id, viewer URN, and file if an Autodesk file is selected."""

    autodesk_file = getattr(getattr(params, "model", None), "autodesk_file", None)
    if not autodesk_file:
        return None

    integration = vkt.external.OAuth2Integration("aps-integration-automation-v2")
    token = integration.get_access_token()
    region = autodesk_file.get_region(token)
    version = autodesk_file.get_latest_version(token)
    viewer_urn_bs64 = _encode_urn(version.urn)

    project_id = getattr(autodesk_file, "project_id", None)
    if not project_id:
        raise vkt.UserError("Project id is missing on the selected Autodesk file.")

    file_urn = getattr(autodesk_file, "urn", None)
    if not file_urn:
        raise vkt.UserError("URN is missing on the selected Autodesk file.")

    exchange_id = fetch_exchange_id(project_id=project_id, file_urn=file_urn, token=token, region=region)

    model_name = getattr(autodesk_file, "name", None) or "selected model"
    vkt.UserMessage.info(
        f"Fetching Autodesk model info for {model_name} (region: {region})."
    )

    return {
        "token": token,
        "region": region,
        "exchange_id": exchange_id,
        "urn_bs64": viewer_urn_bs64,
        "file": autodesk_file,
    }


@vkt.memoize
def get_family_list(*, exchange_id: str, token: str, region: str) -> List[str]:
    """Return sorted distinct family names from the cached catalog."""

    catalog = get_elements_catalog(exchange_id=exchange_id, token=token, region=region)
    families = {row.get("family_name") for row in catalog if row.get("family_name")}
    return sorted(families)


@vkt.memoize
def get_types_for_family(*, exchange_id: str, token: str, region: str, family_name: str) -> List[str]:
    """Return sorted distinct element names for a given family from the cached catalog."""

    catalog = get_elements_catalog(exchange_id=exchange_id, token=token, region=region)
    types = {
        row.get("element_name")
        for row in catalog
        if row.get("element_name") and row.get("family_name") == family_name
    }
    return sorted(types)


def get_family_options(params, **kwargs):
    info = get_model_info(params, **kwargs)
    if not info:
        return []

    try:
        return get_family_list(
            exchange_id=info["exchange_id"],
            token=info["token"],
            region=info["region"],
        )
    except Exception:
        return []


def get_type_options(params, **kwargs):
    assignments = getattr(getattr(params, "assignments_section", None), "assignments", None) or []
    if not assignments:
        return []

    info = get_model_info(params, **kwargs)
    if not info:
        return [[] for _ in assignments]

    options: List[List[str]] = []
    for row in assignments:
        family = row.get("family")
        if not family:
            options.append([])
            continue
        try:
            types = get_types_for_family(
                exchange_id=info["exchange_id"],
                token=info["token"],
                region=info["region"],
                family_name=family,
            )
            options.append(types)
        except Exception:
            options.append([])
    return options


def get_parameter_options(params, **kwargs):
    rows = getattr(getattr(params, "parameter_section", None), "parameter_table", None) or []
    parameter_names = [row.get("parameter_name") for row in rows if row.get("parameter_name")]
    return sorted(set(parameter_names)) if parameter_names else []


class Parametrization(vkt.Parametrization):
    model = vkt.Section("Autodesk Model")
    model.intro = vkt.Text(
        textwrap.dedent(
            """\
            # Revit Type Parameter Assigner

            Select a model, then map family types to parameter names and values for assignment.
            """
        )
    )
    model.autodesk_file = vkt.AutodeskFileField(
        "Revit model",
        oauth2_integration="aps-integration-automation-v2",
    )

    parameter_section = vkt.Section("Parameter Table")
    parameter_section.title = vkt.Text(
        textwrap.dedent(
            """\
            ## Parameter to Assign
            Define the parameter names you want to target and whether they should be visualized.
            """
        )
    )
    parameter_section.parameter_table = vkt.Table("Parameter to Assign")
    parameter_section.parameter_table.parameter_name = vkt.TextField("Parameter Name")
    parameter_section.parameter_table.visualize = vkt.BooleanField("Visualize") 

    assignments_section = vkt.Section("Assignments")
    assignments_section.title = vkt.Text(
        textwrap.dedent(
            """\
            ## Family/Type Parameter Values
            Pick a family, its types, choose a parameter from the table, and provide the value and color.
            """
        )
    )
    assignments_section.assignments = vkt.DynamicArray("Assignments", default=[{}], copylast=True)
    assignments_section.assignments.family = vkt.AutocompleteField(
        "Family Name", options=get_family_options
    )
    assignments_section.assignments.type_name = vkt.OptionField(
        "Type (Element Name)", options=get_type_options, autoselect_single_option=True
    )
    assignments_section.assignments.parameter = vkt.OptionField(
        "Parameter Name", options=get_parameter_options, autoselect_single_option=True
    )
    assignments_section.assignments.parameter_group = vkt.OptionField(
        "Parameter Group",
        options=["PG_TEXT", "PG_DATA", "PG_IDENTITY_DATA", "PG_GEOMETRY"],
        autoselect_single_option=True,
    )
    assignments_section.assignments.parameter_value = vkt.TextField("Parameter Value")
    assignments_section.assignments.color = vkt.ColorField(
        "Color", default=vkt.Color(0, 153, 255)
    )

    run_automation = vkt.Section("Run Automation")
    run_automation.title = vkt.Text(
        textwrap.dedent(
            """\
            ## Run Automation
            Select a model and trigger the automation workflow (coming soon).
            """
        )
    )
    run_automation.autodesk_file = vkt.AutodeskFileField(
        "Revit model",
        oauth2_integration="aps-integration-automation-v2",
    )
    run_automation.break_line = vkt.LineBreak()
    run_automation.trigger = vkt.ActionButton("Run Automation", method="trigger_run_automation")


def _encode_urn(raw_urn: str) -> str:
    """Return urlsafe base64 encoding (keep padding) for Forge URNs."""

    return base64.urlsafe_b64encode(raw_urn.encode()).decode()


def _color_to_hex(color: Optional[vkt.Color]) -> str:
    """Return hex string for viktor Color, fallback to blue if None."""

    return color.hex if color else "#0099ff"


class Controller(vkt.Controller):
    parametrization = Parametrization

    @vkt.WebView("Autodesk Model", duration_guess=30)
    def autodesk_view(self, params, **kwargs):
        info = get_model_info(params, **kwargs)
        if not info:
            raise vkt.UserError("Select a model in the Autodesk file field first")

        assignments = getattr(getattr(params, "assignments_section", None), "assignments", None) or []
        params_rows = getattr(getattr(params, "parameter_section", None), "parameter_table", None) or []
        vkt.UserMessage.info(
            f"Launching viewer with {len(params_rows)} parameter rows and {len(assignments)} assignment rows."
        )

        urn_bs64 = info.get("urn_bs64") or info.get("exchange_id")
        catalog = get_elements_catalog(
            exchange_id=info["exchange_id"],
            token=info["token"],
            region=info["region"],
        )

        visible_params = {
            row.get("parameter_name")
            for row in params_rows
            if row.get("parameter_name") and row.get("visualize")
        }

        external_id_color_map = {}
        for row in assignments:
            type_name = row.get("type_name")
            if not type_name:
                continue
            param_name = row.get("parameter")
            if not param_name or param_name not in visible_params:
                continue
            family_name = row.get("family")
            color_hex = _color_to_hex(row.get("color"))

            matched_elements = [
                element
                for element in catalog
                if element.get("element_name") == type_name
                and (not family_name or element.get("family_name") == family_name)
            ]

            if not matched_elements:
                vkt.UserMessage.warning(f"No instances found for type '{type_name}'.")
                continue

            for element in matched_elements:
                alt = element.get("external_id")
                if alt:
                    external_id_color_map[alt] = color_hex

        external_ids = [{ext_id: color} for ext_id, color in external_id_color_map.items()]

        html_path = Path(__file__).resolve().parent / "ApsViewer.html"
        html = html_path.read_text(encoding="utf-8")
        html = html.replace("APS_TOKEN_PLACEHOLDER", info["token"])
        html = html.replace("URN_PLACEHOLDER", urn_bs64)
        html = html.replace("EXTERNAL_IDS_PLACEHOLDER", json.dumps(external_ids))

        return vkt.WebResult(html=html)

    def trigger_run_automation(self, params, **kwargs):
        """Run DA to add parameters to Revit types via ACC."""

        integration = vkt.external.OAuth2Integration("aps-integration-automation-v2")
        token = integration.get_access_token()

        autodesk_file = getattr(getattr(params, "run_automation", None), "autodesk_file", None)
        if not autodesk_file:
            raise vkt.UserError("Select a model in the Run Automation section first")

        project_id = getattr(autodesk_file, "project_id", None)
        file_urn = getattr(autodesk_file, "urn", None)
        if not project_id or not file_urn:
            raise vkt.UserError("Missing project id or URN for the selected Autodesk file.")

        vkt.UserMessage.info("Starting Design Automation workflow...")
        vkt.progress_message("Preparing files...", percentage=5)

        version = autodesk_file.get_latest_version(token)
        attrs = getattr(version, "attributes", {}) or {}
        display_name = attrs.get("displayName", "model")

        try:
            manifest = fetch_manifest(autodesk_file, token)
            revit_version = get_revit_version_from_manifest(manifest) or DEFAULT_REVIT_VERSION
            vkt.UserMessage.info(f"Detected Revit version: {revit_version}")
        except Exception as exc:  # noqa: BLE001
            revit_version = DEFAULT_REVIT_VERSION
            vkt.UserMessage.info(f"Could not detect Revit version ({exc}); using default {revit_version}")

        signature, activity_full_alias = get_type_parameters_signature(revit_version)

        input_revit = ActivityInputParameterAcc(
            name="rvtFile",
            localName="input.rvt",
            verb="get",
            description="Input Revit File",
            required=True,
            is_engine_input=True,
            project_id=project_id,
            linage_urn=file_urn,
        )

        folder_id = parent_folder_from_item(
            project_id=project_id,
            item_id=file_urn,
            token=token,
        )

        type_params_config = create_type_params_json(params)
        if not type_params_config:
            raise vkt.UserError("Add at least one assignment with a parameter to run automation.")

        input_json = ActivityJsonParameter(
            name="configJson",
            file_name="revit_type_params.json",
            localName="revit_type_params.json",
            verb="get",
            description="Type parameter JSON configuration",
        )
        input_json.set_content(type_params_config)

        short_uuid = uuid.uuid4().hex[:8]
        output_filename = f"{display_name}_{short_uuid}.rvt"
        output_file = ActivityOutputParameterAcc(
            name="result",
            localName="output.rvt",
            verb="put",
            description="Result Revit model with added parameters",
            folder_id=folder_id,
            project_id=project_id,
            file_name=output_filename,
        )

        workitem = WorkItemAcc(
            parameters=[input_revit, input_json, output_file],
            activity_full_alias=activity_full_alias,
        )

        vkt.UserMessage.info("Creating work item...")
        vkt.progress_message("Running Design Automation...", percentage=35)
        workitem_id = workitem.run_public_activity(
            token3lo=token,
            activity_signature=signature,
        )
        vkt.UserMessage.info(f"Workitem ID: {workitem_id}")

        elapsed = 0
        poll_interval = 10
        max_wait = 600
        final_status = None
        report_url = None

        while elapsed <= max_wait:
            status_payload = get_workitem_status(workitem_id, token)
            final_status = status_payload.get("status")
            report_url = status_payload.get("reportUrl")
            pct = min(35 + int((elapsed / max_wait) * 55), 90)
            vkt.progress_message(f"Work item status: {final_status} [{elapsed}s] {report_url=}...", percentage=pct)
            print(report_url)
            if final_status in ("success", "failed", "cancelled"):
                if final_status in ("failed", "cancelled") and report_url:
                    print(f"Work item {final_status}. Report URL: {report_url}")
                break
            time.sleep(poll_interval)
            elapsed += poll_interval

        if final_status != "success":
            msg = f"Automation did not finish with success. Status: {final_status}"
            if report_url:
                msg += f"\nReport URL: {report_url}"
            raise vkt.UserError(msg)

        output_file.create_acc_item(token=token)
        vkt.progress_message("Updated model ready for viewing!", percentage=100)

        total_targets = sum(len(entry.get("Targets", [])) for entry in type_params_config)
        success_msg = (
            "Automation completed successfully!\n\n"
            f"Parameters configured: {len(type_params_config)}\n"
            f"Total targets: {total_targets}\n"
            f"Workitem ID: {workitem_id}"
        )
        if report_url:
            success_msg += f"\nReport URL: {report_url}"

        vkt.UserMessage.success(success_msg)
