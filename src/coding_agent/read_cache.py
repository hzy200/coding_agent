"""run 级文件内容缓存（带 LRU 与字节上限）。

`SandboxFs` 只需要一个 Mapping：`get` / `[]=` / `pop` / `clear`。
普通 dict 在长会话/大仓库里会无限增长；这里按字节数做 LRU 驱逐，
并记录命中率，供 run_end 指标使用。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator, MutableMapping


class ReadCache(MutableMapping[str, bytes]):
    def __init__(self, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.max_bytes = max(0, int(max_bytes or 0))
        self._data: OrderedDict[str, bytes] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def _drop(self, key: str) -> None:
        value = self._data.pop(key, None)
        if value is not None:
            self._bytes = max(0, self._bytes - len(value))

    def __getitem__(self, key: str) -> bytes:
        value = self._data[key]
        self._data.move_to_end(key)
        self.hits += 1
        return value

    def get(self, key: str, default: bytes | None = None) -> bytes | None:
        if key in self._data:
            self.hits += 1
            self._data.move_to_end(key)
            return self._data[key]
        self.misses += 1
        return default

    def __setitem__(self, key: str, value: bytes) -> None:
        if self.max_bytes <= 0 or len(value) > self.max_bytes:
            return
        if key in self._data:
            self._drop(key)
        while self._data and self._bytes + len(value) > self.max_bytes:
            _oldest, old_value = self._data.popitem(last=False)
            self.evictions += 1
            self._bytes = max(0, self._bytes - len(old_value))
        self._data[key] = value
        self._bytes += len(value)

    def __delitem__(self, key: str) -> None:
        self._drop(key)
        del self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def pop(self, key: str, default: bytes | None = None):  # type: ignore[override]
        if key not in self._data:
            return default
        value = self._data.pop(key)
        self._bytes = max(0, self._bytes - len(value))
        return value

    def clear(self) -> None:
        self._data.clear()
        self._bytes = 0

    @property
    def size_bytes(self) -> int:
        return self._bytes

    def snapshot(self) -> dict[str, int]:
        total = self.hits + self.misses
        return {
            "entries": len(self._data),
            "bytes": self._bytes,
            "max_bytes": self.max_bytes,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate_percent": (
                int(self.hits * 100 / total) if total else 0
            ),
            "evictions": self.evictions,
        }
