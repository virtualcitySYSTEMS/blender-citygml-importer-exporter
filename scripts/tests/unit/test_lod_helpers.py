import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def load_module(relative_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


lod_helpers = load_module("shared/lod_helpers.py", "lod_helpers_under_test")


class FakeMaterial(dict):
    pass


class FakeMeshData:
    def __init__(self, materials):
        self.materials = materials


class FakeObject(dict):
    def __init__(self, name, *, obj_type="EMPTY", parent=None, materials=None, **props):
        super().__init__(**props)
        self.name = name
        self.type = obj_type
        self.parent = parent
        self.children = []
        self.data = FakeMeshData(materials or [])
        if parent is not None:
            parent.children.append(self)


class FakeScene:
    def __init__(self, objects):
        self.objects = objects


def test_promote_opening_host_lod_updates_entire_top_level_hierarchy():
    top_material = FakeMaterial(lod=2)
    part_material = FakeMaterial(lod=2)
    hierarchy = FakeObject("Hierarchy", structure_type="hierarchical", lod=2)
    top_mesh = FakeObject("Building", obj_type="MESH", parent=hierarchy, materials=[top_material], lod=2)
    part_mesh = FakeObject(
        "BuildingPart",
        obj_type="MESH",
        parent=hierarchy,
        materials=[part_material],
        cgml3_feature="BuildingPart",
        lod=2,
    )

    changed = lod_helpers.promote_opening_host_lod(top_mesh)

    assert changed is True
    assert hierarchy["lod"] == 3
    assert top_mesh["lod"] == 3
    assert part_mesh["lod"] == 3
    assert top_material["lod"] == 3
    assert part_material["lod"] == 3


def test_normalize_scene_top_level_lods_raises_mixed_top_level_to_highest_lod():
    wall_material = FakeMaterial(lod=2)
    opening_material = FakeMaterial(lod=3)
    part_material = FakeMaterial(lod=2)
    hierarchy = FakeObject("Hierarchy", structure_type="hierarchical", lod=2)
    top_mesh = FakeObject("Building", obj_type="MESH", parent=hierarchy, materials=[wall_material, opening_material], lod=2)
    part_mesh = FakeObject(
        "BuildingPart",
        obj_type="MESH",
        parent=hierarchy,
        materials=[part_material],
        cgml3_feature="BuildingPart",
        lod=2,
    )
    scene = FakeScene([hierarchy, top_mesh, part_mesh])

    result = lod_helpers.normalize_scene_top_level_lods(scene)

    assert result["changed_count"] == 1
    assert result["normalized"][0]["lod"] == 3
    assert hierarchy["lod"] == 3
    assert top_mesh["lod"] == 3
    assert part_mesh["lod"] == 3
    assert wall_material["lod"] == 3
    assert opening_material["lod"] == 3
    assert part_material["lod"] == 3


def test_opening_lod_for_citygml3_keeps_lod2():
    wall_material = FakeMaterial(lod=2)
    hierarchy = FakeObject("Hierarchy", structure_type="hierarchical", lod=2)
    top_mesh = FakeObject("Building", obj_type="MESH", parent=hierarchy, materials=[wall_material], lod=2)

    target_lod = lod_helpers.opening_lod_for_version(top_mesh, "3.0")

    assert target_lod == 2


def test_citygml3_opening_lod_raises_lod1_to_lod2_only():
    wall_material = FakeMaterial(lod=1)
    opening_material = FakeMaterial(lod=2)
    hierarchy = FakeObject("Hierarchy", structure_type="hierarchical", lod=1)
    top_mesh = FakeObject(
        "Building",
        obj_type="MESH",
        parent=hierarchy,
        materials=[wall_material, opening_material],
        lod=1,
    )

    target_lod = lod_helpers.opening_lod_for_version(top_mesh, "3.0")
    changed = lod_helpers.promote_opening_host_lod(top_mesh, opening_lod=target_lod)

    assert target_lod == 2
    assert changed is True
    assert hierarchy["lod"] == 2
    assert top_mesh["lod"] == 2
    assert wall_material["lod"] == 2
    assert opening_material["lod"] == 2
