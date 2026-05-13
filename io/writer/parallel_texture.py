# Blender Plugin: 3DCityDB / CityGML - Importer / Exporter
# Author: Virtual City Systems
# Year: 2026
"""
Parallel Texture Processing for CityGML Export

Provides parallel texture compression and copying for improved export performance.
Supports JPEG compression, PNG optimization, and concurrent file operations.
"""

from __future__ import annotations
from typing import Dict, Set, Tuple, Optional, List
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


class TextureExportJob:
    """Single texture export job with source, destination, and compression settings"""
    
    def __init__(
        self,
        src_path: str,
        dest_name: str,
        compress: bool = False,
        quality: int = 85,
        max_size: Optional[Tuple[int, int]] = None
    ):
        self.src_path = src_path
        self.dest_name = dest_name
        self.compress = compress
        self.quality = quality  # JPEG quality (1-100)
        self.max_size = max_size  # Optional (width, height) resize
        self.success = False
        self.error: Optional[str] = None


def _compress_texture(
    src_path: str,
    dest_path: str,
    quality: int = 85,
    max_size: Optional[Tuple[int, int]] = None
) -> Tuple[bool, Optional[str]]:
    """
    Compress/resize texture using PIL/Pillow.
    
    Args:
        src_path: Source image path
        dest_path: Destination path (should end in .jpg for compression)
        quality: JPEG quality (1-100, default 85)
        max_size: Optional (width, height) to resize
        
    Returns:
        (success, error_message)
    """
    try:
        from PIL import Image
        
        with Image.open(src_path) as img:
            # Convert RGBA to RGB for JPEG
            if img.mode in ('RGBA', 'LA', 'P'):
                # Create white background
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Resize if requested
            if max_size:
                img.thumbnail(max_size, Image.Resampling.LANCZOS)
            
            # Save with compression
            if dest_path.lower().endswith('.jpg') or dest_path.lower().endswith('.jpeg'):
                img.save(dest_path, 'JPEG', quality=quality, optimize=True)
            elif dest_path.lower().endswith('.png'):
                # PNG optimization
                img.save(dest_path, 'PNG', optimize=True)
            else:
                # Unknown format, just copy
                img.save(dest_path)
            
        return True, None
        
    except ImportError:
        return False, "PIL/Pillow not installed (pip install Pillow)"
    except Exception as e:
        return False, str(e)


def _copy_texture(src_path: str, dest_path: str) -> Tuple[bool, Optional[str]]:
    """
    Simple file copy for textures.
    
    Returns:
        (success, error_message)
    """
    try:
        shutil.copy2(src_path, dest_path)
        return True, None
    except Exception as e:
        return False, str(e)


def _process_single_texture(
    job: TextureExportJob,
    out_dir: str,
    appearance_subdir: str = "appearance"
) -> TextureExportJob:
    """
    Process single texture export job (called in thread).
    
    Args:
        job: TextureExportJob to process
        out_dir: Output directory
        appearance_subdir: Subdirectory name (default "appearance")
        
    Returns:
        Updated job with success/error status
    """
    apdir = os.path.join(out_dir, appearance_subdir)
    os.makedirs(apdir, exist_ok=True)
    
    dest_path = os.path.join(apdir, job.dest_name)
    
    if not os.path.isfile(job.src_path):
        job.error = f"Source file not found: {job.src_path}"
        return job
    
    # Compress or copy
    if job.compress:
        job.success, job.error = _compress_texture(
            job.src_path,
            dest_path,
            quality=job.quality,
            max_size=job.max_size
        )
    else:
        job.success, job.error = _copy_texture(job.src_path, dest_path)
    
    return job


class ParallelTextureExporter:
    """
    Parallel texture exporter with compression support.
    
    Usage:
        exporter = ParallelTextureExporter(max_workers=4)
        exporter.add_texture("path/to/texture.png", compress=True, quality=75)
        results = exporter.export_all("/output/dir")
    """
    
    def __init__(self, max_workers: int = 4, appearance_subdir: str = "appearance"):
        """
        Initialize parallel texture exporter.
        
        Args:
            max_workers: Number of parallel worker threads
            appearance_subdir: Subdirectory name for textures (default "appearance")
        """
        self.max_workers = max_workers
        self.appearance_subdir = appearance_subdir
        self.jobs: List[TextureExportJob] = []
        self.used_names: Set[str] = set()
    
    def add_texture(
        self,
        src_path: str,
        compress: bool = False,
        quality: int = 85,
        max_size: Optional[Tuple[int, int]] = None
    ) -> str:
        """
        Add texture to export queue.
        
        Args:
            src_path: Source texture path
            compress: Enable JPEG compression
            quality: JPEG quality (1-100, default 85)
            max_size: Optional (width, height) resize
            
        Returns:
            Relative URI (e.g., "appearance/texture_001.jpg")
        """
        if not src_path:
            return ""
        
        # Generate unique destination filename
        base = os.path.basename(src_path).replace("\\", "/")
        base = base.split("/")[-1]
        name, ext = os.path.splitext(base)
        
        # Change extension to .jpg if compressing
        if compress and ext.lower() in ('.png', '.tif', '.tiff', '.bmp'):
            ext = '.jpg'
        elif not ext:
            ext = '.jpg'
        
        # Sanitize filename
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ext
        
        # Ensure unique name
        cand = safe
        i = 2
        while cand.lower() in self.used_names:
            cand = f"{safe[:-len(ext)]}_{i}{ext}"
            i += 1
        self.used_names.add(cand.lower())
        
        # Create job
        job = TextureExportJob(
            src_path=src_path,
            dest_name=cand,
            compress=compress,
            quality=quality,
            max_size=max_size
        )
        self.jobs.append(job)
        
        # Return relative URI
        rel_uri = f"{self.appearance_subdir}/{cand}".replace("\\", "/")
        return rel_uri
    
    def export_all(self, out_dir: str) -> Dict[str, Tuple[bool, Optional[str]]]:
        """
        Export all queued textures in parallel.
        
        Args:
            out_dir: Output directory
            
        Returns:
            Dict mapping src_path -> (success, error_message)
        """
        if not self.jobs:
            return {}
        
        results = {}
        
        # Create appearance directory
        apdir = os.path.join(out_dir, self.appearance_subdir)
        os.makedirs(apdir, exist_ok=True)
        
        # Process in parallel
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all jobs
            futures = {
                executor.submit(_process_single_texture, job, out_dir, self.appearance_subdir): job
                for job in self.jobs
            }
            
            # Collect results
            for future in as_completed(futures):
                job = futures[future]
                try:
                    result_job = future.result()
                    results[result_job.src_path] = (result_job.success, result_job.error)
                except Exception as e:
                    results[job.src_path] = (False, f"Thread exception: {e}")
        
        return results
    
    def get_texture_count(self) -> int:
        """Get number of queued textures"""
        return len(self.jobs)
    
    def clear(self):
        """Clear all queued jobs"""
        self.jobs.clear()
        self.used_names.clear()


# Legacy compatibility function
def ensure_export_texture_parallel(
    textures: List[str],
    out_dir: str,
    compress: bool = False,
    quality: int = 85,
    max_workers: int = 4
) -> Dict[str, str]:
    """
    Export multiple textures in parallel (legacy compatibility).
    
    Args:
        textures: List of source texture paths
        out_dir: Output directory
        compress: Enable JPEG compression
        quality: JPEG quality (1-100)
        max_workers: Number of parallel workers
        
    Returns:
        Dict mapping src_path -> rel_uri
    """
    exporter = ParallelTextureExporter(max_workers=max_workers)
    
    uri_map = {}
    for tex in textures:
        rel_uri = exporter.add_texture(tex, compress=compress, quality=quality)
        uri_map[tex] = rel_uri
    
    results = exporter.export_all(out_dir)
    
    # Filter out failed exports
    return {src: uri for src, uri in uri_map.items() if results.get(src, (False, None))[0]}


# Command-line interface for testing
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python parallel_texture.py <output_dir> <texture1> [texture2] ... [--compress] [--quality=85]")
        sys.exit(1)
    
    out_dir = sys.argv[1]
    textures = []
    compress = False
    quality = 85
    
    for arg in sys.argv[2:]:
        if arg == "--compress":
            compress = True
        elif arg.startswith("--quality="):
            quality = int(arg.split("=")[1])
        else:
            textures.append(arg)
    
    print(f"Exporting {len(textures)} textures to {out_dir}")
    print(f"Compress: {compress}, Quality: {quality}")
    
    exporter = ParallelTextureExporter(max_workers=4)
    for tex in textures:
        rel_uri = exporter.add_texture(tex, compress=compress, quality=quality)
        print(f"  {tex} -> {rel_uri}")
    
    results = exporter.export_all(out_dir)
    
    success_count = sum(1 for success, _ in results.values() if success)
    print(f"\nResults: {success_count}/{len(results)} successful")
    
    for src, (success, error) in results.items():
        if not success:
            print(f"  FAILED: {src} - {error}")
