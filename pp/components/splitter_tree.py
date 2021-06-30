from typing import Optional

import numpy as np

import pp
from pp.tech import FACTORY, Factory
from pp.types import StrOrDict


@pp.cell_with_validator
def splitter_tree(
    coupler: StrOrDict = "mmi1x2",
    noutputs: int = 4,
    dy: float = 50.0,
    dx: float = 90.0,
    bend_s: Optional[StrOrDict] = "bend_s",
    waveguide: str = "strip",
    factory: Factory = FACTORY,
    **kwargs,
) -> pp.Component:
    """Tree of 1x2 splitters

    Args:
        coupler: 1x2 coupler factory name or dict
        noutputs:
        dx: x spacing between couplers
        dy: y spacing between couplers
        bend_s: Sbend factory name or dict for termination
        waveguide: waveguide
        kwargs: waveguide_settings

    .. code::

             __|
          __|  |__
        _|  |__
         |__        dy

          dx

    """
    c = pp.Component()

    coupler = factory.get_component(coupler, waveguide=waveguide, **kwargs)
    if bend_s:
        dy_coupler_ports = abs(
            coupler.ports["E0"].midpoint[1] - coupler.ports["E1"].midpoint[1]
        )
        height = dy / 4 - dy_coupler_ports / 2
        bend_s = factory.get_component(
            bend_s, waveguide=waveguide, length=dx, height=height, **kwargs
        )
    cols = int(np.log2(noutputs))
    i = 0

    for col in range(cols):
        ncouplers = int(2 ** col)
        y0 = -0.5 * dy * 2 ** (cols - 1)
        for row in range(ncouplers):
            x = col * dx
            y = y0 + (row + 0.5) * dy * 2 ** (cols - col - 1)
            coupler_ref = c.add_ref(coupler)
            coupler_ref.move((x, y))
            c.aliases[f"coupler_{col}_{row}"] = coupler_ref
            if col == 0:
                for port in coupler_ref.get_ports_list():
                    if port.name not in ["E0", "E1"]:
                        c.add_port(name=f"{port.name}_{i}", port=port)
                        i += 1
            if col > 0 and row % 2 == 0:
                port_name = "E0"
            if col > 0 and row % 2 == 1:
                port_name = "E1"
            if col > 0:
                c.add(
                    pp.routing.get_route(
                        c.aliases[f"coupler_{col-1}_{row//2}"].ports[port_name],
                        coupler_ref.ports["W0"],
                        waveguide=waveguide,
                        **kwargs,
                    ).references
                )
            if cols > col > 0:
                for port in coupler_ref.get_ports_list():
                    if port.name not in ["W0", "E0", "E1"]:
                        c.add_port(name=f"{port.name}_{i}", port=port)
                        i += 1
            if col == cols - 1 and bend_s is None:
                for port in coupler_ref.get_ports_list():
                    if port.name != "W0":
                        c.add_port(name=f"{port.name}_{i}", port=port)
                        i += 1
            if col == cols - 1 and bend_s:
                btop = c << bend_s
                bbot = c << bend_s
                bbot.mirror()
                btop.connect("W0", coupler_ref.ports["E1"])
                bbot.connect("W0", coupler_ref.ports["E0"])
                c.add_port(name=f"E_{i}", port=btop.ports["E0"])
                i += 1
                c.add_port(name=f"E_{i}", port=bbot.ports["E0"])
                i += 1

    return c


def test_splitter_tree_ports():
    c = splitter_tree(
        coupler="mmi2x2",
        noutputs=4,
        waveguide="nitride",
    )
    assert len(c.ports) == 8


if __name__ == "__main__":

    c = splitter_tree(
        coupler=dict(component="mmi2x2", gap_mmi=2),
        # noutputs=128 * 2,
        noutputs=2 ** 3,
        # waveguide="nitride",
        bend_s=None,
    )
    print(len(c.ports))
    # for port in c.get_ports_list():
    #     print(port)
    c.show()
