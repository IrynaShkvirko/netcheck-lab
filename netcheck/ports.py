"""Logical ports and their real interface names on one device (ADR-0001)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from netcheck.intent import Intent
from netcheck.render import port_name


class PortMap(Mapping[str, str]):
    """Logical port -> real interface name for one device ({"p1": "eth1"}).

    Reads like a dict. `real()` and `logical()` translate single names in either
    direction, keeping a VLAN sub-interface suffix (p1.10 <-> eth1.10) and leaving a
    name the intent does not map as it is (a bridge port eth9 stays eth9).
    """

    def __init__(self, logical_to_real: Mapping[str, str] | None = None) -> None:
        self._real = dict(logical_to_real or {})
        self._logical = {real: logical for logical, real in self._real.items()}

    @classmethod
    def for_node(cls, intent: Intent, node: str, prefix: str) -> PortMap:
        """The ports the intent gives `node`, named with the backend's prefix (eth, swp)."""
        return cls(
            {
                ep.port: port_name(ep.port, prefix)
                for lk in intent.links_of(node)
                for ep in (lk.a, lk.b)
                if ep.node == node
            }
        )

    def real(self, name: str) -> str:
        """p2 -> eth2; p1.10 -> eth1.10."""
        base, dot, vid = name.partition(".")
        return self._real.get(base, base) + (f".{vid}" if dot else "")

    def logical(self, name: str) -> str:
        """eth2 -> p2; eth1.10 -> p1.10."""
        base, dot, vid = name.partition(".")
        return self._logical.get(base, base) + (f".{vid}" if dot else "")

    def __getitem__(self, logical: str) -> str:
        return self._real[logical]

    def __iter__(self) -> Iterator[str]:
        return iter(self._real)

    def __len__(self) -> int:
        return len(self._real)

    def __repr__(self) -> str:
        return f"PortMap({self._real!r})"
