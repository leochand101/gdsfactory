import datetime
import hashlib
import itertools
import math
import os
import pathlib
import tempfile
import uuid
import warnings
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import gdspy
import numpy as np
import yaml
from numpy import int64
from omegaconf import DictConfig, OmegaConf
from phidl.device_layout import CellArray, Device, Label, _parse_layer
from typing_extensions import Literal

from gdsfactory.component_reference import ComponentReference, Coordinate, SizeInfo
from gdsfactory.config import CONF, logger
from gdsfactory.cross_section import CrossSection
from gdsfactory.layers import LAYER_COLORS, LayerColor, LayerColors
from gdsfactory.port import (
    Port,
    auto_rename_ports,
    auto_rename_ports_counter_clockwise,
    auto_rename_ports_layer_orientation,
    auto_rename_ports_orientation,
    map_ports_layer_to_orientation,
    map_ports_to_orientation_ccw,
    map_ports_to_orientation_cw,
    select_ports,
)
from gdsfactory.serialization import clean_dict
from gdsfactory.snap import snap_to_grid

Plotter = Literal["holoviews", "matplotlib", "qt"]
Axis = Literal["x", "y"]


class MutabilityError(ValueError):
    pass


mutability_error_message = """
You cannot modify a Component after creation as it will affect all of its instances.

Create a new Component and add a reference to it.

For example:

# BAD
c = gf.components.bend_euler()
c.add_ref(gf.components.mzi())

# GOOD
c = gf.Component()
c.add_ref(gf.components.bend_euler())
c.add_ref(gf.components.mzi())
"""

PathType = Union[str, Path]
Float2 = Tuple[float, float]
Layer = Tuple[int, int]
Layers = Tuple[Layer, ...]
LayerSpec = Union[str, int, Layer, None]

tmp = pathlib.Path(tempfile.TemporaryDirectory().name) / "gdsfactory"
tmp.mkdir(exist_ok=True, parents=True)
_timestamp2019 = datetime.datetime.fromtimestamp(1572014192.8273)
MAX_NAME_LENGTH = 32


def _rnd(arr, precision=1e-4):
    arr = np.ascontiguousarray(arr)
    ndigits = round(-math.log10(precision))
    return np.ascontiguousarray(arr.round(ndigits) / precision, dtype=np.int64)


class Component(Device):
    """A Component is like an empty canvas, where you can add polygons,.

    references to other Components and ports (to connect to other components).

    - get/write YAML metadata
    - get ports by type (optical, electrical ...)
    - set data_analysis and test_protocols

    Args:
        name: component_name. Use @cell decorator for auto-naming.
        version: component version.
        changelog: changes from the last version.

    Keyword Args:
        with_uuid: adds unique identifier.

    Properties:
        info: dictionary that includes
            - derived properties
            - external metadata (test_protocol, docs, ...)
            - simulation_settings
            - function_name
            - name: for the component

        settings:
            full: full settings passed to the function to create component.
            changed: changed settings.
            default: default component settings.
            child: dict info from the children, if any.
    """

    def __init__(
        self,
        name: str = "Unnamed",
        version: str = "0.0.1",
        changelog: str = "",
        **kwargs,
    ) -> None:
        """Initialize the Component object."""
        self.__ports__ = {}
        self.uid = str(uuid.uuid4())[:8]
        if "with_uuid" in kwargs or name == "Unnamed":
            name += f"_{self.uid}"

        super().__init__(name=name, exclude_from_current=True)
        self.name = name  # overwrite PHIDL's incremental naming convention
        self.info: Dict[str, Any] = {}

        self.settings: Dict[str, Any] = {}
        self._locked = False
        self.get_child_name = False
        self.version = version
        self.changelog = changelog
        self._reference_names_counter = Counter()
        self._reference_names_used = set()

    def __lshift__(self, element):
        """Convenience operator equivalent to add_ref()."""
        return self.add_ref(element)

    def unlock(self) -> None:
        """Only do this if you know what you are doing."""
        self._locked = False

    def lock(self) -> None:
        """Makes sure components can't add new elements or move existing ones.

        Components lock automatically when going into the CACHE to
        ensure one component does not change others
        """
        self._locked = True

    @classmethod
    def __get_validators__(cls):
        """Get validators for the Component object."""
        yield cls.validate

    @classmethod
    def validate(cls, v):
        """Pydantic assumes component is valid if the following are true.

        - name characters < MAX_NAME_LENGTH
        - is not empty (has references or polygons)
        """
        MAX_NAME_LENGTH = 100
        assert isinstance(
            v, Component
        ), f"TypeError, Got {type(v)}, expecting Component"
        assert (
            len(v.name) <= MAX_NAME_LENGTH
        ), f"name `{v.name}` {len(v.name)} > {MAX_NAME_LENGTH} "
        return v

    @property
    def named_references(self):
        return {ref.name: ref for ref in self.references}

    @property
    def aliases(self):
        warnings.warn(
            "aliases attribute has been renamed to named_references and may be deprecated in a future version of gdsfactory",
            DeprecationWarning,
        )
        return self.named_references

    @aliases.setter
    def aliases(self, value):
        warnings.warn(
            "Setting aliases is no longer supported. aliases attribute has been renamed to named_references and may be deprecated in a future version of gdsfactory. This operation will have no effect.",
            DeprecationWarning,
        )

    def add_label(
        self,
        text: str = "hello",
        position: Tuple[float, float] = (0.0, 0.0),
        magnification: Optional[float] = None,
        rotation: Optional[float] = None,
        anchor: str = "o",
        layer="TEXT",
    ) -> Label:
        """Adds Label to the Component.

        Args:
            text: Label text.
            position: x-, y-coordinates of the Label location.
            magnification:int, float, or None Magnification factor for the Label text.
            rotation: Angle rotation of the Label text.
            anchor: {'n', 'e', 's', 'w', 'o', 'ne', 'nw', ...}
                Position of the anchor relative to the text.
            layer: Specific layer(s) to put Label on.
        """
        from gdsfactory.pdk import get_layer

        layer = get_layer(layer)

        gds_layer, gds_datatype = layer

        if type(text) is not str:
            text = text
        label = Label(
            text=text,
            position=position,
            anchor=anchor,
            magnification=magnification,
            rotation=rotation,
            layer=gds_layer,
            texttype=gds_datatype,
        )
        self.add(label)
        return label

    @property
    def bbox(self):
        """Returns the bounding box of the ComponentReference.

        it snaps to 3 decimals in um (0.001um = 1nm precision)
        """
        bbox = self.get_bounding_box()
        if bbox is None:
            bbox = ((0, 0), (0, 0))
        return np.round(bbox, 3)

    @property
    def ports_layer(self) -> Dict[str, str]:
        """Returns a mapping from layer0_layer1_E0: portName."""
        return map_ports_layer_to_orientation(self.ports)

    def port_by_orientation_cw(self, key: str, **kwargs):
        """Returns port by indexing them clockwise."""
        m = map_ports_to_orientation_cw(self.ports, **kwargs)
        if key not in m:
            raise KeyError(f"{key} not in {list(m.keys())}")
        key2 = m[key]
        return self.ports[key2]

    def port_by_orientation_ccw(self, key: str, **kwargs):
        """Returns port by indexing them clockwise."""
        m = map_ports_to_orientation_ccw(self.ports, **kwargs)
        if key not in m:
            raise KeyError(f"{key} not in {list(m.keys())}")
        key2 = m[key]
        return self.ports[key2]

    def get_ports_xsize(self, **kwargs) -> float:
        """Returns xdistance from east to west ports.

        Keyword Args:
            layer: port GDS layer.
            prefix: with in port name.
            orientation: in degrees.
            width: port width.
            layers_excluded: List of layers to exclude.
            port_type: optical, electrical, ...
        """
        ports_cw = self.get_ports_list(clockwise=True, **kwargs)
        ports_ccw = self.get_ports_list(clockwise=False, **kwargs)
        return snap_to_grid(ports_ccw[0].x - ports_cw[0].x)

    def get_ports_ysize(self, **kwargs) -> float:
        """Returns ydistance from east to west ports.

        Keyword Args:
            layer: port GDS layer.
            prefix: with in port name.
            orientation: in degrees.
            width: port width (um).
            layers_excluded: List of layers to exclude.
            port_type: optical, electrical, ...
        """
        ports_cw = self.get_ports_list(clockwise=True, **kwargs)
        ports_ccw = self.get_ports_list(clockwise=False, **kwargs)
        return snap_to_grid(ports_ccw[0].y - ports_cw[0].y)

    def plot_netlist(self, with_labels: bool = True, font_weight: str = "normal"):
        """Plots a netlist graph with networkx.

        Args:
            with_labels: add label to each node.
            font_weight: normal, bold.
        """
        import matplotlib.pyplot as plt
        import networkx as nx

        plt.figure()
        netlist = self.get_netlist()
        connections = netlist["connections"]
        placements = netlist["placements"]
        G = nx.Graph()
        G.add_edges_from(
            [
                (",".join(k.split(",")[:-1]), ",".join(v.split(",")[:-1]))
                for k, v in connections.items()
            ]
        )

        pos = {k: (v["x"], v["y"]) for k, v in placements.items()}
        labels = {k: ",".join(k.split(",")[:1]) for k in placements.keys()}
        nx.draw(
            G,
            with_labels=with_labels,
            font_weight=font_weight,
            labels=labels,
            pos=pos,
        )
        return G

    def get_netlist_yaml(self, **kwargs) -> Dict[str, Any]:
        from gdsfactory.get_netlist import get_netlist_yaml

        return get_netlist_yaml(self, **kwargs)

    def write_netlist(self, filepath: str) -> None:
        """Write netlist in YAML."""
        netlist = self.get_netlist()
        OmegaConf.save(netlist, filepath)

    def write_netlist_dot(self, filepath: Optional[str] = None) -> None:
        """Write netlist graph in DOT format."""
        from networkx.drawing.nx_agraph import write_dot

        filepath = filepath or f"{self.name}.dot"

        G = self.plot_netlist()
        write_dot(G, filepath)

    def get_netlist(self, **kwargs) -> DictConfig:
        """Returns netlist (instances, placements, connections, ports).

        Keyword Args:
            component: to extract netlist.
            full_settings: True returns all, false changed settings.
            layer_label: label to read instanceNames from (if any).
            tolerance: tolerance in nm to consider two ports connected.

        Returns:
            instances: Dict of instance name and settings.
            connections: Dict of Instance1Name,portName: Instace2Name,portName.
            placements: Dict of instance names and placements (x, y, rotation).
            port: Dict portName: ComponentName,port.
            name: name of component.
        """
        from gdsfactory.get_netlist import get_netlist

        return get_netlist(component=self, **kwargs)

    def get_netlist_recursive(self, **kwargs) -> Dict[str, DictConfig]:
        """Returns recursive netlist for a component and subcomponents.

        Keyword Args:
            component: to extract netlist.
            component_suffix: suffix to append to each component name.
                useful if to save and reload a back-annotated netlist.
            get_netlist_func: function to extract individual netlists.
            full_settings: True returns all, false changed settings.
            layer_label: label to read instanceNames from (if any).
            tolerance: tolerance in nm to consider two ports connected.

        Returns:
            Dictionary of netlists, keyed by the name of each component.
        """
        from gdsfactory.get_netlist import get_netlist_recursive

        return get_netlist_recursive(component=self, **kwargs)

    def assert_ports_on_grid(self, nm: int = 1) -> None:
        """Asserts that all ports are on grid."""
        for port in self.ports.values():
            port.assert_on_grid(nm=nm)

    def get_ports_dict(self, **kwargs) -> Dict[str, Port]:
        """Returns a dict of ports.

        Keyword Args:
            layer: port GDS layer.
            prefix: for example "E" for east, "W" for west ...
        """
        return select_ports(self.ports, **kwargs)

    def get_ports_list(self, **kwargs) -> List[Port]:
        """Returns list of ports.

        Keyword Args:
            layer: select ports with GDS layer.
            prefix: select ports with port name.
            orientation: select ports with orientation in degrees.
            width: select ports with port width.
            layers_excluded: List of layers to exclude.
            port_type: select ports with port_type (optical, electrical, vertical_te).
            clockwise: if True, sort ports clockwise, False: counter-clockwise.
        """
        return list(select_ports(self.ports, **kwargs).values())

    def ref(
        self,
        position: Coordinate = (0, 0),
        port_id: Optional[str] = None,
        rotation: int = 0,
        h_mirror: bool = False,
        v_mirror: bool = False,
    ) -> "ComponentReference":
        """Returns Component reference.

        Args:
            position: x, y position.
            port_id: name of the port.
            rotation: in degrees.
            h_mirror: horizontal mirror using y axis (x, 1) (1, 0).
                This is the most common mirror.
            v_mirror: vertical mirror using x axis (1, y) (0, y).
        """
        _ref = ComponentReference(self)

        if port_id and port_id not in self.ports:
            raise ValueError(f"port {port_id} not in {self.ports.keys()}")

        origin = self.ports[port_id].center if port_id else (0, 0)
        if h_mirror:
            _ref.reflect_h(port_id)

        if v_mirror:
            _ref.reflect_v(port_id)

        if rotation != 0:
            _ref.rotate(rotation, origin)
        _ref.move(origin, position)

        return _ref

    def ref_center(self, position=(0, 0)):
        """Returns a reference of the component centered at (x=0, y=0)."""
        si = self.size_info
        yc = si.south + si.height / 2
        xc = si.west + si.width / 2
        center = (xc, yc)
        _ref = ComponentReference(self)
        _ref.move(center, position)
        return _ref

    def __repr__(self) -> str:
        """Return a string representation of the object."""
        return f"{self.name}: uid {self.uid}, ports {list(self.ports.keys())}, references {list(self.named_references.keys())}, {len(self.polygons)} polygons"

    def pprint(self) -> None:
        """Prints component info."""
        # print(OmegaConf.to_yaml(self.to_dict()))
        print(yaml.dump(self.to_dict()))

    def pprint_ports(self) -> None:
        """Prints component netlists."""
        ports_list = self.get_ports_list()
        for port in ports_list:
            print(port)

    @property
    def metadata_child(self) -> DictConfig:
        """Returns metadata from child if any, Otherwise returns component own.

        metadata Great to access the children metadata at the bottom of the
        hierarchy.
        """
        settings = dict(self.settings)

        while settings.get("child"):
            settings = settings.get("child")

        return DictConfig(dict(settings))

    @property
    def metadata(self) -> DictConfig:
        return DictConfig(dict(self.settings))

    def add_port(
        self,
        name: Optional[Union[str, int, object]] = None,
        center: Optional[Tuple[float, float]] = None,
        width: Optional[float] = None,
        orientation: Optional[float] = None,
        port: Optional[Port] = None,
        layer: LayerSpec = None,
        port_type: str = "optical",
        cross_section: Optional[CrossSection] = None,
    ) -> Port:
        """Add port to component.

        You can copy an existing port like add_port(port = existing_port) or
        create a new port add_port(myname, mycenter, mywidth, myorientation).
        You can also copy an existing port
        with a new name add_port(port = existing_port, name = new_name)

        Args:
            name: port name.
            center: x, y.
            width: in um.
            orientation: in deg.
            port: optional port.
            layer: port layer.
            port_type: optical, electrical, vertical_dc, vertical_te, vertical_tm.
            cross_section: port cross_section.
        """
        from gdsfactory.pdk import get_layer

        layer = get_layer(layer)

        if port:
            if not isinstance(port, Port):
                raise ValueError(f"add_port() needs a Port, got {type(port)}")
            p = port.copy(new_uid=True)
            if name is not None:
                p.name = name
            p.parent = self

        elif isinstance(name, Port):
            p = name.copy(new_uid=True)
            p.parent = self
            name = p.name
        else:
            if width is None:
                raise ValueError("Port needs width parameter (um).")
            if center is None:
                raise ValueError("Port needs center parameter (x, y) um.")

            # half_width = width / 2
            # half_width_correct = snap_to_grid(half_width, nm=1)
            # if not np.isclose(half_width, half_width_correct):
            #     warnings.warn(
            #         f"port width = {width} will create off-grid points.\n"
            #         f"You can fix it by changing width to {2*half_width_correct}\n"
            #         f"port {name}, {center}  {orientation} deg",
            #         stacklevel=3,
            #     )
            # width = snap_to_grid(width)
            # center = snap_to_grid(center)

            p = Port(
                name=name,
                center=center,
                width=width,
                orientation=orientation,
                parent=self,
                layer=layer,
                port_type=port_type,
                cross_section=cross_section,
            )
        if name is not None:
            p.name = name
        if p.name in self.ports:
            raise ValueError(f"add_port() Port name {p.name!r} exists in {self.name!r}")

        self.ports[p.name] = p
        return p

    def add_ports(
        self, ports: Union[List[Port], Dict[str, Port]], prefix: str = ""
    ) -> None:
        """Add a list or dict of ports.

        you can include a prefix to add to the new port names to avoid name conflicts.

        Args:
            ports: list or dict of ports.
            prefix: to prepend to each port name.
        """
        ports = ports if isinstance(ports, list) else ports.values()
        for port in list(ports):
            name = f"{prefix}{port.name}" if prefix else port.name
            self.add_port(name=name, port=port)

    def snap_ports_to_grid(self, nm: int = 1) -> None:
        for port in self.ports.values():
            port.snap_to_grid(nm=nm)

    def remove_layers(
        self,
        layers: Union[List[Tuple[int, int]], Tuple[int, int]] = (),
        include_labels: bool = True,
        invert_selection: bool = False,
        recursive: bool = True,
    ) -> "Component":
        """Remove a list of layers and returns the same Component.

        Args:
            layers: list of layers to remove.
            include_labels: remove labels on those layers.
            invert_selection: removes all layers except layers specified.
            recursive: operate on the cells included in this cell.
        """
        from gdsfactory.pdk import get_layer

        layers = [_parse_layer(get_layer(layer)) for layer in layers]
        all_D = list(self.get_dependencies(recursive))
        all_D += [self]
        for D in all_D:
            for polygonset in D.polygons:
                polygon_layers = zip(polygonset.layers, polygonset.datatypes)
                polygons_to_keep = [(pl in layers) for pl in polygon_layers]
                if not invert_selection:
                    polygons_to_keep = [(not p) for p in polygons_to_keep]
                polygonset.polygons = [
                    p for p, keep in zip(polygonset.polygons, polygons_to_keep) if keep
                ]
                polygonset.layers = [
                    p for p, keep in zip(polygonset.layers, polygons_to_keep) if keep
                ]
                polygonset.datatypes = [
                    p for p, keep in zip(polygonset.datatypes, polygons_to_keep) if keep
                ]

            paths = []
            for path in D.paths:
                paths.extend(
                    path
                    for layer in zip(path.layers, path.datatypes)
                    if layer not in layers
                )

            D.paths = paths

            if include_labels:
                new_labels = []
                for label in D.labels:
                    original_layer = (label.layer, label.texttype)
                    original_layer = _parse_layer(original_layer)
                    if invert_selection:
                        keep_layer = original_layer in layers
                    else:
                        keep_layer = original_layer not in layers
                    if keep_layer:
                        new_labels += [label]
                D.labels = new_labels
        return self

    def extract(
        self,
        layers: Union[List[Tuple[int, int]], Tuple[int, int]] = (),
    ) -> "Component":
        """Extract polygons from a Component and returns a new Component.

        Adapted from phidl.geometry.
        """
        from gdsfactory.name import clean_value

        component = Component(f"{self.name}_{clean_value(layers)}")
        if type(layers) not in (list, tuple):
            raise ValueError("layers needs to be a list or tuple")
        poly_dict = self.get_polygons(by_spec=True)
        parsed_layer_list = [_parse_layer(layer) for layer in layers]
        for layer, polys in poly_dict.items():
            if _parse_layer(layer) in parsed_layer_list:
                component.add_polygon(polys, layer=layer)
        return component

    def add_polygon(self, points, layer=np.nan):
        """Adds a Polygon to the Component.

        Args:
            points: Coordinates of the vertices of the Polygon.
            layer: layer spec to add polygon on.
        """
        from gdsfactory.pdk import get_layer

        return super().add_polygon(points=points, layer=get_layer(layer))

    def copy(self) -> "Component":
        from gdsfactory.copy import copy

        return copy(self)

    def copy_child_info(self, component: "Component") -> None:
        """Copy info from child component into parent.

        Parent components can access child cells settings.
        """
        if not isinstance(component, Component):
            raise ValueError(f"{type(component)} is not a Component")

        self.get_child_name = True
        self.child = component
        self.info.update(component.info)

    @property
    def size_info(self) -> SizeInfo:
        """Size info of the component."""
        return SizeInfo(self.bbox)

    def get_setting(self, setting: str) -> Union[str, int, float]:
        return (
            self.info.get(setting)
            or self.settings.full.get(setting)
            or self.metadata_child.get(setting)
        )

    def is_unlocked(self) -> None:
        """Raises error if Component is locked."""
        if self._locked:
            raise MutabilityError(
                f"Component {self.name!r} cannot be modified as it's already on cache. "
                + mutability_error_message
            )

    def _add(self, element) -> None:
        """Add a new element or list of elements to this Component.

        Args:
            element: `PolygonSet`, `CellReference`, `CellArray` or iterable
            The element or iterable of elements to be inserted in this cell.

        Raises:
            MutabilityError: if component is locked.
        """
        self.is_unlocked()
        super().add(element)

    def add(self, element) -> None:
        """Add a new element or list of elements to this Component.

        Args:
            element: `PolygonSet`, `CellReference`, `CellArray` or iterable
            The element or iterable of elements to be inserted in this cell.

        Raises:
            MutabilityError: if component is locked.
        """
        self._add(element)
        if isinstance(element, (gdspy.CellReference, gdspy.CellArray)):
            self._register_reference(element)
        if isinstance(element, Iterable):
            for i in element:
                if isinstance(i, (gdspy.CellReference, gdspy.CellArray)):
                    self._register_reference(i)

    def add_array(
        self,
        component: "Component",
        columns: int = 2,
        rows: int = 2,
        spacing: Tuple[float, float] = (100, 100),
        alias: Optional[str] = None,
    ) -> CellArray:
        """Creates a CellArray reference to a Component.

        Args:
            component: The referenced component.
            columns: Number of columns in the array.
            rows: Number of rows in the array.
            spacing: array-like[2] of int or float.
                Distance between adjacent columns and adjacent rows.
            alias: str or None. Alias of the referenced Component.

        Returns
            a: CellArray containing references to the Component.
        """
        if not isinstance(component, Component):
            raise TypeError("add_array() needs a Component object.")
        ref = CellArray(
            device=component,
            columns=int(round(columns)),
            rows=int(round(rows)),
            spacing=spacing,
        )
        ref.name = None
        self._add(ref)
        self._register_reference(reference=ref, alias=alias)
        return ref

    def flatten(self, single_layer: Optional[Tuple[int, int]] = None):
        """Returns a flattened copy of the component.

        Flattens the hierarchy of the Component such that there are no longer
        any references to other Components. All polygons and labels from
        underlying references are copied and placed in the top-level Component.
        If single_layer is specified, all polygons are moved to that layer.

        Args:
            single_layer: move all polygons are moved to the specified (optional).
        """
        component_flat = self.copy()
        component_flat.polygons = []
        component_flat.references = []

        poly_dict = self.get_polygons(by_spec=True)
        for layer, polys in poly_dict.items():
            component_flat.add_polygon(polys, layer=single_layer or layer)

        return component_flat

    def flatten_reference(self, ref: ComponentReference):
        """From existing cell replaces reference with a flatten reference \
        which has the transformations already applied.

        Transformed reference keeps the original name.

        Args:
            ref: the reference to flatten into a new cell.

        """
        from gdsfactory.functions import transformed

        self.remove(ref)
        new_component = transformed(ref, decorator=None)
        self.add_ref(new_component, alias=ref.name)

    def add_ref(
        self, component: "Component", alias: Optional[str] = None
    ) -> "ComponentReference":
        """Add ComponentReference to the current Component."""
        if not isinstance(component, Device):
            raise TypeError(f"type = {type(Component)} needs to be a Component.")
        ref = ComponentReference(component)
        self._add(ref)
        self._register_reference(reference=ref, alias=alias)
        return ref

    def _register_reference(
        self, reference: ComponentReference, alias: Optional[str] = None
    ) -> None:
        component = reference.parent
        reference.owner = self

        if alias is None:
            if reference.name is not None:
                alias = reference.name
            else:
                prefix = (
                    component.settings.function_name
                    if hasattr(component, "settings")
                    and hasattr(component.settings, "function_name")
                    else component.name
                )
                self._reference_names_counter.update({prefix: 1})
                alias = f"{prefix}_{self._reference_names_counter[prefix]}"

                while alias in self._reference_names_used:
                    self._reference_names_counter.update({prefix: 1})
                    alias = f"{prefix}_{self._reference_names_counter[prefix]}"

        reference.name = alias

    def get_layers(self) -> Union[Set[Tuple[int, int]], Set[Tuple[int64, int64]]]:
        """Return a set of (layer, datatype).

        .. code ::

            import gdsfactory as gf
            gf.components.straight().get_layers() == {(1, 0), (111, 0)}
        """
        layers = set()
        for element in itertools.chain(self.polygons, self.paths):
            for layer, datatype in zip(element.layers, element.datatypes):
                layers.add((layer, datatype))
        for reference in self.references:
            for layer, datatype in reference.ref_cell.get_layers():
                layers.add((layer, datatype))
        for label in self.labels:
            layers.add((label.layer, 0))
        return layers

    def _repr_html_(self):
        """Show geometry in klayout and in matplotlib for jupyter notebooks."""
        self.show(show_ports=False)  # show in klayout
        self.plot(plotter="matplotlib")
        return self.__repr__()

    def plot(self, plotter: Optional[Plotter] = None, **kwargs) -> None:
        """Returns component plot.

        Args:
            plotter: backend ('holoviews', 'matplotlib', 'qt').

        KeyError Args:
            layers_excluded: list of layers to exclude.
            layer_colors: layer_colors colors loaded from Klayout.
            min_aspect: minimum aspect ratio.
        """
        plotter = plotter or CONF.get("plotter", "matplotlib")

        if plotter == "matplotlib":
            from gdsfactory.quickplotter import quickplot as plot

            return plot(self)
        elif plotter == "holoviews":
            try:
                import holoviews as hv

                hv.extension("bokeh")
            except ImportError as e:
                print("you need to `pip install holoviews`")
                raise e

            return self.ploth(**kwargs)

        elif plotter == "qt":
            from gdsfactory.quickplotter import quickplot2

            return quickplot2(self)

    def plotqt(self):
        from gdsfactory.quickplotter import quickplot2

        return quickplot2(self)

    def ploth(
        self,
        layers_excluded: Optional[Layers] = None,
        layer_colors: LayerColors = LAYER_COLORS,
        min_aspect: float = 0.25,
        padding: float = 0.5,
    ):
        """Plot component in holoviews.

        Args:
            layers_excluded: list of layers to exclude.
            layer_colors: layer_colors colors loaded from Klayout.
            min_aspect: minimum aspect ratio.
            padding: around bounding box.

        Returns:
            Holoviews Overlay to display all polygons.
        """
        from gdsfactory.add_pins import get_pin_triangle_polygon_tip

        try:
            import holoviews as hv

            hv.extension("bokeh")
        except ImportError as e:
            print("you need to `pip install holoviews`")
            raise e

        self._bb_valid = False  # recompute the bounding box
        b = self.bbox + ((-padding, -padding), (padding, padding))
        b = np.array(b.flat)
        center = np.array((np.sum(b[::2]) / 2, np.sum(b[1::2]) / 2))
        size = np.array((np.abs(b[2] - b[0]), np.abs(b[3] - b[1])))
        dx = np.array(
            (
                np.maximum(min_aspect * size[1], size[0]) / 2,
                np.maximum(size[1], min_aspect * size[0]) / 2,
            )
        )
        b = np.hstack((center - dx, center + dx))

        plots_to_overlay = []
        layers_excluded = [] if layers_excluded is None else layers_excluded

        for layer, polygon in self.get_polygons(by_spec=True).items():
            if layer in layers_excluded:
                continue

            try:
                layer = layer_colors.get_from_tuple(layer)
            except ValueError:
                layers = list(layer_colors._layers.keys())
                warnings.warn(f"{layer!r} not defined in {layers}")
                layer = LayerColor(gds_layer=layer[0], gds_datatype=layer[1])

            plots_to_overlay.append(
                hv.Polygons(polygon, label=str(layer.name)).opts(
                    data_aspect=1,
                    frame_width=500,
                    fill_alpha=layer.alpha,
                    ylim=(b[1], b[3]),
                    xlim=(b[0], b[2]),
                    color=layer.color,
                    line_alpha=layer.alpha,
                    tools=["hover"],
                )
            )
        for name, port in self.ports.items():
            name = str(name)
            polygon, ptip = get_pin_triangle_polygon_tip(port=port)

            plots_to_overlay.append(
                hv.Polygons(polygon, label=name).opts(
                    data_aspect=1,
                    frame_width=500,
                    fill_alpha=0,
                    ylim=(b[1], b[3]),
                    xlim=(b[0], b[2]),
                    color="red",
                    line_alpha=layer.alpha,
                    tools=["hover"],
                )
                * hv.Text(ptip[0], ptip[1], name)
            )

        return hv.Overlay(plots_to_overlay).opts(
            show_legend=True, shared_axes=False, ylim=(b[1], b[3]), xlim=(b[0], b[2])
        )

    def show(
        self,
        show_ports: bool = False,
        show_subports: bool = False,
        port_marker_layer: Layer = "SHOW_PORTS",
        **kwargs,
    ) -> None:
        """Show component in klayout.

        returns a copy of the Component, so the original component remains intact.
        with pins markers on each port show_ports = True, and optionally also
        the ports from the references (show_subports=True)

        Args:
            show_ports: shows component with port markers and labels.
            show_subports: add ports markers and labels to references.
            port_marker_layer: for the ports.

        Keyword Args:
            gdspath: GDS file path to write to.
            gdsdir: directory for the GDS file. Defaults to /tmp/.
            unit: unit size for objects in library. 1um by default.
            precision: for object dimensions in the library (m). 1nm by default.
            timestamp: Defaults to 2019-10-25. If None uses current time.
        """
        from gdsfactory.add_pins import add_pins_triangle
        from gdsfactory.show import show

        if show_subports:
            component = self.copy()
            component.name = self.name
            for reference in component.references:
                add_pins_triangle(
                    component=component,
                    reference=reference,
                    layer=port_marker_layer,
                )

        elif show_ports:
            component = self.copy()
            component.name = self.name
            add_pins_triangle(component=component, layer=port_marker_layer)
        else:
            component = self

        show(component, **kwargs)

    def to_3d(self, *args, **kwargs):
        """Returns Component 3D trimesh Scene.

        Keyword Args:
            component: to exture in 3D.
            layer_colors: layer colors from Klayout Layer Properties file.
                Defaults to active PDK.layer_colors.
            layer_stack: contains thickness and zmin for each layer.
                Defaults to active PDK.layer_stack.
            exclude_layers: layers to exclude.
        """
        from gdsfactory.export.to_3d import to_3d

        return to_3d(self, *args, **kwargs)

    def write_gds(
        self,
        gdspath: Optional[PathType] = None,
        gdsdir: Optional[PathType] = None,
        unit: float = 1e-6,
        precision: float = 1e-9,
        timestamp: Optional[datetime.datetime] = _timestamp2019,
        logging: bool = True,
        on_duplicate_cell: Optional[str] = "warn",
    ) -> Path:
        """Write component to GDS and returns gdspath.

        Args:
            gdspath: GDS file path to write to.
            gdsdir: directory for the GDS file. Defaults to /tmp/randomFile/gdsfactory.
            unit: unit size for objects in library. 1um by default.
            precision: for dimensions in the library (m). 1nm by default.
            timestamp: Defaults to 2019-10-25 for consistent hash.
                If None uses current time.
            logging: disable GDS path logging, for example for showing it in klayout.
            on_duplicate_cell: specify how to resolve duplicate-named cells. Choose one of the following:
                "warn" (default): overwrite all duplicate cells with one of the duplicates (arbitrarily).
                "error": throw a ValueError when attempting to write a gds with duplicate cells.
                "overwrite": overwrite all duplicate cells with one of the duplicates, without warning.
                None: do not try to resolve (at your own risk!)
        """
        gdsdir = (
            gdsdir or pathlib.Path(tempfile.TemporaryDirectory().name) / "gdsfactory"
        )
        gdsdir = pathlib.Path(gdsdir)
        gdspath = gdspath or gdsdir / f"{self.name}.gds"
        gdspath = pathlib.Path(gdspath)
        gdsdir = gdspath.parent
        gdsdir.mkdir(exist_ok=True, parents=True)

        cells = self.get_dependencies(recursive=True)
        cell_names = [cell.name for cell in list(cells)]
        cell_names_unique = set(cell_names)

        if len(cell_names) != len(set(cell_names)):
            for cell_name in cell_names_unique:
                cell_names.remove(cell_name)

            if on_duplicate_cell == "error":
                raise ValueError(
                    f"Duplicated cell names in {self.name!r}: {cell_names!r}"
                )
            elif on_duplicate_cell in {"warn", "overwrite"}:
                if on_duplicate_cell == "warn":
                    warnings.warn(
                        f"Duplicated cell names in {self.name!r}:  {cell_names}",
                    )
                cells_dict = {cell.name: cell for cell in cells}
                cells = cells_dict.values()
            elif on_duplicate_cell is not None:
                raise ValueError(
                    f"on_duplicate_cell: {on_duplicate_cell!r} not in (None, warn, error, overwrite)"
                )

        all_cells = [self] + sorted(cells, key=lambda cc: cc.name)

        no_name_cells = [
            cell.name for cell in all_cells if cell.name.startswith("Unnamed")
        ]

        if no_name_cells:
            warnings.warn(
                f"Component {self.name!r} contains {len(no_name_cells)} Unnamed cells"
            )

        lib = gdspy.GdsLibrary(unit=unit, precision=precision)
        lib.write_gds(gdspath, cells=all_cells, timestamp=timestamp)
        self.path = gdspath
        if logging:
            logger.info(f"Write GDS to {str(gdspath)!r}")
        return gdspath

    def write_gds_with_metadata(self, *args, **kwargs) -> Path:
        """Write component in GDS and metadata (component settings) in YAML."""
        gdspath = self.write_gds(*args, **kwargs)
        metadata = gdspath.with_suffix(".yml")
        metadata.write_text(self.to_yaml(with_cells=True, with_ports=True))
        logger.info(f"Write YAML metadata to {str(metadata)!r}")
        return gdspath

    def write_oas(self, filename, **write_kwargs) -> Path:
        """Write component in OASIS format."""
        if str(filename).lower().endswith(".gds"):
            # you are looking for write_gds
            self.write_gds(filename, **write_kwargs)
            return
        try:
            import klayout.db as pya
        except ImportError as err:
            err.args = (
                "you need klayout package to write OASIS\n"
                "pip install klayout\n" + err.args[0],
            ) + err.args[1:]
            raise
        if not filename.lower().endswith(".oas"):
            filename += ".oas"
        fileroot = os.path.splitext(filename)[0]
        tempfilename = f"{fileroot}-tmp.gds"

        self.write_gds(tempfilename, **write_kwargs)
        layout = pya.Layout()
        layout.read(tempfilename)

        # there can only be one top_cell because we only wrote one device
        topcell = layout.top_cell()
        topcell.write(filename)
        os.remove(tempfilename)
        logger.info(f"Write OASIS to {filename!r}")
        return Path(filename)

    def to_dict(
        self,
        ignore_components_prefix: Optional[List[str]] = None,
        ignore_functions_prefix: Optional[List[str]] = None,
        with_cells: bool = False,
        with_ports: bool = False,
    ) -> Dict[str, Any]:
        """Returns Dict representation of a component.

        Args:
            ignore_components_prefix: for components to ignore when exporting.
            ignore_functions_prefix: for functions to ignore when exporting.
            with_cells: write cells recursively.
            with_ports: write port information dict.
        """
        d = {}
        if with_ports:
            ports = {port.name: port.to_dict() for port in self.get_ports_list()}
            d["ports"] = ports

        if with_cells:
            cells = recurse_structures(
                self,
                ignore_functions_prefix=ignore_functions_prefix,
                ignore_components_prefix=ignore_components_prefix,
            )
            d["cells"] = clean_dict(cells)

        d["name"] = self.name
        d["version"] = self.version
        d["settings"] = dict(self.settings)
        return d

    def to_yaml(self, **kwargs) -> str:
        """Write Dict representation of a component in YAML format.

        Args:
            ignore_components_prefix: for components to ignore when exporting.
            ignore_functions_prefix: for functions to ignore when exporting.
            with_cells: write cells recursively.
            with_ports: write port information.
        """
        return OmegaConf.to_yaml(self.to_dict(**kwargs))

    def to_dict_polygons(self) -> Dict[str, Any]:
        """Returns a dict representation of the flattened component."""
        d = {}
        polygons = {}
        layer_to_polygons = self.get_polygons(by_spec=True)

        for layer, polygons_layer in layer_to_polygons.items():
            for polygon in polygons_layer:
                layer_name = f"{layer[0]}_{layer[1]}"
                polygons[layer_name] = [tuple(snap_to_grid(v)) for v in polygon]

        ports = {port.name: port.settings for port in self.get_ports_list()}
        clean_dict(ports)
        clean_dict(polygons)
        d.info = self.info
        d.polygons = polygons
        d.ports = ports
        return d

    def auto_rename_ports(self, **kwargs) -> None:
        """Rename ports by orientation NSEW (north, south, east, west).

        Keyword Args:
            function: to rename ports.
            select_ports_optical: to select optical ports.
            select_ports_electrical: to select electrical ports.
            prefix_optical: prefix.
            prefix_electrical: prefix.

        .. code::

                 3   4
                 |___|_
             2 -|      |- 5
                |      |
             1 -|______|- 6
                 |   |
                 8   7
        """
        self.is_unlocked()
        auto_rename_ports(self, **kwargs)

    def auto_rename_ports_counter_clockwise(self, **kwargs) -> None:
        self.is_unlocked()
        auto_rename_ports_counter_clockwise(self, **kwargs)

    def auto_rename_ports_layer_orientation(self, **kwargs) -> None:
        self.is_unlocked()
        auto_rename_ports_layer_orientation(self, **kwargs)

    def auto_rename_ports_orientation(self, **kwargs) -> None:
        """Rename ports by orientation NSEW (north, south, east, west).

        Keyword Args:
            function: to rename ports.
            select_ports_optical: to select ports.
            select_ports_electrical:
            prefix_optical:
            prefix_electrical:

        .. code::

                 N0  N1
                 |___|_
            W1 -|      |- E1
                |      |
            W0 -|______|- E0
                 |   |
                S0   S1
        """
        self.is_unlocked()
        auto_rename_ports_orientation(self, **kwargs)

    def move(
        self,
        origin: Float2 = (0, 0),
        destination: Optional[Float2] = None,
        axis: Optional[Axis] = None,
    ) -> "Component":
        """Returns new Component with a moved reference to the original.

        component.

        Args:
            origin: of component.
            destination: x, y.
            axis: x or y.
        """
        from gdsfactory.functions import move

        return move(component=self, origin=origin, destination=destination, axis=axis)

    def mirror(
        self,
        p1: Float2 = (0, 1),
        p2: Float2 = (0, 0),
    ) -> "Component":
        """Returns new Component with a mirrored reference.

        Args:
            p1: first point to define mirror axis.
            p2: second point to define mirror axis.
        """
        from gdsfactory.functions import mirror

        return mirror(component=self, p1=p1, p2=p2)

    def rotate(self, angle: float = 90) -> "Component":
        """Returns a new component with a rotated reference to the original.

        component.

        Args:
            angle: in degrees.
        """
        from gdsfactory.functions import rotate

        return rotate(component=self, angle=angle)

    def add_padding(self, **kwargs) -> "Component":
        """Returns new component with padding.

        Keyword Args:
            component: for padding.
            layers: list of layers.
            suffix for name.
            default: default padding (50um).
            top: north padding.
            bottom: south padding.
            right: east padding.
            left: west padding.
        """
        from gdsfactory.add_padding import add_padding

        return add_padding(component=self, **kwargs)

    def absorb(self, reference) -> "Component":
        """Flattens and absorbs polygons from  ComponentReference into the.

        Component.

        It destroys the reference in the process but keeping the polygon geometry.

        remove when PR gets approved and there is a new release
        https://github.com/amccaugh/phidl/pull/135

        Args:
            reference: ComponentReference to be absorbed into the Component.
        """
        if reference not in self.references:
            raise ValueError(
                """[PHIDL] Component.absorb() failed -
                the reference it was asked to absorb does not
                exist in this Component. """
            )
        ref_polygons = reference.get_polygons(by_spec=True)
        for (layer, polys) in ref_polygons.items():
            [self.add_polygon(points=p, layer=layer) for p in polys]

        self.add(reference.parent.labels)
        self.add(reference.parent.paths)
        self.remove(reference)
        return self

    def remove(self, items):
        """Removes items from a Component, which can include Ports, PolygonSets \
        CellReferences, ComponentReferences and Labels.

        Args:
            items: list of Items to be removed from the Component.
        """
        if not hasattr(items, "__iter__"):
            items = [items]
        for item in items:
            if isinstance(item, Port):
                self.ports = {k: v for k, v in self.ports.items() if v != item}
            elif isinstance(item, gdspy.PolygonSet):
                self.polygons.remove(item)
            elif isinstance(item, (gdspy.CellReference, gdspy.CellArray)):
                self.references.remove(item)
                item.owner = None
            elif isinstance(item, gdspy.Label):
                self.labels.remove(item)

        self._bb_valid = False
        return self

    def hash_geometry(self, precision: float = 1e-4) -> str:
        """Returns an SHA1 hash of the geometry in the Component.

        For each layer, each polygon is individually hashed and then the polygon hashes
        are sorted, to ensure the hash stays constant regardless of the ordering
        the polygons.  Similarly, the layers are sorted by (layer, datatype).

        Args:
            precision: Rounding precision for the the objects in the Component.
                For instance, a precision of 1e-2 will round a point at
                (0.124, 1.748) to (0.12, 1.75).

        """
        polygons_by_spec = self.get_polygons(by_spec=True)
        layers = np.array(list(polygons_by_spec.keys()))
        sorted_layers = layers[np.lexsort((layers[:, 0], layers[:, 1]))]

        final_hash = hashlib.sha1()
        for layer in sorted_layers:
            layer_hash = hashlib.sha1(layer.astype(np.int64)).digest()
            polygons = polygons_by_spec[tuple(layer)]
            polygons = [_rnd(p, precision) for p in polygons]
            polygon_hashes = np.sort([hashlib.sha1(p).digest() for p in polygons])
            final_hash.update(layer_hash)
            for ph in polygon_hashes:
                final_hash.update(ph)

        return final_hash.hexdigest()


def test_get_layers() -> Component:
    import gdsfactory as gf

    c = gf.components.straight(
        length=10,
        width=0.5,
        layer=(2, 0),
        bbox_layers=[(111, 0)],
        bbox_offsets=[3],
        with_bbox=True,
        cladding_layers=None,
        add_pins=None,
        add_bbox=None,
    )
    assert c.get_layers() == {(2, 0), (111, 0)}, c.get_layers()
    c.remove_layers((111, 0))
    assert c.get_layers() == {(2, 0)}, c.get_layers()
    return c


def _filter_polys(polygons, layers_excl):
    return [
        p
        for p, l, d in zip(polygons.polygons, polygons.layers, polygons.datatypes)
        if (l, d) not in layers_excl
    ]


def recurse_structures(
    component: Component,
    ignore_components_prefix: Optional[List[str]] = None,
    ignore_functions_prefix: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Recurse component and its components recursively.

    Args:
        component: component to recurse.
        ignore_components_prefix: list of prefix to ignore.
        ignore_functions_prefix: list of prefix to ignore.
    """
    ignore_functions_prefix = ignore_functions_prefix or []
    ignore_components_prefix = ignore_components_prefix or []

    if (
        hasattr(component, "function_name")
        and component.function_name in ignore_functions_prefix
    ):
        return {}

    if hasattr(component, "name") and any(
        component.name.startswith(i) for i in ignore_components_prefix
    ):
        return {}

    output = {component.name: dict(component.settings)}
    for reference in component.references:
        if (
            isinstance(reference, ComponentReference)
            and reference.ref_cell.name not in output
        ):
            output.update(recurse_structures(reference.ref_cell))

    return output


def test_same_uid() -> None:
    import gdsfactory as gf

    c = Component()
    c << gf.components.rectangle()
    c << gf.components.rectangle()

    r1 = c.references[0].parent
    r2 = c.references[1].parent

    assert r1.uid == r2.uid, f"{r1.uid} must equal {r2.uid}"


def test_netlist_simple() -> None:
    import gdsfactory as gf

    c = gf.Component()
    c1 = c << gf.components.straight(length=1, width=1)
    c2 = c << gf.components.straight(length=2, width=2)
    c2.connect(port="o1", destination=c1.ports["o2"])
    c.add_port("o1", port=c1.ports["o1"])
    c.add_port("o2", port=c2.ports["o2"])
    netlist = c.get_netlist()
    # print(netlist.pretty())
    assert len(netlist["instances"]) == 2


def test_netlist_complex() -> None:
    import gdsfactory as gf

    c = gf.components.mzi_arms()
    netlist = c.get_netlist()
    # print(netlist.pretty())
    assert len(netlist["instances"]) == 4, len(netlist["instances"])


def test_extract() -> None:
    import gdsfactory as gf

    c = gf.components.straight(
        length=10,
        width=0.5,
        bbox_layers=[gf.LAYER.WGCLAD],
        bbox_offsets=[0],
        with_bbox=True,
        cladding_layers=None,
        add_pins=None,
        add_bbox=None,
    )
    c2 = c.extract(layers=[gf.LAYER.WGCLAD])

    assert len(c.polygons) == 2, len(c.polygons)
    assert len(c2.polygons) == 1, len(c2.polygons)


def hash_file(filepath):
    md5 = hashlib.md5()
    md5.update(filepath.read_bytes())
    return md5.hexdigest()


def test_bbox_reference():
    import gdsfactory as gf

    c = gf.Component("component_with_offgrid_polygons")
    c1 = c << gf.components.rectangle(size=(1.5e-3, 1.5e-3), port_type=None)
    c2 = c << gf.components.rectangle(size=(1.5e-3, 1.5e-3), port_type=None)
    c2.xmin = c1.xmax

    assert c2.xsize == 2e-3
    return c2


def test_bbox_component() -> None:
    import gdsfactory as gf

    c = gf.components.rectangle(size=(1.5e-3, 1.5e-3), port_type=None)
    assert c.xsize == 2e-3


if __name__ == "__main__":
    import gdsfactory as gf

    # c = gf.components.bend_euler()
    # c2 = c.mirror()
    # print(c2.info)
    c = gf.c.mzi()
