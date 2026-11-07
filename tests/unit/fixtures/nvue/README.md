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

**These are assembled from the documentation, not captured from a switch.** The values
come from `intent/lab.yaml`, so the parsers must produce exactly the state the local
backend sees on a healthy lab (`tests/unit/test_nvue.py` checks that).

When a DSX Air simulation is available, replace them with real responses:

```python
from netcheck.backends.nvidia_air import record_samples

record_samples(device.ospf.t, Path("tests/unit/fixtures/nvue/r1"))
```

Any difference the tests then show is a parser bug or a documentation gap, found without
touching the lab tests.
