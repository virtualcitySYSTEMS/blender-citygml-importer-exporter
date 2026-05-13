<?xml version="1.0" encoding="UTF-8"?>
<CityModel
  xmlns="http://www.opengis.net/citygml/2.0"
  xmlns:gml="http://www.opengis.net/gml"
  xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
  xmlns:gen="http://www.opengis.net/citygml/generics/2.0"
  xmlns:xlink="http://www.w3.org/1999/xlink"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://www.opengis.net/citygml/2.0 http://schemas.opengis.net/citygml/2.0/cityGMLBase.xsd http://www.opengis.net/citygml/building/2.0 http://schemas.opengis.net/citygml/building/2.0/building.xsd">
  <gml:boundedBy>
    <gml:Envelope srsName="urn:ogc:def:crs:EPSG::25832" srsDimension="3">
      <gml:lowerCorner>0 0 0</gml:lowerCorner>
      <gml:upperCorner>3 1 2</gml:upperCorner>
    </gml:Envelope>
  </gml:boundedBy>
  <cityObjectMember>
    <bldg:Building gml:id="B_PART_TEST_PARENT">
      <gml:name>Building with inline part</gml:name>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="B_PARENT_WALL_SURFACE">
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="B_PARENT_WALL_MS">
              <gml:surfaceMember>
                <gml:Polygon gml:id="B_PARENT_WALL_POLY">
                  <gml:exterior>
                    <gml:LinearRing gml:id="B_PARENT_WALL_POLY_0_">
                      <gml:posList srsDimension="3">0 0 0 0 1 0 0 1 2 0 0 2 0 0 0</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:consistsOfBuildingPart>
        <bldg:BuildingPart gml:id="B_PART_TEST_CHILD">
          <gml:name>Inline building part</gml:name>
          <bldg:boundedBy>
            <bldg:WallSurface gml:id="B_PART_WALL_SURFACE">
              <bldg:lod2MultiSurface>
                <gml:MultiSurface gml:id="B_PART_WALL_MS">
                  <gml:surfaceMember>
                    <gml:Polygon gml:id="B_PART_WALL_POLY">
                      <gml:exterior>
                        <gml:LinearRing gml:id="B_PART_WALL_POLY_0_">
                          <gml:posList srsDimension="3">2 0 0 2 1 0 2 1 1 2 0 1 2 0 0</gml:posList>
                        </gml:LinearRing>
                      </gml:exterior>
                    </gml:Polygon>
                  </gml:surfaceMember>
                </gml:MultiSurface>
              </bldg:lod2MultiSurface>
            </bldg:WallSurface>
          </bldg:boundedBy>
        </bldg:BuildingPart>
      </bldg:consistsOfBuildingPart>
    </bldg:Building>
  </cityObjectMember>
</CityModel>
