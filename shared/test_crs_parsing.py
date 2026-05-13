# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Test Script für CRS-Parsing Funktionalität

Dieses Script testet die neue ADV-URN CRS-Erkennung.
Kann in Blender über die Python-Konsole ausgeführt werden:

    import sys
    addon_path = r"C:\\Users\\ofoerster\\AppData\\Roaming\\Blender Foundation\\Blender\\4.4\\scripts\\addons\\CityGML-3_Importer-Exporter"
    sys.path.append(addon_path)
    from shared import test_crs_parsing
    test_crs_parsing.run_all_tests()
"""

from shared.materials_common import normalize_crs_to_epsg, extract_vertical_epsg


def run_tests():
    """Führt alle CRS-Parsing-Tests aus"""
    
    print("\n" + "="*80)
    print("CRS-PARSING TESTS")
    print("="*80 + "\n")
    
    test_cases = [
        # ADV-URNs (NEU!)
        ("urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH", "EPSG:25832+5783", "5783"),
        ("urn:adv:crs:ETRS89_UTM33*DE_DHHN92_NH", "EPSG:25833+5783", "5783"),
        ("urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH", "EPSG:25832", "7837"),
        ("urn:adv:crs:ETRS89_UTM33*DE_DHHN2016_NH", "EPSG:25833", "7837"),
        ("urn:adv:crs:ETRS89_UTM32", "EPSG:25832", ""),
        ("urn:adv:crs:DE_DHDN_3GK3*DE_DHN92_NH", "EPSG:31467+5783", "5783"),
        
        # Standard EPSG
        ("EPSG:25832", "EPSG:25832", ""),
        ("epsg:4326", "EPSG:4326", ""),
        
        # OGC URNs
        ("urn:ogc:def:crs:EPSG::25832", "EPSG:25832", ""),
        ("urn:x-ogc:def:crs:EPSG:6.18.3:4326", "EPSG:4326", ""),
        
        # HTTP URLs
        ("http://www.opengis.net/gml/srs/epsg.xml#3857", "EPSG:3857", ""),
        ("http://www.opengis.net/def/crs/EPSG/0/4258", "EPSG:4258", ""),
        
        # Compound CRS
        ("urn:ogc:def:crs,crs:EPSG::25832,crs:EPSG::5783", "EPSG:25832+5783", "5783"),
        ("urn:ogc:def:crs,crs:EPSG:6.12:31466,crs:EPSG:6.12:5783", "EPSG:31466+5783", "5783"),
        
        # Edge Cases
        ("", "Unknown CRS", ""),
        (None, "Unknown CRS", ""),
        ("invalid_crs_string", "Unknown CRS", ""),
    ]
    
    passed = 0
    failed = 0
    
    for srs_input, expected_epsg, expected_vertical in test_cases:
        # Test normalize_crs_to_epsg
        result_epsg = normalize_crs_to_epsg(srs_input)
        epsg_ok = result_epsg == expected_epsg
        
        # Test extract_vertical_epsg
        result_vertical = extract_vertical_epsg(srs_input)
        vertical_ok = result_vertical == expected_vertical
        
        # Status
        status = "✓ PASS" if (epsg_ok and vertical_ok) else "✗ FAIL"
        
        if epsg_ok and vertical_ok:
            passed += 1
        else:
            failed += 1
        
        # Output
        print(f"{status}")
        print(f"  Input:              {repr(srs_input)}")
        print(f"  Expected EPSG:      {expected_epsg}")
        print(f"  Got EPSG:           {result_epsg} {'✓' if epsg_ok else '✗ MISMATCH'}")
        print(f"  Expected Vertical:  {repr(expected_vertical)}")
        print(f"  Got Vertical:       {repr(result_vertical)} {'✓' if vertical_ok else '✗ MISMATCH'}")
        print()
    
    print("="*80)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("="*80 + "\n")
    
    return failed == 0


def test_material_property_filtering():
    """Testet das Filtern von 'Unknown CRS' bei Material-Properties"""
    
    print("\n" + "="*80)
    print("MATERIAL PROPERTY FILTERING TEST")
    print("="*80 + "\n")
    
    test_cases = [
        ("urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH", True, "EPSG:25832+5783"),
        ("urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH", True, "EPSG:25832"),
        ("EPSG:25832", True, "EPSG:25832"),
        ("invalid_crs", False, None),
        ("", False, None),
    ]
    
    passed = 0
    failed = 0
    
    for srs_input, should_store, expected_value in test_cases:
        epsg = normalize_crs_to_epsg(srs_input)
        
        # Simuliere die Material-Property-Logik
        should_store_result = epsg and epsg != "Unknown CRS" and not epsg.startswith("Unknown")
        
        if should_store_result == should_store:
            status = "✓ PASS"
            passed += 1
        else:
            status = "✗ FAIL"
            failed += 1
        
        print(f"{status}")
        print(f"  Input:         {repr(srs_input)}")
        print(f"  Parsed EPSG:   {repr(epsg)}")
        print(f"  Should Store:  {should_store}")
        print(f"  Would Store:   {should_store_result}")
        if should_store and should_store_result:
            print(f"  Stored Value:  {repr(epsg)}")
        print()
    
    print("="*80)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("="*80 + "\n")
    
    return failed == 0


def run_all_tests():
    """Führt alle Tests aus"""
    print("\n" + "="*80)
    print("RUNNING ALL CRS TESTS")
    print("="*80)
    
    test1 = run_tests()
    test2 = test_material_property_filtering()
    
    if test1 and test2:
        print("\n✓ ALL TESTS PASSED!\n")
        return True
    else:
        print("\n✗ SOME TESTS FAILED!\n")
        return False


if __name__ == "__main__":
    run_all_tests()
