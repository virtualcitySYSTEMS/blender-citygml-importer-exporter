# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Optimized XLink Resolution System (Shared)
==========================================

Provides efficient XLink resolution with caching and batch processing.
Handles local (#id) and external (file.gml#id) references.
"""

from typing import Dict, Set, Optional, Tuple, List, Any
from collections import OrderedDict
from pathlib import Path
import re

try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET


class XLinkResolver:
    """
    Efficient XLink resolver with caching and statistics.
    
    Handles:
    - Local references (#gml_id)
    - External references (file.gml#gml_id)
    - Batch resolution
    - Reference tracking
    """
    
    def __init__(self):
        # Local ID cache (gml:id -> element)
        self.local_cache: Dict[str, Any] = {}
        
        # External file cache (file_path -> {gml:id -> element})
        self.external_cache: Dict[str, Dict[str, Any]] = {}
        
        # Pending resolution queue for batch processing
        self.pending: Dict[str, List[Tuple[str, Any]]] = {}
        
        # Statistics
        self.stats = {
            'local_hits': 0,
            'local_misses': 0,
            'external_hits': 0,
            'external_misses': 0,
            'external_files_loaded': 0,
            'batch_resolutions': 0
        }
        
        # Reference tracking for debugging
        self.references: Dict[str, Set[str]] = {}  # target_id -> set of referrer_ids
    
    def register_local_element(self, gml_id: str, element: Any):
        """
        Register element in local cache.
        
        Args:
            gml_id: GML ID of the element
            element: XML element
        """
        if gml_id:
            self.local_cache[gml_id] = element
    
    def resolve_href(self, href: str, context_element: Any = None) -> Optional[Any]:
        """
        Resolve xlink:href reference.
        
        Args:
            href: Reference string (e.g., "#id" or "file.gml#id")
            context_element: Element making the reference (for tracking)
        
        Returns:
            Referenced element or None if not found
        """
        if not href:
            return None
        
        # Normalize reference
        normalized = self._normalize_href(href)
        if not normalized:
            return None
        
        # Track reference
        if context_element is not None:
            context_id = self._get_element_id(context_element)
            if context_id:
                if normalized not in self.references:
                    self.references[normalized] = set()
                self.references[normalized].add(context_id)
        
        # Local reference (#id)
        if normalized.startswith('#'):
            return self._resolve_local(normalized[1:])
        
        # External reference (file.gml#id)
        if '#' in normalized:
            return self._resolve_external(normalized)
        
        # Bare ID (treat as local)
        return self._resolve_local(normalized)
    
    def _normalize_href(self, href: str) -> str:
        """Normalize href to consistent format."""
        if not href:
            return ""
        
        href = href.strip()
        
        # Remove URL fragments that don't represent IDs
        # Keep only: #id, file.gml#id, or bare id
        if href.startswith('http://') or href.startswith('https://'):
            # Extract fragment if present
            if '#' in href:
                href = '#' + href.split('#', 1)[1]
            else:
                return ""
        
        return href
    
    def _resolve_local(self, gml_id: str) -> Optional[Any]:
        """Resolve local reference."""
        element = self.local_cache.get(gml_id)
        
        if element is not None:
            self.stats['local_hits'] += 1
        else:
            self.stats['local_misses'] += 1
        
        return element
    
    def _resolve_external(self, ref: str) -> Optional[Any]:
        """
        Resolve external reference (file.gml#id).
        
        Args:
            ref: External reference string
        
        Returns:
            Referenced element or None
        """
        if '#' not in ref:
            return None
        
        file_path, gml_id = ref.rsplit('#', 1)
        
        # Check if file already cached
        if file_path in self.external_cache:
            element = self.external_cache[file_path].get(gml_id)
            if element is not None:
                self.stats['external_hits'] += 1
            else:
                self.stats['external_misses'] += 1
            return element
        
        # Load external file
        self._load_external_file(file_path)
        
        # Try again after loading
        if file_path in self.external_cache:
            element = self.external_cache[file_path].get(gml_id)
            if element is not None:
                self.stats['external_hits'] += 1
            else:
                self.stats['external_misses'] += 1
            return element
        
        self.stats['external_misses'] += 1
        return None
    
    def _load_external_file(self, file_path: str):
        """
        Load external GML file and cache all elements with gml:id.
        
        Args:
            file_path: Path to external GML file
        """
        try:
            path = Path(file_path)
            if not path.exists():
                return
            
            # Parse file
            tree = ET.parse(str(path))
            root = tree.getroot()
            
            # Build ID index
            file_cache = {}
            for elem in root.iter():
                gml_id = self._get_element_id(elem)
                if gml_id:
                    file_cache[gml_id] = elem
            
            self.external_cache[file_path] = file_cache
            self.stats['external_files_loaded'] += 1
            
        except Exception:
            # Failed to load - cache empty dict to avoid retrying
            self.external_cache[file_path] = {}
    
    def _get_element_id(self, element: Any) -> Optional[str]:
        """Extract gml:id from element."""
        if element is None:
            return None
        
        try:
            # Try common gml:id attribute
            for ns in ['{http://www.opengis.net/gml}',
                      '{http://www.opengis.net/gml/3.2}',
                      '']:
                gml_id = element.get(f'{ns}id')
                if gml_id:
                    return gml_id
        except (AttributeError, TypeError):
            pass
        
        return None
    
    def batch_resolve(self, refs: List[str]) -> Dict[str, Any]:
        """
        Resolve multiple references in batch for better efficiency.
        
        Args:
            refs: List of href strings
        
        Returns:
            Dictionary mapping href -> resolved element (None if not found)
        """
        results = {}
        
        # Group by external file to minimize file loads
        local_refs = []
        external_refs_by_file = {}
        
        for ref in refs:
            normalized = self._normalize_href(ref)
            if not normalized:
                results[ref] = None
                continue
            
            if normalized.startswith('#') or '#' not in normalized:
                local_refs.append((ref, normalized))
            else:
                file_path = normalized.rsplit('#', 1)[0]
                if file_path not in external_refs_by_file:
                    external_refs_by_file[file_path] = []
                external_refs_by_file[file_path].append((ref, normalized))
        
        # Resolve local references
        for ref, normalized in local_refs:
            gml_id = normalized[1:] if normalized.startswith('#') else normalized
            results[ref] = self._resolve_local(gml_id)
        
        # Load external files and resolve
        for file_path, refs_list in external_refs_by_file.items():
            if file_path not in self.external_cache:
                self._load_external_file(file_path)
            
            for ref, normalized in refs_list:
                gml_id = normalized.rsplit('#', 1)[1]
                results[ref] = self.external_cache.get(file_path, {}).get(gml_id)
        
        self.stats['batch_resolutions'] += 1
        return results
    
    def get_stats(self) -> Dict[str, Any]:
        """Get resolution statistics."""
        total_local = self.stats['local_hits'] + self.stats['local_misses']
        total_external = self.stats['external_hits'] + self.stats['external_misses']
        
        local_hit_rate = (self.stats['local_hits'] / total_local * 100) if total_local > 0 else 0
        external_hit_rate = (self.stats['external_hits'] / total_external * 100) if total_external > 0 else 0
        
        return {
            'local_cache_size': len(self.local_cache),
            'external_files_cached': len(self.external_cache),
            'local_hits': self.stats['local_hits'],
            'local_misses': self.stats['local_misses'],
            'local_hit_rate': local_hit_rate,
            'external_hits': self.stats['external_hits'],
            'external_misses': self.stats['external_misses'],
            'external_hit_rate': external_hit_rate,
            'external_files_loaded': self.stats['external_files_loaded'],
            'batch_resolutions': self.stats['batch_resolutions'],
            'total_references': sum(len(refs) for refs in self.references.values())
        }
    
    def get_references_to(self, target_id: str) -> Set[str]:
        """
        Get all elements that reference a given target.
        
        Args:
            target_id: Target gml:id
        
        Returns:
            Set of referrer gml:ids
        """
        return self.references.get(target_id, set())
    
    def clear(self):
        """Clear all caches."""
        self.local_cache.clear()
        self.external_cache.clear()
        self.pending.clear()
        self.references.clear()


def extract_href(element: Any, xlink_ns: str = "{http://www.w3.org/1999/xlink}") -> Optional[str]:
    """
    Extract xlink:href from element.
    
    Args:
        element: XML element
        xlink_ns: XLink namespace (default: W3C XLink)
    
    Returns:
        href value or None
    """
    if element is None:
        return None
    
    try:
        href = element.get(f"{xlink_ns}href")
        if href:
            return href.strip()
    except (AttributeError, TypeError):
        pass
    
    return None


def resolve_href_text(text: str, element_lookup: Dict[str, Any]) -> Optional[Any]:
    """
    Simple href resolution from text (e.g., "#id" or bare "id").
    
    Args:
        text: Reference text
        element_lookup: Dictionary mapping gml:id -> element
    
    Returns:
        Referenced element or None
    """
    if not text:
        return None
    
    text = text.strip()
    
    # Remove # prefix if present
    if text.startswith('#'):
        text = text[1:]
    
    return element_lookup.get(text)


def get_xlink_stats_summary(resolver: XLinkResolver) -> str:
    """
    Generate human-readable XLink statistics summary.
    
    Args:
        resolver: XLinkResolver instance
    
    Returns:
        Formatted statistics string
    """
    stats = resolver.get_stats()
    
    summary = f"""
=== XLink Resolution Statistics ===
Local Cache:
  Size: {stats['local_cache_size']} elements
  Hits: {stats['local_hits']}
  Misses: {stats['local_misses']}
  Hit Rate: {stats['local_hit_rate']:.1f}%

External References:
  Files Loaded: {stats['external_files_loaded']}
  Files Cached: {stats['external_files_cached']}
  Hits: {stats['external_hits']}
  Misses: {stats['external_misses']}
  Hit Rate: {stats['external_hit_rate']:.1f}%

Performance:
  Batch Resolutions: {stats['batch_resolutions']}
  Total References Tracked: {stats['total_references']}
===================================
"""
    return summary.strip()
