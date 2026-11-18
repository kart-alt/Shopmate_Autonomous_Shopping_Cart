"""
ShopMate billing catalog + cart.

IMPORTANT INVENTORY RULE IN THIS VERSION
----------------------------------------
Scanning / pressing + only adds an item to the shopping cart.
It does NOT reduce products.json stock yet.

Stock changes only after "Payment Done":
    stock 10
    cart x2          -> database still 10
    Payment Done     -> database becomes 8

Removing items, clearing the cart, returning from checkout, or cancelling
payment never changes stock because the purchase has not been committed.

This is suitable for the ShopMate college prototype.
For a production system, use a transactional database and a verified
payment-gateway webhook before committing inventory.
"""

import json
import os
import threading
import time


CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "products.json",
)


class Catalog:
    def __init__(self, path=CATALOG_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._data = {}
        self._load()

    def _load(self):
        with self._lock:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            else:
                self._data = {}
                self._save_locked()

    def _save_locked(self):
        tmp = self.path + ".tmp"

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                self._data,
                f,
                indent=2,
                sort_keys=True,
            )

        os.replace(
            tmp,
            self.path,
        )

    def lookup(self, barcode):
        with self._lock:
            product = self._data.get(
                str(barcode)
            )

            return (
                dict(product)
                if product is not None
                else None
            )

    def snapshot(self):
        with self._lock:
            return {
                barcode: dict(product)
                for barcode, product
                in self._data.items()
            }

    def commit_purchase(self, lines):
        """
        Atomically validate and decrement stock for all purchased cart lines.

        Returns:
            ("ok", updated_inventory)
            ("out_of_stock", payload)

        No stock is changed if any line cannot be fulfilled.
        """
        with self._lock:
            # Validate everything first.
            for line in lines:
                barcode = str(
                    line["barcode"]
                )

                qty = int(
                    line["qty"]
                )

                product = self._data.get(
                    barcode
                )

                if product is None:
                    return (
                        "out_of_stock",
                        {
                            "barcode": barcode,
                            "name": line.get(
                                "name",
                                "Unknown product",
                            ),
                            "available": 0,
                            "requested": qty,
                        },
                    )

                available = int(
                    product.get(
                        "stock",
                        0,
                    )
                )

                if available < qty:
                    return (
                        "out_of_stock",
                        {
                            "barcode": barcode,
                            "name": product.get(
                                "name",
                                line.get(
                                    "name",
                                    "Product",
                                ),
                            ),
                            "available": available,
                            "requested": qty,
                        },
                    )

            # All lines are valid: commit all stock changes.
            for line in lines:
                barcode = str(
                    line["barcode"]
                )

                qty = int(
                    line["qty"]
                )

                self._data[barcode]["stock"] = (
                    int(
                        self._data[barcode].get(
                            "stock",
                            0,
                        )
                    )
                    - qty
                )

            self._save_locked()

            return (
                "ok",
                {
                    barcode: dict(product)
                    for barcode, product
                    in self._data.items()
                },
            )


class Cart:
    """
    Thread-safe shopping cart.

    Stock is NOT reduced when items enter the cart.
    Stock is committed only by complete_checkout().
    """

    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._lock = threading.RLock()
        self._lines = {}
        self._next_id = 1
        self._checkout_active = False
        self._awaiting_session_end = False

    def _qty_in_cart_locked(self, barcode):
        return sum(
            int(line["qty"])
            for line in self._lines.values()
            if line["barcode"] == barcode
        )

    def add_barcode(self, barcode):
        barcode = str(
            barcode
        )

        with self._lock:
            if self._awaiting_session_end:
                return (
                    "session_complete_pending",
                    barcode,
                )

            if self._checkout_active:
                return (
                    "checkout_locked",
                    barcode,
                )

            product = self.catalog.lookup(
                barcode
            )

            if product is None:
                return (
                    "unknown",
                    barcode,
                )

            stock = int(
                product.get(
                    "stock",
                    0,
                )
            )

            qty_in_cart = (
                self._qty_in_cart_locked(
                    barcode
                )
            )

            # Prevent the user from placing more units in the cart
            # than currently exist in store inventory.
            if qty_in_cart >= stock:
                return (
                    "out_of_stock",
                    {
                        "barcode": barcode,
                        "name": product.get(
                            "name",
                            "Product",
                        ),
                        "available": stock,
                    },
                )

            existing = next(
                (
                    line
                    for line in self._lines.values()
                    if line["barcode"] == barcode
                ),
                None,
            )

            if existing:
                existing["qty"] += 1
                line = existing

            else:
                line = {
                    "line_id":
                        self._next_id,
                    "barcode":
                        barcode,
                    "name":
                        product["name"],
                    "price":
                        float(
                            product["price"]
                        ),
                    "qty":
                        1,
                }

                self._lines[
                    self._next_id
                ] = line

                self._next_id += 1

            return (
                "added",
                dict(line),
            )

    def decrement_line(self, line_id):
        line_id = int(
            line_id
        )

        with self._lock:
            if self._checkout_active:
                return (
                    "checkout_locked",
                    None,
                )

            line = self._lines.get(
                line_id
            )

            if not line:
                return (
                    "missing",
                    None,
                )

            line["qty"] -= 1

            if line["qty"] <= 0:
                del self._lines[
                    line_id
                ]

                return (
                    "ok",
                    {
                        "line_id":
                            line_id,
                        "removed":
                            True,
                    },
                )

            return (
                "ok",
                dict(line),
            )

    def remove_line(self, line_id):
        line_id = int(
            line_id
        )

        with self._lock:
            if self._checkout_active:
                return (
                    "checkout_locked",
                    None,
                )

            line = self._lines.pop(
                line_id,
                None,
            )

            if line is None:
                return (
                    "missing",
                    None,
                )

            return (
                "ok",
                {
                    "line_id":
                        line_id,
                    "removed":
                        True,
                },
            )

    def snapshot(self):
        with self._lock:
            lines = [
                dict(line)
                for line
                in self._lines.values()
            ]

            checkout_active = (
                self._checkout_active
            )

            awaiting_session_end = (
                self._awaiting_session_end
            )

        total = sum(
            line["price"]
            *
            line["qty"]
            for line in lines
        )

        return {
            "lines":
                lines,
            "total":
                round(
                    total,
                    2,
                ),
            "checkout_active":
                checkout_active,
            "awaiting_session_end":
                awaiting_session_end,
            "ts":
                time.time(),
        }

    def inventory_snapshot(self):
        return (
            self.catalog.snapshot()
        )

    def begin_checkout(self):
        with self._lock:
            if self._awaiting_session_end:
                return (
                    "session_complete_pending",
                    None,
                )

            if not self._lines:
                return (
                    "empty",
                    None,
                )

            self._checkout_active = True

            lines = [
                dict(line)
                for line
                in self._lines.values()
            ]

            total = round(
                sum(
                    line["price"]
                    *
                    line["qty"]
                    for line in lines
                ),
                2,
            )

            return (
                "ok",
                {
                    "lines":
                        lines,
                    "total":
                        total,
                    "checkout_active":
                        True,
                    "ts":
                        time.time(),
                },
            )

    def cancel_checkout(self):
        """
        Leave checkout and return to the existing cart.
        Inventory remains unchanged.
        """
        with self._lock:
            self._checkout_active = False

        return self.snapshot()

    def clear_cart(self):
        """
        Clear the unpaid shopping cart.

        Inventory remains unchanged because nothing has been purchased.
        """
        with self._lock:
            self._lines.clear()
            self._checkout_active = False
            self._next_id = 1

        return self.snapshot()

    def cancel_payment_and_clear(self):
        """
        Cancel checkout/payment and discard the whole cart.
        Inventory remains unchanged.
        """
        return self.clear_cart()

    def complete_checkout(self):
        """
        Prototype payment completion.

        The dashboard's "Payment Done" button calls this method.
        In this college prototype it is a MANUAL confirmation, not bank
        verification.

        Only here do we decrease persistent inventory.
        """
        with self._lock:
            if not self._checkout_active:
                return (
                    "not_active",
                    None,
                )

            lines = [
                dict(line)
                for line
                in self._lines.values()
            ]

            total = round(
                sum(
                    line["price"]
                    *
                    line["qty"]
                    for line in lines
                ),
                2,
            )

            if not lines:
                self._checkout_active = False

                return (
                    "empty",
                    None,
                )

            # Do the persistent inventory commit while the cart is still
            # locked so quantities cannot change underneath us.
            status, payload = (
                self.catalog.commit_purchase(
                    lines
                )
            )

            if status != "ok":
                self._checkout_active = False

                return (
                    status,
                    payload,
                )

            paid_at = time.time()

            receipt = {
                "receipt_id":
                    f"SM-{int(paid_at * 1000)}",
                "lines":
                    lines,
                "total":
                    total,
                "paid_at":
                    paid_at,
            }

            self._lines.clear()
            self._checkout_active = False
            self._next_id = 1
            self._awaiting_session_end = True

            return (
                "ok",
                receipt,
            )

    def complete_shopping_session(self):
        """
        End the just-paid shopping session and reset ShopMate so the next
        customer starts with a clean, active session.
        """
        with self._lock:
            self._lines.clear()
            self._checkout_active = False
            self._next_id = 1
            self._awaiting_session_end = False

        return self.snapshot()
