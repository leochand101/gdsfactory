# Plugins: FDTD

FDTD: Finite difference time domain wave propagation. Provides broadband wavelength spectrum for a component  described as Sparameters (ratio of output/input field for each port) as a function of wavelength.
  - lumerical Ansys FDTD. See `gdsfactory.simulation.lumerical`
  - meep. See `gdsfactory.simulation.gmeep` (open source)
  - tidy3d. See `gdsfactory.simulation.gtidy`

FDTD simulators can compute the [Sparameters](https://en.wikipedia.org/wiki/Scattering_parameters) response of a component, which measures the input to output field relationship as a function of wavelength or frequency.

![](https://i.imgur.com/RSOTDIN.png)

Sparameters are common in RF and photonic simulation.


```{eval-rst}
.. toctree::
   :maxdepth: 2
   :titlesonly:
   :caption: FDTD:

   notebooks/plugins/lumerical/1_fdtd_sparameters.ipynb
   notebooks/plugins/meep/001_meep_sparameters.ipynb
   notebooks/plugins/meep/002_gratings.ipynb
   notebooks/plugins/tidy3d/00_tidy3d.ipynb
```
