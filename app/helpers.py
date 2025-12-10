import base64
import os
import requests

from dotenv import load_dotenv
from typing import Any

load_dotenv()

APS_BASE_URL = "https://developer.api.autodesk.com"
MD_BASE_URL = f"{APS_BASE_URL}/modelderivative/v2"

def to_md_urn(wip_urn: str) -> str:
    """Convert WIP URN to Model Derivative URN."""
    raw = wip_urn.split("?", 1)[0]
    encoded = base64.urlsafe_b64encode(raw.encode("utf8")).decode("utf8")
    return encoded.rstrip("=")


def get_revit_version_from_manifest(manifest: dict) -> str | None:
    """Extract Revit version from manifest."""
    try:
        derivatives = manifest.get("derivatives", [])
        if not derivatives:
            return None
        
        for derivative in derivatives:
            properties = derivative.get("properties", {})
            doc_info = properties.get("Document Information", {})
            rvt_version = doc_info.get("RVTVersion")
            if rvt_version:
                return str(rvt_version)
        
        return None
    except Exception as e:
        print(f"Error extracting Revit version from manifest: {e}")
        return None


def fetch_manifest(autodesk_file_param, token):
    """Fetch model derivative manifest."""
    version = autodesk_file_param.get_latest_version(token)
    urn = version.urn
    encoded_urn = base64.urlsafe_b64encode(urn.encode()).decode().rstrip("=")
    url = f"https://developer.api.autodesk.com/modelderivative/v2/designdata/{encoded_urn}/manifest"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

# Supported Revit versions for Design Automation
SUPPORTED_REVIT_VERSIONS = ["2023", "2024", "2025", "2026"]

# Default to 2024 if version cannot be detected
DEFAULT_REVIT_VERSION = "2024"


def get_type_parameters_config(revit_version: str) -> tuple[str, str]:
    """Get the activity signature and full alias for TypeParameters based on Revit version.
    
    Args:
        revit_version: The Revit version string (e.g., "2023", "2024", "2025", "2026")
    
    Returns:
        tuple: (signature, activity_full_alias)
            - signature: The long base64 signature string for run_public_activity()
            - activity_full_alias: The activity ID like "nickname.ActivityName+alias" for WorkItemAcc()
    
    Raises:
        ValueError: If the Revit version is not supported or env vars are missing.
    """
    if revit_version not in SUPPORTED_REVIT_VERSIONS:
        raise ValueError(
            f"Revit version '{revit_version}' is not supported. "
            f"Supported versions: {', '.join(SUPPORTED_REVIT_VERSIONS)}"
        )
    
    # Environment variable names follow the pattern:
    # Signature: TypeParametersActivity{version} (the long base64 string)
    # Activity alias: ACTIVITY_FULL_ALIAS_TypeParameters{version} (e.g., nickname.ActivityName+dev)
    signature_env = f"TypeParametersActivity{revit_version}"
    alias_env = f"ACTIVITY_FULL_ALIAS_TypeParameters{revit_version}"
    
    signature = os.getenv(signature_env, "")
    activity_full_alias = os.getenv(alias_env, "")
    
    if not signature:
        raise ValueError(f"Missing environment variable: {signature_env}")
    if not activity_full_alias:
        raise ValueError(f"Missing environment variable: {alias_env}")
    
    return signature, activity_full_alias


# Keep old function name as alias for backwards compatibility
def get_type_parameters_signature(revit_version: str | None) -> tuple[str, str]:
    """Alias for get_type_parameters_config for backwards compatibility."""
    if revit_version is None:
        revit_version = DEFAULT_REVIT_VERSION
    return get_type_parameters_config(revit_version)


def get_viewables_from_urn(token:str, object_urn: str) -> list[dict[str, Any]]:
    """
    Get available viewables (views) from a translated model.
    """
    
    response = requests.get(
        f"{MD_BASE_URL}/designdata/{object_urn}/manifest",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30
    )
    response.raise_for_status()
    
    manifest = response.json()
    viewables: list[dict[str, Any]] = []

    def clean_name(name: str) -> str:
        """Normalize view name by stripping cosmetic prefixes."""
        return name.replace("[3D] ", "").replace("[2D] ", "").strip()

    # Prefer a single derivative tree (svf2 first, then svf) to avoid double-processing
    derivatives = manifest.get("derivatives", [])
    derivative = next(
        (d for d in derivatives if d.get("outputType") == "svf2"),
        next((d for d in derivatives if d.get("outputType") == "svf"), None),
    )

    if not derivative:
        print("No svf/svf2 derivative found in manifest")
        return viewables

    seen: set[tuple[str, str]] = set()  # (guid, display_name)
    for geom in derivative.get("children", []):
        if geom.get("type") != "geometry":
            continue
        role = geom.get("role", "")
        if role not in ["3d", "2d"]:
            continue
        guid = geom.get("guid")
        if not guid:
            continue

        base_name = geom.get("name", "") or "Unnamed View"
        candidate_names = [base_name]
        for child in geom.get("children", []):
            if child.get("type") == "view":
                candidate_names.append(child.get("name") or base_name)

        display_name = None
        for name in candidate_names:
            cleaned = clean_name(name)
            if cleaned:
                display_name = cleaned
                break
        if not display_name:
            display_name = base_name or "Unnamed View"

        key = (guid, display_name)
        if key in seen:
            continue
        seen.add(key)
        viewables.append({"guid": guid, "name": display_name, "role": role})


    return viewables


def get_view_names_from_manifest(manifest: dict) -> list[str]:
    """
    Extract view names from a model derivative manifest for IFC export selection.
    Returns a list of view names that can be used in MultiSelectField options.
    """
    seen = set()
    view_names = []
    
    for derivative in manifest.get("derivatives", []):
        if derivative.get("outputType") in ["svf", "svf2"]:
            for geometry_node in derivative.get("children", []):
                if geometry_node.get("type") == "geometry" and geometry_node.get("role") in ["3d", "2d"]:
                    # base name is the geometry node name
                    base_name = geometry_node.get("name", "")
                    candidate_names = [base_name]
                    # if there is a child of type view with its own name, include that too
                    for child_node in geometry_node.get("children", []):
                        if child_node.get("type") == "view":
                            name = child_node.get("name", "") or base_name
                            candidate_names.append(name)
                    for name in candidate_names:
                        if not name:
                            continue
                        # remove any cosmetic prefixes
                        clean = name.replace("[3D] ", "").replace("[2D] ", "")
                        if clean not in seen:
                            seen.add(clean)
                            view_names.append(clean)
    
    print(f"Found {len(view_names)} view name(s) in manifest")
    return view_names


def create_ifc_export_json(selected_view_names: list[str]) -> dict[str, Any]:
    """
    Create IFC export settings JSON configuration from selected view names.
    """
    config = {
        "view_names": selected_view_names,
        "FileVersion": "IFC4",
        "IFCFileType": "IFC",
        "ExportBaseQuantities": True,
        "SpaceBoundaryLevel": 2,
        "FamilyMappingFile": "",
        "ExportInternalRevitPropertySets": False,
        "ExportIFCCommonPropertySets": True,
        "ExportAnnotations": False,
        "Export2DElements": False,
        "ExportRoomsInView": False,
        "VisibleElementsOfCurrentView": False,
        "ExportLinkedFiles": False,
        "IncludeSteelElements": False,
        "ExportPartsAsBuildingElements": True,
        "UseActiveViewGeometry": False,
        "UseFamilyAndTypeNameForReference": False,
        "Use2DRoomBoundaryForVolume": False,
        "IncludeSiteElevation": False,
        "ExportBoundingBox": False,
        "ExportSolidModelRep": False,
        "StoreIFCGUID": False,
        "ExportSchedulesAsPsets": False,
        "ExportSpecificSchedules": False,
        "ExportUserDefinedPsets": False,
        "ExportUserDefinedPsetsFileName": "",
        "ExportUserDefinedParameterMapping": False,
        "ExportUserDefinedParameterMappingFileName": "",
        "ActivePhase": "",
        "SitePlacement": 0,
        "TessellationLevelOfDetail": 0.0,
        "UseOnlyTriangulation": False
    }
    
    return config
