from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import ItemRow


STATUS_TODAY = "today_available"
STATUS_LOW = "today_low_stock"
STATUS_SOLD_OUT = "sold_out"
STATUS_TOMORROW = "tomorrow_only"
STATUS_UNKNOWN = "unknown"


@dataclass
class ItemState:
    item_id: str
    name: str
    requested_qty: int
    stock_qty: int | None
    availability_status: str
    availability_reason: str
    final_qty: int


@dataclass
class BatchValidation:
    kept: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    reduced: list[dict[str, Any]]
    states: list[ItemState]


class VkusvillGateway:
    def derive_item_state(self, item: ItemRow, requested_qty: int) -> ItemState:
        status = str(getattr(item, "availability_status", "") or "").strip() or STATUS_UNKNOWN
        reason = str(getattr(item, "availability_reason", "") or "").strip()
        stock_qty = getattr(item, "stock_qty", None)
        req = max(0, int(requested_qty))

        if status == STATUS_UNKNOWN:
            if stock_qty is None:
                status = STATUS_UNKNOWN
            elif int(stock_qty) <= 0:
                status = STATUS_SOLD_OUT
            elif req > int(stock_qty):
                status = STATUS_LOW
            else:
                status = STATUS_TODAY

        final_qty = req
        if status in {STATUS_SOLD_OUT, STATUS_TOMORROW}:
            final_qty = 0
        elif stock_qty is not None:
            final_qty = min(req, max(0, int(stock_qty)))

        return ItemState(
            item_id=str(item.item_id),
            name=str(item.name),
            requested_qty=req,
            stock_qty=(int(stock_qty) if stock_qty is not None else None),
            availability_status=status,
            availability_reason=reason,
            final_qty=final_qty,
        )

    def validate_selected_rows(self, items_by_id: dict[str, ItemRow], selected: list[dict[str, Any]]) -> BatchValidation:
        kept: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        reduced: list[dict[str, Any]] = []
        states: list[ItemState] = []

        for row in selected:
            item_id = str(row.get("item_id") or "")
            req_qty = int(row.get("qty") or 0)
            item = items_by_id.get(item_id)
            if item is None:
                skipped.append(
                    {
                        "item_id": item_id,
                        "name": str(row.get("name") or ""),
                        "reason": "исчез из текущего набора",
                        "availability_status": STATUS_SOLD_OUT,
                    }
                )
                states.append(
                    ItemState(
                        item_id=item_id,
                        name=str(row.get("name") or ""),
                        requested_qty=req_qty,
                        stock_qty=0,
                        availability_status=STATUS_SOLD_OUT,
                        availability_reason="missing_from_snapshot",
                        final_qty=0,
                    )
                )
                continue

            state = self.derive_item_state(item, req_qty)
            states.append(state)

            if state.availability_status == STATUS_TOMORROW:
                skipped.append(
                    {
                        "item_id": item_id,
                        "name": state.name,
                        "reason": "доступен только завтра",
                        "availability_status": STATUS_TOMORROW,
                    }
                )
                continue

            if state.availability_status == STATUS_SOLD_OUT or state.final_qty <= 0:
                skipped.append(
                    {
                        "item_id": item_id,
                        "name": state.name,
                        "reason": "уже закончился",
                        "availability_status": STATUS_SOLD_OUT,
                    }
                )
                continue

            if state.final_qty < state.requested_qty:
                new_row = dict(row)
                new_row["qty"] = state.final_qty
                kept.append(new_row)
                reduced.append(
                    {
                        "item_id": item_id,
                        "name": state.name,
                        "requested_qty": state.requested_qty,
                        "final_qty": state.final_qty,
                        "availability_status": STATUS_LOW,
                    }
                )
                continue

            kept.append(dict(row))

        return BatchValidation(kept=kept, skipped=skipped, reduced=reduced, states=states)
