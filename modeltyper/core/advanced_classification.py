# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Advanced Classification Algorithms
Zusätzliche Erkennungslogik für spezielle Surface Types
"""

import math
from mathutils import Vector
from . import geometry as geom


def detect_closure_surface_candidates(bm, labels):
    """
    Identifiziert potenzielle ClosureSurface-Kandidaten
    
    ClosureSurface sind virtuelle Verschlussflächen, die typischerweise:
    - An offenen Kanten liegen (nicht alle Kanten haben 2 angrenzende Faces)
    - Kleine Öffnungen schließen
    - Geometrische Lücken füllen
    
    Returns:
        set: Indices von potentiellen ClosureSurface-Faces
    """
    candidates = set()
    
    # Finde Faces mit offenen Kanten (Boundary Edges)
    for f in bm.faces:
        has_boundary = False
        for edge in f.edges:
            if edge.is_boundary:  # Kante hat nur 1 angrenzendes Face
                has_boundary = True
                break
        
        if has_boundary:
            # Zusätzliche Kriterien:
            # - Kleine Fläche (relativ zur Gesamt-Geometrie)
            area = f.calc_area()
            if area < 0.1:  # Sehr kleine Flächen
                candidates.add(f.index)
    
    return candidates


def detect_interior_candidates(bm, labels):
    """
    Identifiziert potenzielle Interior-Surface-Kandidaten
    
    Interior Surfaces (FloorSurface, CeilingSurface, InteriorWallSurface):
    - Liegen im Inneren der Geometrie
    - Nicht sichtbar von außen
    - Typischerweise zwischen Ground und Roof
    
    Returns:
        dict: {face_index: suggested_type}
    """
    candidates = {}
    
    mins, maxs = geom.bm_bounds_local(bm)
    z_min, z_max = mins.z, maxs.z
    z_span = max(1e-6, (z_max - z_min))
    
    # Definiere "innen" als mittlerer Z-Bereich (nicht unten/oben 20%)
    z_lower = z_min + 0.2 * z_span
    z_upper = z_max - 0.2 * z_span
    
    for f in bm.faces:
        c = f.calc_center_median()
        
        # Prüfe, ob Face im mittleren Z-Bereich liegt
        if z_lower < c.z < z_upper:
            n = f.normal.normalized()
            
            # Prüfe Orientierung
            if geom.face_is_horizontal(n):
                if n.z > 0:
                    # Horizontal aufwärts → könnte FloorSurface sein
                    # Aber nur, wenn es NICHT bereits OuterFloorSurface ist
                    if labels[f.index] not in ("OuterFloorSurface", "GroundSurface"):
                        candidates[f.index] = "FloorSurface"
                else:
                    # Horizontal abwärts → könnte CeilingSurface sein
                    if labels[f.index] != "OuterCeilingSurface":
                        candidates[f.index] = "CeilingSurface"
            
            elif geom.face_is_vertical(n):
                # Vertikal → könnte InteriorWallSurface sein
                if labels[f.index] == "WallSurface":
                    # Zusätzliches Kriterium: hat das Face Nachbarn auf beiden Seiten?
                    # (Interior Walls trennen Räume)
                    candidates[f.index] = "InteriorWallSurface"
    
    return candidates


def refine_labels_with_advanced_detection(bm, labels, enable_closure=False, enable_interior=False):
    """
    Erweitert die Labels mit fortgeschrittener Erkennung
    
    Args:
        bm: BMesh
        labels: Liste der aktuellen Labels
        enable_closure: Aktiviert ClosureSurface-Erkennung
        enable_interior: Aktiviert Interior-Surface-Erkennung
    
    Returns:
        labels: Aktualisierte Label-Liste
    """
    
    if enable_closure:
        closure_candidates = detect_closure_surface_candidates(bm, labels)
        
        # Markiere nur Faces als ClosureSurface, die noch keine spezifische Klassifikation haben
        for idx in closure_candidates:
            # Optional: Nur Faces ohne starke Klassifikation überschreiben
            if labels[idx] == "WallSurface":  # Beispiel: schwache Klassifikation
                # Diese würde nur überschrieben, wenn User explizit will
                pass
    
    if enable_interior:
        interior_candidates = detect_interior_candidates(bm, labels)
        
        # Wende Interior-Klassifikationen an
        for idx, suggested_type in interior_candidates.items():
            # Nur überschreiben, wenn sinnvoll
            current = labels[idx]
            if suggested_type == "FloorSurface" and current == "OuterFloorSurface":
                continue  # Behalte OuterFloorSurface
            if suggested_type == "CeilingSurface" and current == "OuterCeilingSurface":
                continue  # Behalte OuterCeilingSurface
            
            # Ansonsten: könnte überschrieben werden (mit User-Option)
            # labels[idx] = suggested_type
    
    return labels


def suggest_improvements(bm, labels):
    """
    Analysiert die Klassifikation und gibt Verbesserungsvorschläge
    
    Returns:
        list: Liste von Vorschlägen (Strings)
    """
    suggestions = []
    
    # Zähle Surface Types
    type_counts = {}
    for label in labels:
        type_counts[label] = type_counts.get(label, 0) + 1
    
    # Analyse
    total = len(labels)
    
    # Prüfe auf ungewöhnliche Verteilungen
    if type_counts.get("WallSurface", 0) / total > 0.8:
        suggestions.append("⚠️ Sehr viele WallSurfaces - möglicherweise sind automatische Kriterien zu streng")
    
    if type_counts.get("GroundSurface", 0) == 0:
        suggestions.append("ℹ️ Keine GroundSurface erkannt - prüfen Sie die unterste Z-Position")
    
    if type_counts.get("RoofSurface", 0) == 0:
        suggestions.append("ℹ️ Keine RoofSurface erkannt - prüfen Sie die oberste Z-Position")
    
    # Prüfe auf potenzielle ClosureSurfaces
    closure_candidates = detect_closure_surface_candidates(bm, labels)
    if closure_candidates:
        suggestions.append(f"💡 {len(closure_candidates)} potenzielle ClosureSurface-Kandidaten gefunden (manuelle Prüfung empfohlen)")
    
    # Prüfe auf potenzielle Interior Surfaces
    interior_candidates = detect_interior_candidates(bm, labels)
    if interior_candidates:
        suggestions.append(f"💡 {len(interior_candidates)} potenzielle Interior-Surface-Kandidaten gefunden (LOD4)")
    
    return suggestions


def register():
    pass

def unregister():
    pass
