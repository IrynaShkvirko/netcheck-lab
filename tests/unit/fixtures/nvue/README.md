# NVUE response samples

What the NVUE REST API (`/nvue_v1/...`) would return for the `campus-mini` lab on Cumulus
Linux: one folder per device, one file per `GET <path>?rev=<rev>`, named
`<name>.<rev>.json` after `PATHS` in `netcheck/backends/nvidia_air.py`.

| File | Path | Shape taken from |
| --- | --- | --- |
| `ospf_neighbors.operational` | `vrf/default/router/ospf/neighbor` | JSON example in the NVUE reference (`nv show vrf default router ospf neighbor <id> -o json`) |
| `interfaces.applied` | `interface` | `nv set interface <id> router ospf ...` and `ip address` object model |
| `interfaces.operational` | `interface` | `link` fields of `nv show interface <id> link`; `bridge domain <id> access/vlan` |
| `routes.operational` | `vrf/default/router/rib/ipv4/route` | route / protocol / entry-index / via command tree (before Cumulus 5.11) |
| `bridge_domain.operational` | `bridge/domain/br_default` | `nv show bridge domain <id>` and its `vlan` children |

**These are assembled from the documentation, not captured from a switch**, and
`provenance.yaml` says so for each file. While any of them is `documentation`, every unit
run prints how many are captured, the tests in `test_nvue.py` carry the
`documentation_samples` marker, lab runs on `--backend nvidia` say the backend is
unverified, and the README status reads unverified. The values come from
`intent/lab.yaml`, so the parsers must produce exactly the state the local backend sees on
a healthy lab (`tests/unit/test_nvue.py` checks that).

When a DSX Air simulation is available, replace them with real responses; the files are
then recorded as captured, with the switch's version:

```python
from netcheck.backends.nvidia_air import record_samples

root = Path("tests/unit/fixtures/nvue")
record_samples(device.ospf.t, root / "r1", provenance_root=root)
```

Any difference the tests then show is a parser bug or a documentation gap, found without
touching the lab tests.
