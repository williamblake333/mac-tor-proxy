from ._base import _Dummy, _Meta, Signal, make_module_getattr
__getattr__ = make_module_getattr(globals())

class QMainWindow(_Dummy):
    def __getattr__(self, name):
        if "_" in name.strip("_"):
            raise AttributeError(f"MainWindow has no attribute {name!r} (typo?)")
        return _Dummy.__getattr__(self, name)

class QLabel(_Dummy):
    def __init__(self, text="", *a, **k): object.__setattr__(self, "_t", text)
    def setText(self, t): object.__setattr__(self, "_t", t)
    def text(self): return self._t

class QTableWidgetItem(_Dummy):
    def __init__(self, text=""):
        for k, v in dict(_t=text, _d={}, _flags="editable", _cs=None, _r=-1, _c=-1).items():
            object.__setattr__(self, k, v)
    def text(self): return self._t
    def setText(self, t): object.__setattr__(self, "_t", t)
    def data(self, role): return self._d.get("u")
    def setData(self, role, v): self._d["u"] = v
    def setCheckState(self, s): object.__setattr__(self, "_cs", s)
    def checkState(self): return self._cs
    def row(self): return self._r
    def column(self): return self._c

class _Idx:
    def __init__(self, r): self._r = r
    def row(self): return self._r

class QTableWidget(_Dummy):
    def __init__(self, rows=0, cols=0):
        object.__setattr__(self, "rows", [])
        object.__setattr__(self, "sel", set())
        object.__setattr__(self, "itemChanged", Signal())
        object.__setattr__(self, "customContextMenuRequested", Signal())
    def setRowCount(self, n):
        del self.rows[n:]
        self.sel.clear()
    def rowCount(self): return len(self.rows)
    def insertRow(self, r): self.rows.insert(r, {})
    def setItem(self, r, c, it):
        object.__setattr__(it, "_r", r); object.__setattr__(it, "_c", c); self.rows[r][c] = it
    def setCellWidget(self, r, c, w): self.rows[r][("w", c)] = w
    def cellWidget(self, r, c): return self.rows[r].get(("w", c))
    def item(self, r, c): return self.rows[r].get(c) if 0 <= r < len(self.rows) else None
    def selectRow(self, r): self.sel.clear(); self.sel.add(r)
    def selectedIndexes(self): return [_Idx(r) for r in sorted(self.sel) for _ in range(5)]

class QComboBox(_Dummy):
    def __init__(self):
        object.__setattr__(self, "items", []); object.__setattr__(self, "idx", -1)
        object.__setattr__(self, "currentIndexChanged", Signal())
    def addItem(self, text, data=None):
        self.items.append((text, data))
        if self.idx < 0: object.__setattr__(self, "idx", 0)
    def findData(self, d):
        return next((i for i, (_, x) in enumerate(self.items) if x == d), -1)
    def setCurrentIndex(self, i):
        changed = i != self.idx
        object.__setattr__(self, "idx", i)
        if changed: self.currentIndexChanged.emit(i)
    def currentData(self): return self.items[self.idx][1]
