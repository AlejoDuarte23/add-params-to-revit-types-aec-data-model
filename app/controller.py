import base64
import json
import textwrap
from pathlib import Path
from typing import List, Optional

import requests
import viktor as vkt


AEC_GRAPHQL_URL = "https://developer.api.autodesk.com/aec/graphql"


ELEMENTS_BY_TYPE_QUERY = """
query ElementsByType($elementGroupId: ID!, $rsqlFilter: String!, $pagination: PaginationInput) {
    elementsByElementGroup(
        elementGroupId: $elementGroupId
        filter: { query: $rsqlFilter }
        pagination: $pagination
    ) {
        pagination { cursor pageSize }
        results {
            id
            name
            alternativeIdentifiers {
                externalElementId
            }
        }
    }
}
"""


def execute_graphql(query: str, token: str, region: str, variables: Optional[dict] = None, timeout: int = 30) -> dict:
    """Execute a GraphQL query against the Autodesk AEC Data Model endpoint."""

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Region": region,
    }
    payload = {"query": query, "variables": variables or {}}
    response = requests.post(AEC_GRAPHQL_URL, headers=headers, json=payload, timeout=timeout)

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")

    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {body['errors']}")

    return body.get("data", {})


def fetch_elements_by_type(
    *,
    element_group_id: str,
    token: str,
    region: str,
    family_name: Optional[str],
    element_name: str,
    page_limit: int = 200,
) -> List[dict]:
    """Return all instance elements that match the given family and element (type) name.

    Filters on Element Context == Instance, Element Name, and optionally Family Name.
    Returns a list of dicts containing id, name, and alternativeIdentifiers.externalElementId.
    """

    family_safe = family_name.replace("'", "\\'") if family_name else None
    element_safe = element_name.replace("'", "\\'")

    rsql_parts = ["'property.name.Element Context'==Instance"]
    if family_safe:
        rsql_parts.append(f"'property.name.Family Name'=='{family_safe}'")
    rsql_parts.append(f"'property.name.Element Name'=='{element_safe}'")
    rsql_filter = " and ".join(rsql_parts)

    results: List[dict] = []
    cursor: Optional[str] = None
    page = 1

    while True:
        vkt.progress_message(message=f"Fetching instances for '{element_name}' (page {page})")
        variables = {
            "elementGroupId": element_group_id,
            "rsqlFilter": rsql_filter,
            "pagination": {"limit": page_limit} if not cursor else {"cursor": cursor, "limit": page_limit},
        }
        data = execute_graphql(ELEMENTS_BY_TYPE_QUERY, token=token, region=region, variables=variables)
        block = data.get("elementsByElementGroup") or {}
        page_results = block.get("results") or []
        results.extend(page_results)

        pagination = block.get("pagination") or {}
        new_cursor = pagination.get("cursor")
        if not new_cursor or new_cursor == cursor or len(page_results) == 0:
            break
        cursor = new_cursor
        page += 1

    return results


def get_model_info(params, **kwargs) -> Optional[dict]:
    """Return token, region, element group id, and file if an Autodesk file is selected."""

    autodesk_file = getattr(getattr(params, "model", None), "autodesk_file", None)
    if not autodesk_file:
        return None

    integration = vkt.external.OAuth2Integration("aps-integration-design")
    token = integration.get_access_token()
    region = autodesk_file.get_region(token)
    element_group_id = autodesk_file.get_aec_data_model_element_group_id(token)

    model_name = getattr(autodesk_file, "name", None) or "selected model"
    vkt.UserMessage.info(
        f"Fetching Autodesk model info for {model_name} (region: {region})."
    )

    return {
        "token": token,
        "region": region,
        "element_group_id": element_group_id,
        "file": autodesk_file,
    }


FAMILY_QUERY = """
query DistinctFamilies($elementGroupId: ID!, $limit: Int!) {
  distinctPropertyValuesInElementGroupByName(
    elementGroupId: $elementGroupId
    name: "Family Name"
    filter: { query: "'property.name.Element Context'==Instance" }
  ) {
    results {
      values(limit: $limit) {
        value
      }
    }
  }
}
"""


ELEMENT_NAMES_BY_FAMILY_QUERY = """
query ElementNamesByFamily($elementGroupId: ID!, $rsqlFilter: String!) {
  distinctPropertyValuesInElementGroupByName(
    elementGroupId: $elementGroupId
    name: "Element Name"
    filter: { query: $rsqlFilter }
  ) {
    results {
      values(limit: 500) {
        value
      }
    }
  }
}
"""


def _extract_distinct_values(data: dict) -> List[str]:
    block = data.get("distinctPropertyValuesInElementGroupByName") or {}
    values = []
    for result in block.get("results") or []:
        for item in result.get("values") or []:
            value = item.get("value")
            if value:
                values.append(value)
    return values


@vkt.memoize
def get_family_list(*, element_group_id: str, token: str, region: str) -> List[str]:
    """Return sorted distinct family names (fetched once per model)."""

    data = execute_graphql(
        FAMILY_QUERY,
        token=token,
        region=region,
        variables={"elementGroupId": element_group_id, "limit": 500},
    )
    return sorted(set(_extract_distinct_values(data)))


@vkt.memoize
def get_types_for_family(*, element_group_id: str, token: str, region: str, family_name: str) -> List[str]:
    """Return sorted distinct element names for a given family (lazy per family)."""

    rsql_filter = " and ".join(
        [
            "'property.name.Element Context'==Instance",
            f"'property.name.Family Name'=='{family_name}'",
        ]
    )

    family_data = execute_graphql(
        ELEMENT_NAMES_BY_FAMILY_QUERY,
        token=token,
        region=region,
        variables={
            "elementGroupId": element_group_id,
            "rsqlFilter": rsql_filter,
        },
    )
    return sorted(set(_extract_distinct_values(family_data)))


def get_family_options(params, **kwargs):
    info = get_model_info(params, **kwargs)
    if not info:
        return []

    try:
        return get_family_list(
            element_group_id=info["element_group_id"],
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
                element_group_id=info["element_group_id"],
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
        oauth2_integration="aps-integration-design",
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
        "Family Name", options=get_family_options, autoselect_single_option=True
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
    """Return urlsafe base64 encoding without padding for Forge URNs."""

    return base64.urlsafe_b64encode(raw_urn.encode()).decode().rstrip("=")


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

        version = info["file"].get_latest_version(info["token"])
        urn_bs64 = _encode_urn(version.urn)

        external_id_color_map = {}
        for row in assignments:
            type_name = row.get("type_name")
            if not type_name:
                continue
            family_name = row.get("family")
            color_hex = _color_to_hex(row.get("color"))

            try:
                elements = fetch_elements_by_type(
                    element_group_id=info["element_group_id"],
                    token=info["token"],
                    region=info["region"],
                    family_name=family_name,
                    element_name=type_name,
                )
            except Exception as exc:
                vkt.UserMessage.warning(f"Failed to fetch instances for {type_name}: {exc}")
                continue

            for element in elements:
                alt = (element.get("alternativeIdentifiers") or {}).get("externalElementId")
                if alt:
                    external_id_color_map[alt] = color_hex

        external_ids = [{ext_id: color} for ext_id, color in external_id_color_map.items()]

        html_path = Path(__file__).resolve().parent / "ApsViewer.html"
        html = html_path.read_text(encoding="utf-8")
        html = html.replace("APS_TOKEN_PLACEHOLDER", info["token"])
        html = html.replace("URN_PLACEHOLDER", urn_bs64)
        html = html.replace("EXTERNAL_IDS_PLACEHOLDER", json.dumps(external_ids))

        return vkt.WebResult(html=html)
