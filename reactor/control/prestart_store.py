"""Server-owned persistence for user-authored pre-start recipes."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import threading
from typing import Any

from .prestart_model import (
    CURRENT_RECIPE_ID,
    PrestartLibrary,
    PrestartRecipe,
    capability_catalog,
    current_prestart_recipe,
    resolve_recipe,
)
from ..config import ReactorConfig


class RecipeConflictError(RuntimeError):
    pass


class PrestartRecipeStore:
    """Versioned recipe library with atomic replacement and revision checks."""

    def __init__(self, path: Path, cfg: ReactorConfig) -> None:
        self.path = Path(path)
        self.cfg = cfg
        self.catalog = capability_catalog(cfg)
        self._lock = threading.RLock()

    def load(self) -> PrestartLibrary:
        with self._lock:
            library = self._read()
            return library.model_copy(deep=True)

    def selected(self) -> PrestartRecipe:
        library = self.load()
        return next(r for r in library.recipes if r.id == library.selected_id)

    def get(self, recipe_id: str) -> PrestartRecipe:
        library = self.load()
        recipe = next((r for r in library.recipes if r.id == recipe_id), None)
        if recipe is None:
            raise KeyError(f"no such pre-start recipe: {recipe_id}")
        return recipe

    def resolve(self, recipe_id: str | None = None,
                values: dict[str, Any] | None = None):
        recipe = self.get(recipe_id) if recipe_id else self.selected()
        return resolve_recipe(recipe, self.catalog, values)

    def create(self, name: str, *, from_id: str = CURRENT_RECIPE_ID) -> PrestartRecipe:
        with self._lock:
            library = self._read()
            source = next((r for r in library.recipes if r.id == from_id), None)
            if source is None:
                raise KeyError(f"no such pre-start recipe: {from_id}")
            recipe_id = self._unique_id(name, {r.id for r in library.recipes})
            recipe = source.model_copy(deep=True, update={
                "id": recipe_id, "name": name.strip() or "Untitled pre-start",
                "revision": 1, "builtin": False,
            })
            library.recipes.append(recipe)
            library.selected_id = recipe.id
            self._write(library)
            return recipe.model_copy(deep=True)

    def save(self, recipe_id: str, payload: dict[str, Any] | PrestartRecipe,
             *, expected_revision: int) -> PrestartRecipe:
        with self._lock:
            library = self._read()
            index = next((i for i, r in enumerate(library.recipes)
                          if r.id == recipe_id), None)
            if index is None:
                raise KeyError(f"no such pre-start recipe: {recipe_id}")
            old = library.recipes[index]
            if old.builtin:
                raise RuntimeError("the Current pre-start is protected; duplicate it to edit")
            if expected_revision != old.revision:
                raise RecipeConflictError(
                    f"recipe changed since it was loaded: expected revision "
                    f"{expected_revision}, current revision is {old.revision}")
            incoming = payload if isinstance(payload, PrestartRecipe) else (
                PrestartRecipe.model_validate(payload))
            if incoming.id != recipe_id:
                raise ValueError("recipe id cannot be changed")
            saved = incoming.model_copy(deep=True, update={
                "revision": old.revision + 1, "builtin": False,
            })
            # Saving a broken target/action combination is refused. This is
            # pure validation and cannot touch hardware.
            resolve_recipe(saved, self.catalog)
            library.recipes[index] = saved
            self._write(library)
            return saved.model_copy(deep=True)

    def delete(self, recipe_id: str) -> PrestartLibrary:
        with self._lock:
            library = self._read()
            recipe = next((r for r in library.recipes if r.id == recipe_id), None)
            if recipe is None:
                raise KeyError(f"no such pre-start recipe: {recipe_id}")
            if recipe.builtin:
                raise RuntimeError("the Current pre-start cannot be deleted")
            library.recipes = [r for r in library.recipes if r.id != recipe_id]
            if library.selected_id == recipe_id:
                library.selected_id = CURRENT_RECIPE_ID
            self._write(library)
            return library.model_copy(deep=True)

    def select(self, recipe_id: str) -> PrestartLibrary:
        with self._lock:
            library = self._read()
            if not any(r.id == recipe_id for r in library.recipes):
                raise KeyError(f"no such pre-start recipe: {recipe_id}")
            library.selected_id = recipe_id
            self._write(library)
            return library.model_copy(deep=True)

    def payload(self) -> dict[str, Any]:
        library = self.load()
        return {
            "schema_version": library.schema_version,
            "selected_id": library.selected_id,
            "recipes": [r.model_dump(mode="json") for r in library.recipes],
        }

    def preview(self, recipe_id: str | None = None,
                values: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = self.resolve(recipe_id, values)
        return resolved.model_dump(mode="json")

    def _read(self) -> PrestartLibrary:
        baseline = current_prestart_recipe(self.cfg)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            library = PrestartLibrary.model_validate(raw)
        except FileNotFoundError:
            return PrestartLibrary(recipes=[baseline])
        except Exception as exc:
            raise RuntimeError(f"could not load pre-start recipes: {exc}") from exc
        # The protected baseline is code-owned so a schema upgrade or manual
        # file edit cannot quietly change what "Current pre-start" means.
        others = [r for r in library.recipes if r.id != CURRENT_RECIPE_ID]
        selected = library.selected_id
        if selected == CURRENT_RECIPE_ID or not any(r.id == selected for r in others):
            selected = CURRENT_RECIPE_ID
        return PrestartLibrary(selected_id=selected, recipes=[baseline, *others])

    def _write(self, library: PrestartLibrary) -> None:
        validated = PrestartLibrary.model_validate(library.model_dump())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        text = json.dumps(validated.model_dump(mode="json"), indent=2) + "\n"
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _unique_id(name: str, existing: set[str]) -> str:
        stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "prestart"
        candidate, suffix = stem, 2
        while candidate in existing or candidate == CURRENT_RECIPE_ID:
            candidate = f"{stem}-{suffix}"
            suffix += 1
        return candidate
