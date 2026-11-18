"""
ShopMate phone billing dashboard.

Run from the project directory:
    python billing_dashboard.py --port 8082

Open from a phone on the same network:
    http://<PI-IP>:8082/

Features:
- Pi CSI camera barcode scanning
- Persistent stock in products.json
- Scanning adds items to the cart without changing stock.
- Stock is committed after the prototype "Payment Done" confirmation.
- Checkout generates a UPI QR using the configured payee and cart total.

UPI QR:
    Set SHOPMATE_UPI_PAYEE_ID before starting the server.

Important:
    "Payment Done" is a prototype/manual confirmation.
    A real deployment needs a payment gateway/webhook to verify payment.
"""
import argparse
import asyncio
import json
import os
import weakref
from urllib.parse import urlencode

import cv2

from aiohttp import web, WSMsgType

from cart import Catalog, Cart
from billing_scanner import BarcodeScanner


UPI_PAYEE_ID = os.environ.get(
    "SHOPMATE_UPI_PAYEE_ID",
    "",
).strip()
UPI_PAYEE_NAME = os.environ.get(
    "SHOPMATE_UPI_PAYEE_NAME",
    "ShopMate",
).strip()
UPI_AID = os.environ.get(
    "SHOPMATE_UPI_AID",
    "",
).strip()

MJPEG_BOUNDARY = "frame"


PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta
  name="viewport"
  content="width=device-width, initial-scale=1"
>
<title>ShopMate Billing</title>

<style>
  * {
    box-sizing: border-box;
  }

  :root {
    --bg: #0b0f14;
    --panel: #141a22;
    --panel2: #19212b;
    --text: #f5f7fa;
    --muted: #9eabb9;
    --green: #20b26b;
    --green2: #168653;
    --red: #d9534f;
    --amber: #d99614;
    --blue: #4da3ff;
    --line: #283340;
  }

  body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0;
    background: linear-gradient(180deg, #0b0f14 0%, #0e131a 100%);
    color: var(--text);
  }

  .wrap {
    max-width: 820px;
    margin: auto;
    padding: 16px;
  }

  .topbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
  }

  .brand h1 {
    margin: 0;
    font-size: 1.65rem;
  }

  .brand .sub {
    margin-top: 3px;
    color: var(--muted);
    font-size: 0.9rem;
  }

  #status {
    padding: 7px 12px;
    border-radius: 999px;
    background: #17212b;
    color: #9fd0ff;
    font-size: 0.84rem;
    white-space: nowrap;
  }

  .camera-card,
  .card {
    background: rgba(20, 26, 34, 0.98);
    border: 1px solid #202a35;
    border-radius: 16px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, .18);
  }

  .camera-card {
    overflow: hidden;
    margin-bottom: 14px;
  }

  #preview {
    display: block;
    width: 100%;
    max-height: 360px;
    object-fit: contain;
    background: #000;
  }

  .preview-label {
    color: var(--muted);
    padding: 10px 14px 12px;
    font-size: 0.82rem;
  }

  .card {
    padding: 16px;
    margin-bottom: 14px;
  }

  .card-title {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }

  .card-title h2 {
    margin: 0;
    font-size: 1.18rem;
  }

  .scan-hint {
    color: var(--muted);
    font-size: .82rem;
  }

  #lines {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  #lines li {
    padding: 14px 0;
    border-bottom: 1px solid var(--line);
  }

  #lines li:last-child {
    border-bottom: none;
  }

  .line-main {
    display: flex;
    justify-content: space-between;
    gap: 14px;
    align-items: flex-start;
  }

  .product-name {
    font-size: 1.05rem;
    font-weight: 750;
    margin-bottom: 4px;
  }

  .product-meta {
    color: var(--muted);
    font-size: .84rem;
    line-height: 1.45;
  }

  .line-total {
    color: #69f28f;
    font-size: 1.05rem;
    font-weight: 750;
    white-space: nowrap;
  }

  .qty-controls {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
  }

  .qty-btn {
    width: 42px;
    height: 38px;
    border: none;
    border-radius: 10px;
    font-size: 1.35rem;
    font-weight: 800;
    cursor: pointer;
    color: white;
  }

  .qty-btn.minus {
    background: var(--amber);
  }

  .qty-btn.plus {
    background: var(--green2);
  }

  .qty-pill {
    min-width: 50px;
    height: 38px;
    padding: 0 12px;
    border-radius: 10px;
    background: #0f141b;
    border: 1px solid #2a3542;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
  }

  button:disabled {
    opacity: .45;
    cursor: default;
  }

  #empty {
    color: var(--muted);
    padding: 30px 10px;
    text-align: center;
  }

  .empty-icon {
    font-size: 2.2rem;
    margin-bottom: 8px;
  }

  .summary {
    border-top: 1px solid var(--line);
    margin-top: 6px;
    padding-top: 14px;
  }

  .cart-buttons {
    display: grid;
    grid-template-columns: 1fr;
    gap: 9px;
  }

  #clearCartBtn {
    width: 100%;
    background: #3b4653;
    color: white;
    border: none;
    border-radius: 12px;
    padding: 11px 15px;
    font-size: .98rem;
    font-weight: 750;
    cursor: pointer;
  }

  #total {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 1.5rem;
    font-weight: 850;
    margin-bottom: 12px;
  }

  #checkoutBtn {
    width: 100%;
    background: var(--green2);
    color: white;
    border: none;
    border-radius: 12px;
    padding: 13px 15px;
    font-size: 1.05rem;
    font-weight: 800;
    cursor: pointer;
  }

  .inventory-row {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    padding: 9px 0;
    border-bottom: 1px solid var(--line);
  }

  .inventory-row:last-child {
    border-bottom: none;
  }

  .stock {
    color: #9fd0ff;
    font-weight: 750;
  }

  .small {
    color: var(--muted);
    font-size: .8rem;
  }

  #toastWrap {
    position: fixed;
    top: 16px;
    left: 50%;
    transform: translateX(-50%);
    width: min(92vw, 520px);
    z-index: 100;
    pointer-events: none;
  }

  .toast {
    display: none;
    padding: 12px 14px;
    border-radius: 12px;
    background: #10371f;
    border: 1px solid #24673a;
    color: #dfffe9;
    box-shadow: 0 14px 30px rgba(0,0,0,.28);
    font-weight: 700;
  }

  .toast.warn {
    background: #3b2c0b;
    border-color: #765711;
    color: #ffe7a8;
  }


  #receiptPanel {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(3, 7, 12, .90);
    z-index: 60;
    overflow-y: auto;
    padding: 18px;
  }

  .receipt-box {
    max-width: 480px;
    margin: 24px auto;
    background: var(--panel);
    border: 1px solid #24303d;
    padding: 20px;
    border-radius: 18px;
    box-shadow: 0 20px 50px rgba(0,0,0,.35);
  }

  .receipt-success {
    text-align: center;
    color: #9ff3ba;
    font-weight: 800;
    margin-bottom: 8px;
  }

  .receipt-box h2 {
    text-align: center;
    margin: 6px 0 4px;
  }

  .receipt-id,
  .receipt-time {
    text-align: center;
    color: var(--muted);
    font-size: .82rem;
  }

  #receiptLines {
    margin: 16px 0;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
  }

  .receipt-line {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--line);
  }

  .receipt-line:last-child {
    border-bottom: none;
  }

  .receipt-total {
    display: flex;
    justify-content: space-between;
    font-size: 1.35rem;
    font-weight: 850;
    margin: 14px 0;
  }

  .complete-shopping {
    width: 100%;
    border: none;
    border-radius: 11px;
    padding: 13px;
    background: var(--green2);
    color: white;
    font-size: 1rem;
    font-weight: 800;
    cursor: pointer;
  }

  #checkoutPanel {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(3, 7, 12, .88);
    z-index: 50;
    overflow-y: auto;
    padding: 18px;
  }

  .checkout-box {
    max-width: 440px;
    margin: 24px auto;
    background: var(--panel);
    border: 1px solid #24303d;
    padding: 20px;
    border-radius: 18px;
    text-align: center;
    box-shadow: 0 20px 50px rgba(0,0,0,.35);
  }

  .checkout-box h2 {
    margin-top: 0;
  }

  #qr {
    width: min(310px, 92%);
    background: white;
    padding: 10px;
    border-radius: 14px;
    margin: 10px auto;
  }

  .pay-total {
    font-size: 1.7rem;
    font-weight: 850;
    margin: 12px 0;
  }

  .done,
  .cancel {
    width: 100%;
    border: none;
    border-radius: 11px;
    padding: 12px;
    color: white;
    font-size: 1rem;
    font-weight: 800;
    cursor: pointer;
    margin-top: 9px;
  }

  .done {
    background: var(--green2);
  }

  .cancel {
    background: #39424d;
  }

  .success-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: #9ff3ba;
    font-size: .82rem;
  }
</style>
</head>

<body>

<div id="toastWrap">
  <div id="toast" class="toast"></div>
</div>

<div class="wrap">

  <div class="topbar">
    <div class="brand">
      <h1>ShopMate Billing</h1>
      <div class="sub">Scan, review and checkout from your phone</div>
    </div>

    <div id="status">connecting...</div>
  </div>

  <div class="camera-card">
    <img id="preview" src="/stream">
    <div class="preview-label">
      Pi camera live preview — keep the barcode flat, sharp and well lit.
    </div>
  </div>

  <div class="card">
    <div class="card-title">
      <h2>Your Cart</h2>
      <span class="scan-hint">Scan barcode or use + / −</span>
    </div>

    <div id="empty">
      <div class="empty-icon">🛒</div>
      No products yet.<br>
      Scan a barcode to add your first item.
    </div>

    <ul id="lines"></ul>

    <div class="summary">
      <div id="total">
        <span>Total</span>
        <span>₹0.00</span>
      </div>

      <div class="cart-buttons">
        <button
          id="checkoutBtn"
          onclick="checkout()"
          disabled
        >
          Checkout
        </button>

        <button
          id="clearCartBtn"
          onclick="clearCart()"
          disabled
        >
          Clear Cart
        </button>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">
      <h2>Store Inventory</h2>
      <span class="scan-hint">Live stock</span>
    </div>

    <div id="inventory"></div>
  </div>

</div>



<div id="receiptPanel">
  <div class="receipt-box">

    <div class="receipt-success">
      ✓ Payment successfully completed
    </div>

    <h2>Purchase Receipt</h2>

    <div
      id="receiptId"
      class="receipt-id"
    ></div>

    <div
      id="receiptTime"
      class="receipt-time"
    ></div>

    <div id="receiptLines"></div>

    <div class="receipt-total">
      <span>Total Paid</span>
      <span id="receiptTotal">₹0.00</span>
    </div>

    <button
      class="complete-shopping"
      onclick="completeShopping()"
    >
      Complete Shopping
    </button>

    <div
      class="small"
      style="text-align:center; margin-top:9px"
    >
      This ends the current cart session and prepares ShopMate for the next customer.
    </div>

  </div>
</div>


<div id="checkoutPanel">
  <div class="checkout-box">

    <div class="success-badge">
      ✓ Prototype checkout
    </div>

    <h2>Pay with UPI</h2>

    <div id="qrContainer">
      <img
        id="qr"
        src="/upi-qr"
        alt="UPI QR"
        onerror="qrMissing()"
      >
    </div>

    <div
      id="qrMissing"
      class="small"
      style="display:none"
    >
      UPI QR could not be loaded.<br>
      Check the UPI payee configuration and server logs.
    </div>

    <div
      id="checkoutTotal"
      class="pay-total"
    >
      ₹0.00
    </div>

    <div class="small">
      Scan the QR using any UPI app. The exact ShopMate cart total is
      included in the QR, so you do not need to type the amount manually.
      For this prototype, press <strong>Payment Done</strong> after you pay.
    </div>

    <button
      class="done"
      onclick="paymentDone()"
    >
      Payment Done
    </button>

    <button
      class="cancel"
      onclick="cancelCheckout()"
    >
      Back to Cart
    </button>

    <button
      class="cancel"
      onclick="cancelPayment()"
      style="background:#8b2d2d"
    >
      Cancel Payment & Clear Cart
    </button>

  </div>
</div>


<script>
let ws = null;
let latestCart = null;


function connect()
{
  ws = new WebSocket(
    `ws://${location.host}/ws`
  );

  ws.onopen = () =>
  {
    document.getElementById(
      'status'
    ).textContent = '● Connected';
  };

  ws.onclose = () =>
  {
    document.getElementById(
      'status'
    ).textContent =
      '● Reconnecting...';

    setTimeout(
      connect,
      1500
    );
  };

  ws.onerror = () =>
  {
    try
    {
      ws.close();
    }
    catch (_) {}
  };

  ws.onmessage = (event) =>
  {
    const msg = JSON.parse(
      event.data
    );

    if (msg.type === 'cart')
    {
      latestCart = msg.cart;
      renderCart(msg.cart);

      if (msg.inventory)
      {
        renderInventory(
          msg.inventory
        );
      }
    }

    else if (msg.type === 'notice')
    {
      showToast(
        msg.message,
        !!msg.warning
      );
    }

    else if (msg.type === 'scan_success')
    {
      showToast(
        `✓ ${msg.name} scanned successfully`
      );
    }

    else if (msg.type === 'checkout')
    {
      openCheckout(
        msg.total
      );
    }

    else if (msg.type === 'payment_complete')
    {
      closeCheckout();
      showReceipt(msg.receipt);
    }

    else if (msg.type === 'session_complete')
    {
      closeReceipt();

      showToast(
        '✓ Shopping session completed. Ready for the next customer.'
      );
    }
  };
}



function showReceipt(receipt)
{
  const linesRoot =
    document.getElementById(
      'receiptLines'
    );

  linesRoot.innerHTML = '';

  for (const line of receipt.lines)
  {
    const row =
      document.createElement(
        'div'
      );

    row.className =
      'receipt-line';

    const lineTotal =
      Number(line.price)
      *
      Number(line.qty);

    row.innerHTML = `
      <span>
        <strong>${escapeHtml(line.name)}</strong>
        <div class="small">
          ₹${Number(line.price).toFixed(2)} × ${line.qty}
        </div>
      </span>

      <strong>
        ₹${lineTotal.toFixed(2)}
      </strong>
    `;

    linesRoot.appendChild(
      row
    );
  }

  document.getElementById(
    'receiptId'
  ).textContent =
    `Receipt: ${receipt.receipt_id}`;

  document.getElementById(
    'receiptTime'
  ).textContent =
    new Date(
      Number(receipt.paid_at) * 1000
    ).toLocaleString();

  document.getElementById(
    'receiptTotal'
  ).textContent =
    `₹${Number(receipt.total).toFixed(2)}`;

  document.getElementById(
    'receiptPanel'
  ).style.display =
    'block';
}


function closeReceipt()
{
  document.getElementById(
    'receiptPanel'
  ).style.display =
    'none';
}


function completeShopping()
{
  send({
    type: 'complete_session'
  });
}


function showToast(text, warning=false)
{
  const el =
    document.getElementById(
      'toast'
    );

  el.textContent = text;
  el.className =
    warning
      ? 'toast warn'
      : 'toast';

  el.style.display = 'block';

  clearTimeout(
    showToast.timer
  );

  showToast.timer =
    setTimeout(
      () =>
      {
        el.style.display = 'none';
      },
      2600
    );
}


function renderCart(cart)
{
  const ul =
    document.getElementById(
      'lines'
    );

  const empty =
    document.getElementById(
      'empty'
    );

  const checkoutBtn =
    document.getElementById(
      'checkoutBtn'
    );

  const clearCartBtn =
    document.getElementById(
      'clearCartBtn'
    );

  ul.innerHTML = '';

  empty.style.display =
    cart.lines.length
      ? 'none'
      : 'block';

  for (const line of cart.lines)
  {
    const li =
      document.createElement(
        'li'
      );

    const lineTotal =
      Number(line.price)
      *
      Number(line.qty);

    li.innerHTML = `
      <div class="line-main">

        <div>
          <div class="product-name">
            ${escapeHtml(line.name)}
          </div>

          <div class="product-meta">
            ₹${Number(line.price).toFixed(2)} each
          </div>
        </div>

        <div class="line-total">
          ₹${lineTotal.toFixed(2)}
        </div>

      </div>

      <div class="qty-controls">

        <button
          class="qty-btn minus"
          onclick="decrementLine(${line.line_id})"
          ${cart.checkout_active ? 'disabled' : ''}
          aria-label="Remove one"
        >
          −
        </button>

        <div class="qty-pill">
          ${line.qty}
        </div>

        <button
          class="qty-btn plus"
          onclick="incrementLine('${escapeJs(line.barcode)}')"
          ${cart.checkout_active ? 'disabled' : ''}
          aria-label="Add one"
        >
          +
        </button>

      </div>
    `;

    ul.appendChild(li);
  }

  document.getElementById(
    'total'
  ).innerHTML =
    `<span>Total</span>
     <span>₹${Number(cart.total).toFixed(2)}</span>`;

  checkoutBtn.disabled =
    cart.lines.length === 0
    ||
    cart.checkout_active;

  clearCartBtn.disabled =
    cart.lines.length === 0
    ||
    cart.checkout_active;
}


function renderInventory(inventory)
{
  const root =
    document.getElementById(
      'inventory'
    );

  root.innerHTML = '';

  for (
    const [barcode, product]
    of Object.entries(inventory)
  )
  {
    const row =
      document.createElement(
        'div'
      );

    row.className =
      'inventory-row';

    row.innerHTML = `
      <div>
        <strong>${escapeHtml(product.name)}</strong>
        <div class="small">
          ₹${Number(product.price).toFixed(2)}
          &nbsp;•&nbsp;
          Barcode: ${escapeHtml(barcode)}
        </div>
      </div>

      <span class="stock">
        Stock: ${product.stock}
      </span>
    `;

    root.appendChild(row);
  }
}


function incrementLine(barcode)
{
  send({
    type: 'increment',
    barcode: barcode
  });
}


function decrementLine(lineId)
{
  send({
    type: 'decrement',
    line_id: lineId
  });
}


function checkout()
{
  send({
    type: 'checkout'
  });
}


function paymentDone()
{
  send({
    type: 'payment_done'
  });
}


function clearCart()
{
  if (
    confirm(
      'Clear all products from your cart?'
    )
  )
  {
    send({
      type: 'clear_cart'
    });
  }
}


function cancelPayment()
{
  if (
    confirm(
      'Cancel payment and remove every product from the cart?'
    )
  )
  {
    closeCheckout();

    send({
      type: 'cancel_payment'
    });
  }
}


function cancelCheckout()
{
  // Close immediately so the user is never trapped in the checkout modal.
  closeCheckout();

  send({
    type: 'cancel_checkout'
  });

  showToast(
    'Returned to cart'
  );
}


function openCheckout(total)
{
  document.getElementById(
    'checkoutTotal'
  ).textContent =
    `Pay ₹${Number(total).toFixed(2)}`;

  // Reload the QR for every checkout. The server generates it from the
  // server-side cart total, not from a browser-supplied amount.
  const qr = document.getElementById(
    'qr'
  );

  qr.style.display = 'block';

  document.getElementById(
    'qrMissing'
  ).style.display = 'none';

  qr.src =
    `/upi-qr?t=${Date.now()}`;

  document.getElementById(
    'checkoutPanel'
  ).style.display =
    'block';
}


function closeCheckout()
{
  document.getElementById(
    'checkoutPanel'
  ).style.display =
    'none';
}


function qrMissing()
{
  document.getElementById(
    'qr'
  ).style.display =
    'none';

  document.getElementById(
    'qrMissing'
  ).style.display =
    'block';
}


function send(obj)
{
  if (
    ws
    &&
    ws.readyState
      === WebSocket.OPEN
  )
  {
    ws.send(
      JSON.stringify(obj)
    );
  }
}


function escapeHtml(value)
{
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}


function escapeJs(value)
{
  return String(value)
    .replaceAll('\\', '\\\\')
    .replaceAll("'", "\\'");
}


connect();
</script>

</body>
</html>
"""


clients = weakref.WeakSet()


async def index(request):
    return web.Response(
        text=PAGE,
        content_type="text/html",
    )


def build_upi_payment_uri(amount):
    """Build a UPI payment URI with the server-side checkout total."""
    if not UPI_PAYEE_ID:
        raise RuntimeError(
            "Set SHOPMATE_UPI_PAYEE_ID to enable UPI checkout"
        )

    params = {
        "pa": UPI_PAYEE_ID,
        "pn": UPI_PAYEE_NAME,
        "am": f"{float(amount):.2f}",
        "cu": "INR",
        "tn": "ShopMate checkout",
    }

    if UPI_AID:
        params["aid"] = UPI_AID

    return (
        "upi://pay?"
        +
        urlencode(params)
    )


def make_upi_qr_png(amount):
    """Generate a PNG QR code using OpenCV; no extra package is needed."""
    if not hasattr(
        cv2,
        "QRCodeEncoder_create",
    ):
        raise RuntimeError(
            "This OpenCV build does not support QRCodeEncoder_create"
        )

    payment_uri = build_upi_payment_uri(
        amount
    )

    encoder = cv2.QRCodeEncoder_create()
    qr = encoder.encode(
        payment_uri
    )

    # Add the standard quiet zone and enlarge with nearest-neighbour
    # interpolation so phone UPI apps can scan it reliably.
    qr = cv2.copyMakeBorder(
        qr,
        4,
        4,
        4,
        4,
        cv2.BORDER_CONSTANT,
        value=255,
    )

    qr = cv2.resize(
        qr,
        None,
        fx=10,
        fy=10,
        interpolation=cv2.INTER_NEAREST,
    )

    ok, encoded = cv2.imencode(
        ".png",
        qr,
    )

    if not ok:
        raise RuntimeError(
            "Could not encode the UPI QR image"
        )

    return encoded.tobytes()


async def upi_qr_handler(request):
    if not UPI_PAYEE_ID:
        raise web.HTTPServiceUnavailable(
            text="UPI checkout is not configured"
        )

    cart = request.app["cart"]
    snapshot = cart.snapshot()

    # Never accept the amount from the browser. The QR amount always comes
    # from the active server-side cart, so changing the URL cannot generate
    # a cheaper ShopMate checkout QR.
    if (
        not snapshot.get(
            "checkout_active",
            False,
        )
        or
        not snapshot.get(
            "lines"
        )
    ):
        raise web.HTTPBadRequest(
            text="No active checkout"
        )

    total = float(
        snapshot.get(
            "total",
            0.0,
        )
    )

    if total <= 0:
        raise web.HTTPBadRequest(
            text="Checkout total must be greater than zero"
        )

    try:
        png = make_upi_qr_png(
            total
        )
    except Exception as exc:
        raise web.HTTPInternalServerError(
            text=f"Could not generate UPI QR: {exc}"
        )

    return web.Response(
        body=png,
        content_type="image/png",
        headers={
            "Cache-Control":
                "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma":
                "no-cache",
        },
    )


async def stream_handler(request):
    scanner = request.app["scanner"]

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type":
                f"multipart/x-mixed-replace; "
                f"boundary={MJPEG_BOUNDARY}"
        },
    )

    await response.prepare(request)

    try:
        while True:
            jpeg = scanner.get_latest_jpeg()

            if jpeg is not None:
                header = (
                    f"--{MJPEG_BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n"
                    f"\r\n"
                ).encode()

                await response.write(
                    header
                    +
                    jpeg
                    +
                    b"\r\n"
                )

            await asyncio.sleep(0.12)

    except (
        ConnectionResetError,
        asyncio.CancelledError,
        BrokenPipeError,
    ):
        pass

    return response


async def send_notice(app, message, warning=False):
    dead = []

    for ws in list(clients):
        try:
            await ws.send_json({
                "type": "notice",
                "message": message,
                "warning": bool(warning),
            })

        except (
            ConnectionResetError,
            RuntimeError,
        ):
            dead.append(ws)

    for ws in dead:
        clients.discard(ws)


async def send_scan_success(app, name):
    dead = []

    for ws in list(clients):
        try:
            await ws.send_json({
                "type": "scan_success",
                "name": name,
            })

        except (
            ConnectionResetError,
            RuntimeError,
        ):
            dead.append(ws)

    for ws in dead:
        clients.discard(ws)


async def broadcast_cart(app):
    cart = app["cart"]

    payload = {
        "type": "cart",
        "cart": cart.snapshot(),
        "inventory": cart.inventory_snapshot(),
    }

    dead = []

    for ws in list(clients):
        try:
            await ws.send_json(
                payload
            )

        except (
            ConnectionResetError,
            RuntimeError,
        ):
            dead.append(ws)

    for ws in dead:
        clients.discard(ws)


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    clients.add(ws)

    cart = request.app["cart"]

    await ws.send_json({
        "type": "cart",
        "cart": cart.snapshot(),
        "inventory": cart.inventory_snapshot(),
    })

    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue

        try:
            data = json.loads(
                msg.data
            )

            msg_type = data.get(
                "type"
            )

            if msg_type == "increment":
                status, payload = (
                    cart.add_barcode(
                        data["barcode"]
                    )
                )

                if status == "added":
                    await ws.send_json({
                        "type": "scan_success",
                        "name": payload["name"],
                    })

                elif status == "out_of_stock":
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            f"{payload['name']} is out of stock.",
                        "warning": True,
                    })

                elif status == "checkout_locked":
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            "Checkout is active. Go back to the cart first.",
                        "warning": True,
                    })

                elif status == "unknown":
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            f"Product not found for barcode: {payload}",
                        "warning": True,
                    })

                elif status == "session_complete_pending":
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            "Complete the current shopping session before adding new products.",
                        "warning": True,
                    })

                await broadcast_cart(
                    request.app
                )

            elif msg_type == "decrement":
                status, _ = (
                    cart.decrement_line(
                        data["line_id"]
                    )
                )

                if status == "checkout_locked":
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            "Checkout is active. "
                            "Go back to the cart first.",
                    })

                await broadcast_cart(
                    request.app
                )

            elif msg_type == "remove_line":
                status, _ = (
                    cart.remove_line(
                        data["line_id"]
                    )
                )

                if status == "checkout_locked":
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            "Checkout is active. "
                            "Go back to the cart first.",
                    })

                await broadcast_cart(
                    request.app
                )

            elif msg_type == "clear_cart":
                cart.clear_cart()

                await broadcast_cart(
                    request.app
                )

                await ws.send_json({
                    "type": "notice",
                    "message":
                        "Cart cleared. Store stock was not changed.",
                })

            elif msg_type == "cancel_payment":
                cart.cancel_payment_and_clear()

                await broadcast_cart(
                    request.app
                )

                await ws.send_json({
                    "type": "notice",
                    "message":
                        "Payment cancelled. Cart cleared and stock was not changed.",
                })

            elif msg_type == "checkout":
                if not UPI_PAYEE_ID:
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            "UPI checkout is not configured. "
                            "Set SHOPMATE_UPI_PAYEE_ID and restart the server.",
                        "warning": True,
                    })
                    continue

                status, snapshot = (
                    cart.begin_checkout()
                )

                if status == "empty":
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            "Cart is empty. Add at least one product before checkout.",
                        "warning": True,
                    })

                elif status == "session_complete_pending":
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            "Complete the current shopping session first.",
                        "warning": True,
                    })

                else:
                    await broadcast_cart(
                        request.app
                    )

                    await ws.send_json({
                        "type": "checkout",
                        "total":
                            snapshot["total"],
                    })

            elif msg_type == "cancel_checkout":
                cart.cancel_checkout()

                await broadcast_cart(
                    request.app
                )

            elif msg_type == "complete_session":
                cart.complete_shopping_session()

                await broadcast_cart(
                    request.app
                )

                await ws.send_json({
                    "type":
                        "session_complete",
                })

            elif msg_type == "payment_done":
                status, receipt = (
                    cart.complete_checkout()
                )

                if status == "ok":
                    await ws.send_json({
                        "type":
                            "payment_complete",
                        "total":
                            receipt["total"],
                        "receipt":
                            receipt,
                    })

                    await broadcast_cart(
                        request.app
                    )

                elif status == "out_of_stock":
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            f"Payment not completed: {receipt['name']} "
                            f"has only {receipt['available']} left in stock.",
                        "warning": True,
                    })

                    await broadcast_cart(
                        request.app
                    )

                else:
                    await ws.send_json({
                        "type": "notice",
                        "message":
                            "No active checkout.",
                        "warning": True,
                    })

        except Exception as exc:
            await ws.send_json({
                "type": "notice",
                "message":
                    f"Request error: {exc}",
            })

    clients.discard(ws)
    return ws


def make_scanner_callback(
    app,
    loop,
):
    def on_event(
        kind,
        payload,
    ):
        if kind == "added":
            asyncio.run_coroutine_threadsafe(
                broadcast_cart(app),
                loop,
            )

            asyncio.run_coroutine_threadsafe(
                send_scan_success(
                    app,
                    payload["name"],
                ),
                loop,
            )

        elif kind == "unknown":
            asyncio.run_coroutine_threadsafe(
                send_notice(
                    app,
                    f"Product not found for barcode: {payload}",
                    warning=True,
                ),
                loop,
            )

        elif kind == "out_of_stock":
            asyncio.run_coroutine_threadsafe(
                send_notice(
                    app,
                    f"{payload['name']} is out of stock.",
                    warning=True,
                ),
                loop,
            )

        elif kind == "checkout_locked":
            asyncio.run_coroutine_threadsafe(
                send_notice(
                    app,
                    "Checkout is active. "
                    "Barcode scanning is temporarily locked.",
                    warning=True,
                ),
                loop,
            )

        elif kind == "session_complete_pending":
            asyncio.run_coroutine_threadsafe(
                send_notice(
                    app,
                    "Complete the current shopping session before scanning new products.",
                    warning=True,
                ),
                loop,
            )

    return on_event


async def on_startup(app):
    loop = asyncio.get_running_loop()

    catalog = Catalog()
    cart = Cart(
        catalog
    )

    app["cart"] = cart

    scanner = BarcodeScanner(
        on_event=make_scanner_callback(
            app,
            loop,
        ),
        camera_num=0,
    )

    app["scanner"] = scanner
    scanner.start(cart)


async def on_cleanup(app):
    scanner = app.get(
        "scanner"
    )

    if scanner:
        scanner.stop()

    cart = app.get(
        "cart"
    )

    if cart:
        # Clean shutdown before payment:
        # restore any reserved stock.
        snapshot = cart.snapshot()

        if snapshot["lines"]:
            cart.cancel_cart()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--port",
        type=int,
        default=8082,
    )

    args = parser.parse_args()

    app = web.Application()

    app.router.add_get(
        "/",
        index,
    )

    app.router.add_get(
        "/stream",
        stream_handler,
    )

    app.router.add_get(
        "/upi-qr",
        upi_qr_handler,
    )

    app.router.add_get(
        "/ws",
        websocket_handler,
    )

    app.on_startup.append(
        on_startup
    )

    app.on_cleanup.append(
        on_cleanup
    )

    print(
        f"[billing] serving "
        f"http://0.0.0.0:{args.port}/",
        flush=True,
    )

    print(
        "[billing] stock database: "
        "products.json",
        flush=True,
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=args.port,
    )


if __name__ == "__main__":
    main()
