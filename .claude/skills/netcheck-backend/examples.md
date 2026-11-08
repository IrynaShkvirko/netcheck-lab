# netcheck backend: good and bad patterns

Each pair names the rule in `SKILL.md` it illustrates.

## Logical ports through PortMap (the seam)

```python
# bad: the adapter invents real names; breaks on sub-interfaces and other prefixes
def set_link(self, port: str, up: bool) -> None:
    self.apply([("PATCH", f"interface/swp{port[1:]}/link", ...)])


# good
def set_link(self, port: str, up: bool) -> None:
    state = "up" if up else "down"
    self.apply([("PATCH", f"interface/{self.port_map[port]}/link", {"state": {state: {}}})])
```

## Strict parsers

```python
# bad: an unexpected shape becomes "no neighbors", which reads as an OSPF outage
def parse_neighbors(data):
    try:
        return [...]
    except (KeyError, TypeError):
        return []


# good: fail loudly and point at the parser
def _dict(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NvueShapeError(f"{where}: expected an object, got {type(value).__name__}")
    return value
```

## Verified restore

```python
# bad: fire and forget; a link left down breaks every later test silently
def restore_to(self, state):
    for port, admin in state["links"].items():
        self.links.set_link(port, admin == "up")


# good: no-op when equal, apply, re-read, raise with what is still wrong
def restore_to(self, state: dict[str, Any]) -> None:
    if self.take() == state:
        return
    self.apply([("DELETE", "", None), ("PATCH", "", state)])
    after = self.take()
    if after != state:
        raise TransportError(
            f"{self.device}: config still differs after restore: {_diff_keys(state, after)}"
        )
```

## Polling instead of sleeping

```python
# bad
router.links.set_link("p2", up=False)
time.sleep(10)
assert not router.ospf.neighbors()

# good: timeout and poll from timing.yaml; a TransportError from the probe means "not yet"
neighbors, elapsed = timing.wait_until(
    "ospf_neighbor_full", router.ospf.neighbors, lambda ns: not any(n.port == "p2" for n in ns)
)
```

## Transport built only by the backend

```python
# bad: the adapter opens its own connection; untestable without a lab
class NvueOspf(Ospf):
    def __init__(self, host: str) -> None:
        self.t = NvueRestTransport(host, "cumulus", os.environ["NVUE_PASSWORD"], ...)


# good: the factory gets an opener; tests pass FakeNvueTransport
class NvueFactory(CapabilityFactory):
    def __init__(self, open_transport: Callable[[DeviceSpec], ApiTransport]) -> None: ...
```

## Quoting shell commands

```python
# bad: a BPF filter with spaces or quotes breaks the command (or injects one)
self.run(f"tcpdump -i {ifname} -w {path} {bpf} &")

# good
self.run(
    f"tcpdump -i {self.real(port)} -nn -U -w {path} {shlex.quote(bpf)} >/dev/null 2>&1 & echo $!"
)
```
