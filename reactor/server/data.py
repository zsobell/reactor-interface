"""Post-run files and HTTP routes. This module has no hardware dependencies.

Synchronous endpoints use FastAPI's worker pool. The upload endpoint reads the
body asynchronously, then sends its entire read/merge/save operation to a worker.
Recording has its own executor so analysis cannot queue ahead of run writes.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse

from ..analysis import ellipsometer_merge as ell
from ..run_identity import META_SUFFIX, display_name, next_number, read_identity

def merged_name(reactor_run: str, sidecar: str, filename: str) -> str:
    """Name the merged file after the RUN, not after the dropped refit file.

    The refit comes out of the FS-1 software as "DynData - <timestamp>.txt",
    so naming the output off it produced
    `DynData - 2026-08-21T140934.713_reactor_synced.csv` for a run the
    operator had named Mo-015 - the one file in the set that did not say
    which experiment it belonged to. The reactor run export is the best
    source: it already carries the run name and the run's own timestamp
    (`Mo-015_260821_131320_run.csv`), so the merged file becomes
    `Mo-015_260821_131320_reactor_synced.csv` and sorts next to it.

    Falls back to the sidecar (also run-name prefixed) and finally to the
    refit filename, for an ellipsometry-only merge with neither selected.
    """
    for src, suffix in ((reactor_run, "_run.csv"),
                        (sidecar, "_ellipsometer.csv")):
        if not src:
            continue
        base = Path(src).name
        stem = base[:-len(suffix)] if base.endswith(suffix) else Path(base).stem
        if stem:
            return f"{stem}_reactor_synced.csv"
    return f"{Path(filename).stem or 'refit'}_reactor_synced.csv"


class DataFiles:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()

    def in_data_dir(self, name: str) -> Path:
        """Resolve `name` inside the data dir, refusing anything that escapes
        it. Used by every route below that reads a file by name."""
        d = self.directory
        p = (d / name).resolve()
        if d.resolve() not in p.parents or not p.exists():
            raise HTTPException(404, f"no such file in data dir: {name}")
        return p

    def entries(self, pattern: str) -> list[dict[str, Any]]:
        """Matching files in the data dir AND its per-run subfolders, newest
        first.

        `name` is the path RELATIVE to the data dir ("Mo-015/Mo-015_..._run.csv"
        for a file in a run folder, a bare filename for one still loose in
        data/), which is what `_in_data_dir` resolves and what the picker shows
        - so the folder is visible in the dropdown rather than hidden.

        Sorted by mtime rather than by name: with run folders in play, sorting
        by path orders by folder name, which is not chronological, and the
        analysis page relies on "newest first" to pick up a fresh merge.
        """
        d = self.directory
        out: list[dict[str, Any]] = []
        if d.exists():
            for p in d.rglob(pattern):
                with contextlib.suppress(OSError):
                    st = p.stat()
                    identity = self.identity_for(p)
                    out.append({"name": p.relative_to(d).as_posix(),
                                "size": st.st_size, "mtime": st.st_mtime,
                                "run_name": identity["name"] if identity else None,
                                "display_name": display_name(identity) if identity else None})
        out.sort(key=lambda f: f["mtime"], reverse=True)
        return out

    @staticmethod
    def identity_path(path: Path) -> Path | None:
        for suffix in ("_reactor_synced.csv", "_bycycle.csv", "_run.csv"):
            if path.name.endswith(suffix):
                return path.with_name(path.name[:-len(suffix)] + META_SUFFIX)
        return None

    def identity_for(self, path: Path) -> dict | None:
        identity_path = self.identity_path(path)
        return read_identity(identity_path) if identity_path else None

    def rename(self, name: str, new_name: str) -> dict[str, Any]:
        selected = self.in_data_dir(name)
        identity_path = self.identity_path(selected)
        identity = read_identity(identity_path) if identity_path else None
        if (identity is None or identity_path.parent != selected.parent
                or any(part.startswith("HCPES-") for part in
                       selected.relative_to(self.directory).parts)):
            raise HTTPException(404, "run has no editable analysis name")
        new_name = new_name.strip()
        if (not new_name or len(new_name) > 80
                or any(ord(char) < 32 for char in new_name)):
            raise HTTPException(422, "run name must be 1-80 printable characters")
        if new_name != identity["name"]:
            if new_name.casefold() != identity["name"].casefold():
                identity["number"] = next_number(
                    self.directory, new_name, identity["date"], exclude=identity_path)
            identity["name"] = new_name
            temporary = identity_path.with_suffix(".json.tmp")
            try:
                temporary.write_text(json.dumps(identity, indent=2), encoding="utf-8")
                temporary.replace(identity_path)
            except OSError as exc:
                raise HTTPException(500, f"could not rename run: {exc}") from exc
        return {"name": name, "run_name": identity["name"],
                "display_name": display_name(identity)}

    def delete_targets(self, name: str) -> list[Path]:
        """Find the selected CSV and companions with its exact run stem."""
        if Path(name).is_absolute():
            raise HTTPException(404, "not a deletable run CSV")
        selected = self.in_data_dir(name)
        if (not selected.is_file() or selected.suffix.lower() != ".csv"
                or any(part.startswith("HCPES-") for part in
                       selected.relative_to(self.directory).parts)
                or (self.directory / name).absolute() != selected):
            raise HTTPException(404, "not a deletable run CSV")
        suffixes = (
            "_reactor_synced.csv", "_bycycle.csv", "_run.csv",
            "_events.log", "_errors.log", "_run_params.txt",
            "_ellipsometer.csv", META_SUFFIX,
        )
        suffix = next((end for end in suffixes if selected.name.endswith(end)), None)
        if suffix is None:
            return [selected]
        stem = selected.name[:-len(suffix)]
        targets = [selected.parent / f"{stem}{end}" for end in suffixes]
        return [path for path in targets if path.is_file() and not path.is_symlink()]

    def delete_preview(self, name: str) -> dict[str, Any]:
        return {"name": name, "files": [
            path.relative_to(self.directory).as_posix()
            for path in self.delete_targets(name)]}

    def delete(self, name: str) -> dict[str, Any]:
        targets = self.delete_targets(name)
        removed = []
        for path in targets:
            try:
                path.unlink()
            except OSError as exc:
                raise HTTPException(500, f"could not delete {path.name}: {exc}") from exc
            removed.append(path.relative_to(self.directory).as_posix())
        parent = targets[0].parent
        if parent != self.directory:
            with contextlib.suppress(OSError):
                parent.rmdir()  # only remove the folder when no other data remains
        return {"deleted": removed}

    def merge(self, dyn_text: str, sidecar: str, reactor_run: str = "",
              channels: str = "", filename: str = "refit") -> dict:
        if not dyn_text.strip():
            raise HTTPException(400, "empty refit file body")
        side_text = self.in_data_dir(sidecar).read_text(encoding="utf-8", errors="replace")
        run_text = None
        if reactor_run:
            run_text = self.in_data_dir(reactor_run).read_text(
                encoding="utf-8", errors="replace")
        chans = [c.strip() for c in channels.split(",") if c.strip()] or None
        try:
            result = ell.merge_text(dyn_text, side_text, run_text,
                                    reactor_channels=chans)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        tm = result.time_map
        out_name = merged_name(reactor_run, sidecar, filename)
        csv_text = result.to_csv()

        # Also drop a copy in the data dir. The browser download stays exactly
        # as it was - this is a redundant copy, same reasoning as the run
        # export - so the analysis page can open the merged file straight from
        # the data folder instead of hunting through Downloads. A failure here
        # must not cost the operator the download, so it is only reported.
        saved, save_error = None, None
        if result.n_points:
            try:
                # Into the run's own folder, alongside the files it was built
                # from - not loose in data/. Falls back to the data dir for an
                # ellipsometry-only merge with no run selected.
                out_dir = self.directory
                for src in (reactor_run, sidecar):
                    if src:
                        out_dir = self.in_data_dir(src).parent
                        break
                out_dir.mkdir(parents=True, exist_ok=True)
                requested = Path(out_name)
                stem = requested.stem
                suffix = requested.suffix
                number = 1
                while True:
                    candidate = stem if number == 1 else f"{stem}_{number:02d}"
                    out_path = out_dir / f"{candidate}{suffix}"
                    try:
                        fh = out_path.open("x", encoding="utf-8", newline="")
                        break
                    except FileExistsError:
                        number += 1
                # newline="" matters: the csv module already terminates
                # its rows with CR LF, and writing that back in text mode
                # translates the LF again, giving CR CR LF - which Excel
                # reads as a blank row between every row of data. That is
                # the "every other row empty" the operator hit on Mo-015.
                with fh:
                    fh.write(csv_text)
                out_name = out_path.name
                # Relative to the data dir, so it matches the names in
                # /api/data/files and the page can load it straight back.
                saved = out_path.relative_to(self.directory).as_posix()
            except OSError as exc:
                save_error = f"{type(exc).__name__}: {exc}"

        return {
            "csv": csv_text,
            "filename": out_name,
            "saved_as": saved,
            "save_error": save_error,
            "mode": result.mode,
            "n_points": result.n_points,
            "time_map": {"a": tm.a, "b": tm.b, "n": tm.n,
                         "max_residual_s": tm.max_residual_s},
            "warnings": result.warnings,
            "reactor_channels": result.reactor_channels,
            "ellipsometry_columns": result.ellipsometry_columns,
        }



def create_data_router(
    files: DataFiles, *, before_delete: Callable[[str], None] | None = None,
) -> APIRouter:
    router = APIRouter()
    kinds = {"_bycycle.csv": "by cycle", "_run.csv": "run (by time)",
             "_reactor_synced.csv": "merged + ellipsometry",
             "_ellipsometer.csv": "ellipsometer sidecar"}

    @router.get("/api/data/files")
    def list_files():
        entries = files.entries("*.csv")
        for entry in entries:
            entry["kind"] = next((label for suffix, label in kinds.items()
                                  if entry["name"].endswith(suffix)), "csv")
        return {"dir": str(files.directory), "files": entries}

    @router.get("/api/data/file")
    def get_file(name: str):
        return FileResponse(files.in_data_dir(name), media_type="text/csv")

    @router.get("/api/data/delete-preview")
    def delete_preview(name: str):
        return files.delete_preview(name)

    @router.delete("/api/data/file")
    def delete_file(name: str):
        if before_delete is not None:
            before_delete(name)
        return files.delete(name)

    @router.post("/api/data/rename")
    def rename_file(payload: dict = Body(...)):
        name = str(payload.get("name") or "")
        if before_delete is not None:
            before_delete(name)
        return files.rename(name, str(payload.get("new_name") or ""))

    @router.get("/api/ellipsometer/sidecars")
    def list_sidecars():
        return {"dir": str(files.directory),
                "sidecars": files.entries("*_ellipsometer.csv"),
                "reactor_runs": files.entries("*_run.csv")}

    @router.post("/api/ellipsometer/merge")
    async def merge(request: Request, sidecar: str, reactor_run: str = "",
                    channels: str = "", filename: str = "refit"):
        text = (await request.body()).decode("utf-8", errors="replace")
        return await asyncio.to_thread(files.merge, text, sidecar, reactor_run,
                                       channels, filename)

    return router
