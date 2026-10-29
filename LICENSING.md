# Licensing

The project default is **GPL-2.0-only**, whose complete text is in [LICENSE](LICENSE).
The following original project components are provided under the licenses below.
An existing file-level SPDX declaration always takes precedence over this map.

| Component | License |
| --- | --- |
| `driver/`, `include/` except `include/uapi/`, `patches/`, `tests/`, `initcpio/` | GPL-2.0-only |
| `include/uapi/`, `runtime/`, `demo/`, `demo-superres` | MIT |
| `scripts/` | MIT, except files already marked GPL-2.0-only |
| `docs/`, root Markdown guides, requirements files and `.github/` workflows | MIT |
| Other original project files | GPL-2.0-only unless a file-level notice specifies otherwise |

The MIT text is in [LICENSES/MIT.txt](LICENSES/MIT.txt). Existing GPL notices in
analysis/readout tools remain effective. Test harnesses that compile extracted
project source preserve that source’s license; this map does not relicense
included GPL code or any external vendor input.

The project licenses cover the source in this repository. Acquired NVIDIA
libraries, drivers, firmware and generated compiler output are separate external
inputs/artifacts and are not granted these licenses by this repository. See
[NOTICE](NOTICE) for provenance and the model/image sources.

The NASA sample photograph and its measured DLA result in `demo/assets/` have
separate provenance documented in [the asset record](demo/assets/README.md).
The project does not claim copyright in the source NASA photograph.
