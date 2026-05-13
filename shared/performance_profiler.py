# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
CityGML Import/Export Performance Profiler
===========================================

Misst und analysiert Performance-Bottlenecks beim Import/Export.
Identifiziert langsame Operationen und gibt Optimierungsempfehlungen.
"""

import time
import functools
from typing import Callable, Dict, List, Any
from pathlib import Path
import json


class PerformanceProfiler:
    """
    Sammelt Performance-Metriken während Import/Export.
    
    Usage:
        profiler = PerformanceProfiler()
        
        with profiler.measure("parse_xml"):
            tree = etree.parse(path)
        
        with profiler.measure("build_geometry"):
            build_geometry(...)
        
        profiler.print_report()
    """
    
    def __init__(self):
        self.measurements: Dict[str, List[float]] = {}
        self.counts: Dict[str, int] = {}
        self.start_times: Dict[str, float] = {}
        self.memory_samples: List[tuple] = []
        self.enabled = True
    
    def measure(self, operation: str):
        """Context manager für Zeitmessung."""
        return _ProfilerContext(self, operation)
    
    def start(self, operation: str):
        """Startet Zeitmessung für Operation."""
        if not self.enabled:
            return
        self.start_times[operation] = time.perf_counter()
    
    def end(self, operation: str):
        """Beendet Zeitmessung für Operation."""
        if not self.enabled or operation not in self.start_times:
            return
        
        elapsed = time.perf_counter() - self.start_times[operation]
        
        if operation not in self.measurements:
            self.measurements[operation] = []
            self.counts[operation] = 0
        
        self.measurements[operation].append(elapsed)
        self.counts[operation] += 1
        del self.start_times[operation]
    
    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        """Berechnet Statistiken für alle Operationen."""
        stats = {}
        
        for op, times in self.measurements.items():
            if not times:
                continue
            
            total = sum(times)
            count = len(times)
            avg = total / count if count > 0 else 0
            min_time = min(times)
            max_time = max(times)
            
            stats[op] = {
                'total': total,
                'count': count,
                'average': avg,
                'min': min_time,
                'max': max_time,
                'calls_per_sec': count / total if total > 0 else 0
            }
        
        return stats
    
    def get_total_time(self) -> float:
        """Gesamtzeit aller Messungen."""
        return sum(sum(times) for times in self.measurements.values())
    
    def print_report(self, sort_by: str = 'total'):
        """
        Druckt Performance-Report.
        
        Args:
            sort_by: 'total', 'average', 'count', 'max'
        """
        stats = self.get_stats()
        
        if not stats:
            print("No performance data collected.")
            return
        
        # Sortiere nach gewählter Metrik
        sorted_ops = sorted(
            stats.items(),
            key=lambda x: x[1].get(sort_by, 0),
            reverse=True
        )
        
        total_time = self.get_total_time()
        
        print("\n" + "=" * 80)
        print("PERFORMANCE PROFILING REPORT")
        print("=" * 80)
        print(f"Total Time: {total_time:.3f}s\n")
        
        print(f"{'Operation':<40} {'Total':<10} {'Count':<8} {'Avg':<10} {'Max':<10} {'%':<6}")
        print("-" * 80)
        
        for op, data in sorted_ops:
            percentage = (data['total'] / total_time * 100) if total_time > 0 else 0
            
            print(f"{op[:38]:<40} "
                  f"{data['total']:>8.3f}s "
                  f"{data['count']:>7} "
                  f"{data['average']:>8.4f}s "
                  f"{data['max']:>8.4f}s "
                  f"{percentage:>5.1f}%")
        
        print("=" * 80)
        
        # Identifiziere Bottlenecks
        self._identify_bottlenecks(sorted_ops, total_time)
    
    def _identify_bottlenecks(self, sorted_ops: List, total_time: float):
        """Identifiziert und gibt Empfehlungen für Bottlenecks."""
        print("\n📊 BOTTLENECK ANALYSIS:")
        print("-" * 80)
        
        # Top 3 zeitintensivste Operationen
        top_3 = sorted_ops[:3]
        
        for i, (op, data) in enumerate(top_3, 1):
            percentage = (data['total'] / total_time * 100) if total_time > 0 else 0
            
            if percentage > 20:
                print(f"\n{i}. ⚠️  {op} ({percentage:.1f}% der Gesamtzeit)")
                self._suggest_optimization(op, data)
            elif percentage > 10:
                print(f"\n{i}. ℹ️  {op} ({percentage:.1f}% der Gesamtzeit)")
                self._suggest_optimization(op, data)
        
        print("\n" + "=" * 80)
    
    def _suggest_optimization(self, operation: str, data: Dict):
        """Gibt Optimierungsvorschläge für Operation."""
        suggestions = []
        
        # Viele Aufrufe → Caching erwägen
        if data['count'] > 1000:
            suggestions.append("→ Erwägen Sie Caching (viele Wiederholungen)")
        
        # Hohe durchschnittliche Zeit → Parallelisierung
        if data['average'] > 0.1:
            suggestions.append("→ Parallelisierung könnte helfen (lange Einzelaufrufe)")
        
        # Große Spanne zwischen min/max → Inkonsistenz
        if data['max'] > data['min'] * 10:
            suggestions.append("→ Große Zeitvariation - prüfen Sie Input-Größen")
        
        # Spezifische Empfehlungen nach Operation
        if 'parse' in operation.lower():
            suggestions.append("→ Verwenden Sie lxml statt xml.etree für bessere Performance")
        elif 'texture' in operation.lower():
            suggestions.append("→ Parallele Textur-Verarbeitung aktivieren")
        elif 'geometry' in operation.lower():
            suggestions.append("→ Geometry-Cache und Deduplication nutzen")
        elif 'material' in operation.lower():
            suggestions.append("→ Material-Batching implementieren")
        
        for suggestion in suggestions[:3]:  # Max 3 Vorschläge
            print(f"  {suggestion}")
    
    def save_to_file(self, filepath: str):
        """Speichert Profiling-Daten als JSON."""
        stats = self.get_stats()
        
        output = {
            'total_time': self.get_total_time(),
            'operations': stats,
            'timestamp': time.time()
        }
        
        Path(filepath).write_text(json.dumps(output, indent=2))
        print(f"Profiling data saved to: {filepath}")
    
    def reset(self):
        """Setzt alle Metriken zurück."""
        self.measurements.clear()
        self.counts.clear()
        self.start_times.clear()
        self.memory_samples.clear()


class _ProfilerContext:
    """Context manager für with-Statement."""
    
    def __init__(self, profiler: PerformanceProfiler, operation: str):
        self.profiler = profiler
        self.operation = operation
    
    def __enter__(self):
        self.profiler.start(self.operation)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.profiler.end(self.operation)
        return False


def profile(operation_name: str = None):
    """
    Decorator für Funktions-Profiling.
    
    Usage:
        @profile("parse_geometry")
        def parse_geometry(...):
            ...
    """
    def decorator(func: Callable) -> Callable:
        op_name = operation_name or f"{func.__module__}.{func.__name__}"
        
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Finde Profiler in args/kwargs oder nutze globalen
            profiler = None
            
            # Suche nach 'profiler' in kwargs
            if 'profiler' in kwargs:
                profiler = kwargs.get('profiler')
            
            # Suche in args (z.B. self.profiler)
            for arg in args:
                if hasattr(arg, 'profiler') and isinstance(arg.profiler, PerformanceProfiler):
                    profiler = arg.profiler
                    break
            
            if profiler and profiler.enabled:
                with profiler.measure(op_name):
                    return func(*args, **kwargs)
            else:
                return func(*args, **kwargs)
        
        return wrapper
    return decorator


# Globaler Profiler für einfachen Zugriff
_global_profiler = PerformanceProfiler()


def get_profiler() -> PerformanceProfiler:
    """Gibt globalen Profiler zurück."""
    return _global_profiler


def enable_profiling():
    """Aktiviert globales Profiling."""
    _global_profiler.enabled = True


def disable_profiling():
    """Deaktiviert globales Profiling."""
    _global_profiler.enabled = False


def print_report():
    """Druckt Report des globalen Profilers."""
    _global_profiler.print_report()


def reset_profiler():
    """Setzt globalen Profiler zurück."""
    _global_profiler.reset()
