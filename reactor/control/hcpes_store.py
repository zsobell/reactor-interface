"""Server-owned persistence for HCPES plans and linked-polarity campaigns."""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import ReactorConfig
from .hcpes_model import (
    CURRENT_PLAN_ID,
    CampaignSession,
    HcpesPlan,
    PolarityCampaign,
    clone_opposite_polarity,
    current_hcpes_plan,
    preview_values,
    resolve_plan,
)


SCHEMA_VERSION = 1


class PlanConflictError(RuntimeError):
    pass


class LinkedSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    plan_id: str
    polarity: Literal[-1, 1]
    directory_name: str = Field(pattern=r"^HCPES-[A-Za-z0-9._-]+$")


class CampaignRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=100)
    plan_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_plan_id: str
    opposite_plan_id: str
    status: Literal["awaiting_opposite", "complete"] = "awaiting_opposite"
    sessions: list[LinkedSession] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def _valid_link(self) -> "CampaignRecord":
        ids = [session.session_id for session in self.sessions]
        polarities = [session.polarity for session in self.sessions]
        if len(ids) != len(set(ids)):
            raise ValueError("campaign session ids must be unique")
        if len(polarities) != len(set(polarities)):
            raise ValueError("campaign session polarities must be unique")
        if self.status == "awaiting_opposite" and len(self.sessions) != 1:
            raise ValueError("an awaiting campaign must contain one source session")
        if self.status == "complete" and set(polarities) != {-1, 1}:
            raise ValueError("a complete campaign requires both polarities")
        return self


class HcpesLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    selected_id: str = CURRENT_PLAN_ID
    plans: list[HcpesPlan] = Field(default_factory=list)
    campaigns: list[CampaignRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "HcpesLibrary":
        plan_ids = [plan.id for plan in self.plans]
        campaign_ids = [campaign.id for campaign in self.campaigns]
        if len(plan_ids) != len(set(plan_ids)):
            raise ValueError("HCPES plan ids must be unique")
        if len(campaign_ids) != len(set(campaign_ids)):
            raise ValueError("HCPES campaign ids must be unique")
        if self.plans and self.selected_id not in set(plan_ids):
            raise ValueError("selected HCPES plan does not exist")
        return self


class HcpesPlanStore:
    """Versioned plan library with atomic writes and immutable run snapshots."""

    def __init__(self, path: Path, cfg: ReactorConfig) -> None:
        self.path = Path(path)
        self.cfg = cfg
        self._lock = threading.RLock()

    def load(self) -> HcpesLibrary:
        with self._lock:
            return self._read().model_copy(deep=True)

    def payload(self) -> dict[str, Any]:
        return self.load().model_dump(mode="json")

    def selected(self) -> HcpesPlan:
        library = self.load()
        return next(plan for plan in library.plans if plan.id == library.selected_id)

    def get(self, plan_id: str) -> HcpesPlan:
        library = self.load()
        plan = next((item for item in library.plans if item.id == plan_id), None)
        if plan is None:
            raise KeyError(f"no such HCPES plan: {plan_id}")
        return plan

    def resolve_saved(self, plan_id: str, expected_revision: int):
        plan = self.get(plan_id)
        if plan.revision != expected_revision:
            raise PlanConflictError(
                "HCPES plan changed since it was reviewed: expected revision "
                f"{expected_revision}, current revision is {plan.revision}")
        return resolve_plan(plan, self.cfg)

    def create(self, name: str, *, from_id: str = CURRENT_PLAN_ID) -> HcpesPlan:
        with self._lock:
            library = self._read()
            source = self._find_plan(library, from_id)
            plan_id = self._unique_id(name, {plan.id for plan in library.plans}, "plan")
            plan = source.model_copy(deep=True, update={
                "id": plan_id,
                "name": name.strip() or "Untitled HCPES plan",
                "revision": 1,
                "builtin": False,
            })
            resolve_plan(plan, self.cfg)
            library.plans.append(plan)
            library.selected_id = plan.id
            self._write(library)
            return plan.model_copy(deep=True)

    def save(
        self,
        plan_id: str,
        payload: dict[str, Any] | HcpesPlan,
        *,
        expected_revision: int,
    ) -> HcpesPlan:
        with self._lock:
            library = self._read()
            index = next((i for i, plan in enumerate(library.plans)
                          if plan.id == plan_id), None)
            if index is None:
                raise KeyError(f"no such HCPES plan: {plan_id}")
            old = library.plans[index]
            if old.builtin:
                raise RuntimeError("the current HCPES template is protected; duplicate it to edit")
            if expected_revision != old.revision:
                raise PlanConflictError(
                    "HCPES plan changed since it was loaded: expected revision "
                    f"{expected_revision}, current revision is {old.revision}")
            incoming = payload if isinstance(payload, HcpesPlan) else (
                HcpesPlan.model_validate(payload))
            if incoming.id != plan_id:
                raise ValueError("HCPES plan id cannot be changed")
            saved = incoming.model_copy(deep=True, update={
                "revision": old.revision + 1,
                "builtin": False,
            })
            resolve_plan(saved, self.cfg)
            library.plans[index] = saved
            self._write(library)
            return saved.model_copy(deep=True)

    def delete(self, plan_id: str) -> HcpesLibrary:
        with self._lock:
            library = self._read()
            plan = self._find_plan(library, plan_id)
            if plan.builtin:
                raise RuntimeError("the current HCPES template cannot be deleted")
            if any(plan_id in {campaign.source_plan_id, campaign.opposite_plan_id}
                   for campaign in library.campaigns):
                raise RuntimeError("the HCPES plan belongs to a linked-polarity campaign")
            library.plans = [item for item in library.plans if item.id != plan_id]
            if library.selected_id == plan_id:
                library.selected_id = CURRENT_PLAN_ID
            self._write(library)
            return library.model_copy(deep=True)

    def select(self, plan_id: str) -> HcpesLibrary:
        with self._lock:
            library = self._read()
            self._find_plan(library, plan_id)
            library.selected_id = plan_id
            self._write(library)
            return library.model_copy(deep=True)

    def preview(
        self,
        plan_id: str | None = None,
        payload: dict[str, Any] | HcpesPlan | None = None,
    ) -> dict[str, Any]:
        if payload is not None and plan_id is not None:
            raise ValueError("preview accepts either plan_id or plan, not both")
        plan = (
            payload if isinstance(payload, HcpesPlan)
            else HcpesPlan.model_validate(payload) if payload is not None
            else self.get(plan_id) if plan_id else self.selected()
        )
        resolved = resolve_plan(plan, self.cfg)
        capabilities = {cap.target: cap for cap in resolved.capabilities}
        return {
            "plan": plan.model_dump(mode="json"),
            "compatibility_signature": resolved.compatibility_signature,
            "nesting": [axis.target for axis in resolved.active_axes],
            "axes": [
                {
                    **axis.model_dump(mode="json", exclude_none=True),
                    "label": capabilities[axis.target].label,
                    "unit": capabilities[axis.target].unit,
                    "role": capabilities[axis.target].role,
                    "count": axis.count,
                    "preview_values": preview_values(axis),
                    "preview_truncated": axis.count > 8,
                }
                for axis in plan.axes
            ],
            "estimate": resolved.estimate().model_dump(mode="json"),
            "cleanup": {
                "all_mfcs_zero": True,
                "ar_isolation_closed_after_zero": True,
                "hv_off": True,
                "hcpes_supply_outputs_off": True,
                "plasma_relay_parked": True,
            },
        }

    def create_opposite(
        self,
        source_plan_id: str,
        *,
        expected_revision: int,
        name: str,
        campaign_name: str,
        source_session: LinkedSession,
        source_signature: str,
    ) -> tuple[HcpesPlan, CampaignRecord]:
        """Clone an exact reviewed plan and durably link its completed run."""
        with self._lock:
            library = self._read()
            source = self._find_plan(library, source_plan_id)
            if source.revision != expected_revision:
                raise PlanConflictError(
                    "HCPES plan changed since the source run: expected revision "
                    f"{expected_revision}, current revision is {source.revision}")
            resolved = resolve_plan(source, self.cfg)
            if source_signature != resolved.compatibility_signature:
                raise ValueError("source session does not match the reviewed HCPES plan")
            if source_session.plan_id != source.id:
                raise ValueError("source session plan id does not match")
            if source_session.polarity != source.stage_polarity:
                raise ValueError("source session polarity does not match")
            existing_plan_ids = {plan.id for plan in library.plans}
            opposite_id = self._unique_id(name, existing_plan_ids, "opposite")
            opposite = clone_opposite_polarity(
                source, new_id=opposite_id,
                new_name=name.strip() or f"{source.name} opposite polarity",
            )
            campaign_id = self._unique_id(
                campaign_name,
                {campaign.id for campaign in library.campaigns},
                "campaign",
            )
            campaign = CampaignRecord(
                id=campaign_id,
                name=campaign_name.strip() or f"{source.name} polarities",
                plan_signature=resolved.compatibility_signature,
                source_plan_id=source.id,
                opposite_plan_id=opposite.id,
                sessions=[source_session],
            )
            library.plans.append(opposite)
            library.campaigns.append(campaign)
            library.selected_id = opposite.id
            self._write(library)
            return opposite.model_copy(deep=True), campaign.model_copy(deep=True)

    def validate_campaign_start(
        self, campaign_id: str, plan: HcpesPlan, session_id: str,
    ) -> CampaignRecord:
        campaign = self._find_campaign(self.load(), campaign_id)
        if campaign.status != "awaiting_opposite":
            raise RuntimeError("the linked-polarity campaign is already complete")
        if plan.id != campaign.opposite_plan_id:
            raise ValueError("campaign must run its exact opposite-polarity plan")
        resolved = resolve_plan(plan, self.cfg)
        if resolved.compatibility_signature != campaign.plan_signature:
            raise ValueError("opposite-polarity plan no longer matches its campaign")
        if plan.stage_polarity == campaign.sessions[0].polarity:
            raise ValueError("campaign follow-up must use the opposite stage polarity")
        LinkedSession(
            session_id=session_id,
            plan_id=plan.id,
            polarity=plan.stage_polarity,
            directory_name=f"HCPES-{session_id}",
        )
        return campaign

    def campaign_candidate(
        self,
        campaign_id: str,
        *,
        plan: HcpesPlan,
        session_id: str,
        directory_name: str,
    ) -> tuple[PolarityCampaign, dict[str, str], LinkedSession]:
        campaign = self.validate_campaign_start(campaign_id, plan, session_id)
        followup = LinkedSession(
            session_id=session_id,
            plan_id=plan.id,
            polarity=plan.stage_polarity,
            directory_name=directory_name,
        )
        sessions = [*campaign.sessions, followup]
        model = PolarityCampaign(
            id=campaign.id,
            name=campaign.name,
            sessions=[
                CampaignSession(
                    session_id=session.session_id,
                    polarity=session.polarity,
                    plan_signature=campaign.plan_signature,
                )
                for session in sessions
            ],
        )
        return model, {
            session.session_id: session.directory_name for session in sessions
        }, followup

    def mark_campaign_complete(
        self, campaign_id: str, followup: LinkedSession,
    ) -> CampaignRecord:
        with self._lock:
            library = self._read()
            index = next((i for i, campaign in enumerate(library.campaigns)
                          if campaign.id == campaign_id), None)
            if index is None:
                raise KeyError(f"no such HCPES campaign: {campaign_id}")
            old = library.campaigns[index]
            if old.status == "complete":
                return old.model_copy(deep=True)
            completed = old.model_copy(deep=True, update={
                "status": "complete",
                "sessions": [*old.sessions, followup],
            })
            completed = CampaignRecord.model_validate(completed.model_dump())
            library.campaigns[index] = completed
            self._write(library)
            return completed.model_copy(deep=True)

    def _read(self) -> HcpesLibrary:
        baseline = current_hcpes_plan(self.cfg)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            library = HcpesLibrary.model_validate(raw)
        except FileNotFoundError:
            return HcpesLibrary(plans=[baseline])
        except Exception as exc:
            raise RuntimeError(f"could not load HCPES plans: {exc}") from exc
        others = [plan for plan in library.plans if plan.id != CURRENT_PLAN_ID]
        selected = library.selected_id
        if selected == CURRENT_PLAN_ID or not any(plan.id == selected for plan in others):
            selected = CURRENT_PLAN_ID
        merged = library.model_copy(deep=True, update={
            "selected_id": selected,
            "plans": [baseline, *others],
        })
        return HcpesLibrary.model_validate(merged.model_dump())

    def _write(self, library: HcpesLibrary) -> None:
        validated = HcpesLibrary.model_validate(library.model_dump())
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
    def _find_plan(library: HcpesLibrary, plan_id: str) -> HcpesPlan:
        plan = next((item for item in library.plans if item.id == plan_id), None)
        if plan is None:
            raise KeyError(f"no such HCPES plan: {plan_id}")
        return plan

    @staticmethod
    def _find_campaign(library: HcpesLibrary, campaign_id: str) -> CampaignRecord:
        campaign = next(
            (item for item in library.campaigns if item.id == campaign_id), None)
        if campaign is None:
            raise KeyError(f"no such HCPES campaign: {campaign_id}")
        return campaign

    @staticmethod
    def _unique_id(name: str, existing: set[str], fallback: str) -> str:
        stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or fallback
        candidate, suffix = stem, 2
        while candidate in existing or candidate == CURRENT_PLAN_ID:
            candidate = f"{stem}-{suffix}"
            suffix += 1
        return candidate
