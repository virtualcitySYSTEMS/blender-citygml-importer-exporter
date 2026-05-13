# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML Codelist Definitions

Standard codelists for CityGML attributes (class, function, usage).
Based on CityGML 2.0 and 3.0 specifications.

Sources:
- CityGML 2.0: OGC 12-019 Annex B (Codelists)
- CityGML 3.0: OGC 20-010 Annex C (Codelists)

Note: These are reference codelists. Actual implementations may extend or modify them.
"""

# Building class codes (bldg:class)
BUILDING_CLASS = {
    # CityGML 2.0 & 3.0 common codes
    "1000": "Residential building",
    "1010": "Detached house",
    "1020": "Semi-detached house", 
    "1030": "Terraced house",
    "1040": "Apartment block",
    "1050": "Dormitory",
    "1060": "Residential building (other)",
    
    "1070": "Building for religious activities",
    "1080": "Historical building",
    
    "1100": "Building for business and commerce",
    "1110": "Office building",
    "1120": "Bank",
    "1130": "Shopping centre",
    "1140": "Department store",
    "1150": "Market",
    "1160": "Kiosk",
    
    "1170": "Building for retail services",
    "1180": "Building for motor vehicle sale and service",
    "1190": "Gas station",
    
    "1200": "Building for hotel and catering trade",
    "1210": "Hotel",
    "1220": "Youth hostel",
    "1230": "Guest house",
    "1240": "Restaurant",
    "1250": "Bar / Pub",
    "1260": "Cafe",
    
    "1270": "Building for cultural activities",
    "1280": "Theatre / Opera",
    "1290": "Concert hall",
    "1300": "Museum",
    "1310": "Library",
    "1320": "Community centre",
    "1330": "Cinema",
    
    "1340": "Building for education and research",
    "1350": "School",
    "1360": "University",
    "1370": "Research establishment",
    
    "1380": "Building for health care",
    "1390": "Hospital",
    "1400": "Health centre",
    "1410": "Nursing home",
    
    "1420": "Building for sport activities",
    "1430": "Sports hall",
    "1440": "Swimming pool",
    "1450": "Ice rink",
    
    "1460": "Building for public administration",
    "1470": "Town hall",
    "1480": "Fire station",
    "1490": "Police station",
    "1500": "Prison",
    "1510": "Court",
    "1520": "Post office",
    
    "1530": "Building for infrastructure",
    "1540": "Bus station",
    "1550": "Railway station",
    "1560": "Airport",
    "1570": "Ship terminal",
    "1580": "Parking garage",
    
    "1590": "Building for industrial purposes",
    "1600": "Factory",
    "1610": "Workshop",
    "1620": "Warehouse",
    "1630": "Power plant",
    "1640": "Pumping station",
    "1650": "Water tower",
    
    "1660": "Building for agriculture and forestry",
    "1670": "Barn",
    "1680": "Stable",
    "1690": "Greenhouse",
    "1700": "Silo",
    
    "1710": "Building for military purposes",
    "1720": "Barracks",
    "1730": "Bunker",
    
    "1740": "Other building",
}

# Building function codes (bldg:function)
BUILDING_FUNCTION = {
    # Uses same codes as BUILDING_CLASS
    # Function describes the actual/current use
    **BUILDING_CLASS
}

# Building usage codes (bldg:usage)
BUILDING_USAGE = {
    # Uses same codes as BUILDING_CLASS
    # Usage describes the planned/designated use
    **BUILDING_CLASS
}

# Roof type codes (bldg:roofType)
ROOF_TYPE = {
    "1000": "Flat roof",
    "1010": "Monopitch roof",
    "1020": "Dual pitched roof / Gable roof",
    "1030": "Hipped roof",
    "1040": "Pyramidal roof",
    "1050": "Pavilion roof / Polygonal roof",
    "1060": "Mansard roof",
    "1070": "Gambrel roof",
    "1080": "Complex roof",
}

# WaterBody class codes (wtr:class)
WATERBODY_CLASS = {
    "1000": "Lake",
    "1010": "Reservoir",
    "1020": "Pond",
    "1030": "River",
    "1040": "Stream",
    "1050": "Canal",
    "1060": "Ditch",
    "1070": "Sea / Ocean",
    "1080": "Other water body",
}

# WaterBody function codes (wtr:function)
WATERBODY_FUNCTION = {
    **WATERBODY_CLASS
}

# WaterBody usage codes (wtr:usage)
WATERBODY_USAGE = {
    **WATERBODY_CLASS
}

# Transportation class codes (tran:class)
TRANSPORTATION_CLASS = {
    "1000": "Road",
    "1010": "Motorway / Highway",
    "1020": "Main road",
    "1030": "Secondary road",
    "1040": "Local road",
    "1050": "Private road",
    
    "1100": "Railway",
    "1110": "Main line railway",
    "1120": "Light railway / Tram",
    "1130": "Underground railway / Metro",
    
    "1200": "Track",
    "1210": "Path",
    "1220": "Cycle path",
    "1230": "Footpath",
    
    "1300": "Square / Plaza",
    "1310": "Pedestrian zone",
    
    "1400": "Waterway",
    "1410": "Canal",
    "1420": "Navigable waterway",
}

# Transportation function codes (tran:function)
TRANSPORTATION_FUNCTION = {
    **TRANSPORTATION_CLASS
}

# Transportation usage codes (tran:usage)
TRANSPORTATION_USAGE = {
    **TRANSPORTATION_CLASS
}

# LandUse class codes (luse:class)
LANDUSE_CLASS = {
    "1000": "Residential",
    "1010": "Detached housing",
    "1020": "Terraced housing",
    "1030": "Multi-storey housing",
    
    "1100": "Commercial",
    "1110": "Retail",
    "1120": "Office",
    "1130": "Industrial",
    
    "1200": "Mixed use",
    "1210": "Residential and commercial",
    
    "1300": "Transport infrastructure",
    "1310": "Road",
    "1320": "Railway",
    "1330": "Airport",
    "1340": "Harbour",
    
    "1400": "Agriculture",
    "1410": "Arable land",
    "1420": "Pasture",
    "1430": "Orchard",
    "1440": "Vineyard",
    
    "1500": "Forestry",
    "1510": "Deciduous forest",
    "1520": "Coniferous forest",
    "1530": "Mixed forest",
    
    "1600": "Recreation",
    "1610": "Sports field",
    "1620": "Park",
    "1630": "Playground",
    "1640": "Cemetery",
    
    "1700": "Natural",
    "1710": "Wetland",
    "1720": "Heath",
    "1730": "Scrubland",
    "1740": "Bare ground",
    
    "1800": "Water",
    "1810": "River",
    "1820": "Lake",
    "1830": "Sea",
    
    "1900": "Other land use",
}

# LandUse function codes (luse:function)
LANDUSE_FUNCTION = {
    **LANDUSE_CLASS
}

# LandUse usage codes (luse:usage)
LANDUSE_USAGE = {
    **LANDUSE_CLASS
}

# CityFurniture class codes (frn:class)
CITYFURNITURE_CLASS = {
    "1000": "Bench",
    "1010": "Street lamp",
    "1020": "Traffic signal",
    "1030": "Traffic sign",
    "1040": "Waste bin",
    "1050": "Post box",
    "1060": "Phone booth",
    "1070": "Bus shelter",
    "1080": "Fountain",
    "1090": "Statue / Monument",
    "1100": "Advertising column",
    "1110": "Bollard",
    "1120": "Barrier",
    "1130": "Other city furniture",
}

# CityFurniture function codes (frn:function)
CITYFURNITURE_FUNCTION = {
    **CITYFURNITURE_CLASS
}

# Vegetation class codes (veg:class)
VEGETATION_CLASS = {
    "1000": "Tree",
    "1010": "Deciduous tree",
    "1020": "Coniferous tree",
    "1030": "Palm tree",
    
    "1100": "Plant cover",
    "1110": "Grass",
    "1120": "Shrub",
    "1130": "Hedge",
    "1140": "Flower bed",
}

# Vegetation function codes (veg:function)
VEGETATION_FUNCTION = {
    **VEGETATION_CLASS
}

# Vegetation species (veg:species) - common examples
VEGETATION_SPECIES = {
    # Deciduous trees
    "Quercus robur": "Common Oak",
    "Fagus sylvatica": "European Beech",
    "Acer platanoides": "Norway Maple",
    "Betula pendula": "Silver Birch",
    "Tilia cordata": "Small-leaved Lime",
    "Fraxinus excelsior": "European Ash",
    "Aesculus hippocastanum": "Horse Chestnut",
    
    # Coniferous trees
    "Pinus sylvestris": "Scots Pine",
    "Picea abies": "Norway Spruce",
    "Abies alba": "Silver Fir",
    "Larix decidua": "European Larch",
    
    # Fruit trees
    "Malus domestica": "Apple",
    "Pyrus communis": "Pear",
    "Prunus avium": "Cherry",
    
    # Shrubs
    "Buxus sempervirens": "Common Box",
    "Rosa canina": "Dog Rose",
    "Corylus avellana": "Hazel",
}

# Bridge class codes (brid:class)
BRIDGE_CLASS = {
    "1000": "Bridge",
    "1010": "Road bridge",
    "1020": "Railway bridge",
    "1030": "Pedestrian bridge",
    "1040": "Aqueduct",
    "1050": "Viaduct",
    "1060": "Other bridge",
}

# Bridge function codes (brid:function)
BRIDGE_FUNCTION = {
    **BRIDGE_CLASS
}

# Tunnel class codes (tun:class)
TUNNEL_CLASS = {
    "1000": "Tunnel",
    "1010": "Road tunnel",
    "1020": "Railway tunnel",
    "1030": "Metro tunnel",
    "1040": "Pedestrian tunnel",
    "1050": "Utility tunnel",
    "1060": "Other tunnel",
}

# Tunnel function codes (tun:function)
TUNNEL_FUNCTION = {
    **TUNNEL_CLASS
}

# Master dictionary for all codelists
CODELISTS = {
    'bldg:class': BUILDING_CLASS,
    'bldg:function': BUILDING_FUNCTION,
    'bldg:usage': BUILDING_USAGE,
    'bldg:roofType': ROOF_TYPE,
    
    'wtr:class': WATERBODY_CLASS,
    'wtr:function': WATERBODY_FUNCTION,
    'wtr:usage': WATERBODY_USAGE,
    
    'tran:class': TRANSPORTATION_CLASS,
    'tran:function': TRANSPORTATION_FUNCTION,
    'tran:usage': TRANSPORTATION_USAGE,
    
    'luse:class': LANDUSE_CLASS,
    'luse:function': LANDUSE_FUNCTION,
    'luse:usage': LANDUSE_USAGE,
    
    'frn:class': CITYFURNITURE_CLASS,
    'frn:function': CITYFURNITURE_FUNCTION,
    
    'veg:class': VEGETATION_CLASS,
    'veg:function': VEGETATION_FUNCTION,
    'veg:species': VEGETATION_SPECIES,
    
    'brid:class': BRIDGE_CLASS,
    'brid:function': BRIDGE_FUNCTION,
    
    'tun:class': TUNNEL_CLASS,
    'tun:function': TUNNEL_FUNCTION,
}


def get_codelist(attribute_name: str) -> dict | None:
    """
    Get codelist for a CityGML attribute.
    
    Args:
        attribute_name: Qualified attribute name (e.g., 'bldg:class', 'wtr:function')
        
    Returns:
        Dictionary mapping codes to descriptions, or None if not found
    """
    return CODELISTS.get(attribute_name)


def validate_code(attribute_name: str, code: str) -> bool:
    """
    Validate if a code is valid for the given attribute.
    
    Args:
        attribute_name: Qualified attribute name (e.g., 'bldg:class')
        code: Code value to validate (e.g., '1000')
        
    Returns:
        True if code is valid, False otherwise
    """
    codelist = get_codelist(attribute_name)
    if not codelist:
        return True  # No validation if codelist not defined
    
    return code in codelist


def get_code_description(attribute_name: str, code: str) -> str | None:
    """
    Get human-readable description for a code.
    
    Args:
        attribute_name: Qualified attribute name (e.g., 'bldg:class')
        code: Code value (e.g., '1000')
        
    Returns:
        Description string or None if not found
    """
    codelist = get_codelist(attribute_name)
    if not codelist:
        return None
    
    return codelist.get(code)


def get_common_codes(attribute_name: str, limit: int = 10) -> list[tuple[str, str]]:
    """
    Get most common codes for an attribute (for UI dropdowns).
    
    Args:
        attribute_name: Qualified attribute name (e.g., 'bldg:class')
        limit: Maximum number of codes to return
        
    Returns:
        List of (code, description) tuples
    """
    codelist = get_codelist(attribute_name)
    if not codelist:
        return []
    
    # Return first N items (codelists are already ordered by importance)
    items = list(codelist.items())
    return items[:limit]


def search_codes(attribute_name: str, query: str) -> list[tuple[str, str]]:
    """
    Search codes by description text.
    
    Args:
        attribute_name: Qualified attribute name (e.g., 'bldg:class')
        query: Search query (case-insensitive)
        
    Returns:
        List of (code, description) tuples matching the query
    """
    codelist = get_codelist(attribute_name)
    if not codelist:
        return []
    
    query_lower = query.lower()
    results = []
    
    for code, desc in codelist.items():
        if query_lower in desc.lower():
            results.append((code, desc))
    
    return results
