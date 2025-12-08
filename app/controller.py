import base64
import json
import textwrap
from pathlib import Path
from typing import Dict, List, Optional

import requests
import viktor as vkt


DX_GRAPHQL_URL = "https://developer.api.autodesk.com/dataexchange/2023-05/graphql"


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


def _extract_props(prop_results: Optional[dict]) -> Dict[str, Optional[str]]:
    """Convert GraphQL property results into a flat dict of name -> value."""

    properties = {}
    for prop in (prop_results or {}).get("results") or []:
        name = prop.get("name")
        value = prop.get("value")
        if name:
            properties[name] = value if value is None else str(value)
    return properties


_ELEMENT_CACHE: Dict[str, List[dict]] = {}


def _fetch_elements_catalog(*, exchange_id: str, token: str, region: str, page_size: int = 200) -> List[dict]:
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
            props = _extract_props(element.get("properties"))
            family_name = props.get("Family Name")
            element_name = props.get("Element Name")

            ext_id = None
            for alt in element.get("alternativeIdentifiers") or []:
                candidate = alt.get("externalElementId")
                if candidate:
                    ext_id = str(candidate)
                    break

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


def get_elements_catalog(*, exchange_id: str, token: str, region: str) -> List[dict]:
    """Return cached catalog of elements for the exchange id."""

    if exchange_id in _ELEMENT_CACHE:
        return _ELEMENT_CACHE[exchange_id]

    catalog = _fetch_elements_catalog(exchange_id=exchange_id, token=token, region=region)
    _ELEMENT_CACHE[exchange_id] = catalog
    return catalog


def get_model_info(params, **kwargs) -> Optional[dict]:
    """Return token, region, exchange id, and file if an Autodesk file is selected."""

    autodesk_file = getattr(getattr(params, "model", None), "autodesk_file", None)
    if not autodesk_file:
        return None

    integration = vkt.external.OAuth2Integration("aps-integration-viktor")
    token = integration.get_access_token()
    region = autodesk_file.get_region(token)
    #version = autodesk_file.get_latest_version(token)
    urn = autodesk_file.urn
    print(f"{urn=}")
    exchange_id = _encode_urn(urn)
    print(f"{exchange_id=}")

    model_name = getattr(autodesk_file, "name", None) or "selected model"
    vkt.UserMessage.info(
        f"Fetching Autodesk model info for {model_name} (region: {region})."
    )

    return {
        "token": token,
        "region": region,
        "exchange_id": exchange_id,
        "file": autodesk_file,
    }


def get_family_list(*, exchange_id: str, token: str, region: str) -> List[str]:
    """Return sorted distinct family names from the cached catalog."""

    catalog = get_elements_catalog(exchange_id=exchange_id, token=token, region=region)
    families = {row.get("family_name") for row in catalog if row.get("family_name")}
    return sorted(families)


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
        oauth2_integration="aps-integration-viktor",
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
    assignments_section.assignments.parameter_value = vkt.TextField("Parameter Value")
    assignments_section.assignments.color = vkt.ColorField(
        "Color", default=vkt.Color(0, 153, 255)
    )


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

        urn_bs64 = info["exchange_id"]
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
